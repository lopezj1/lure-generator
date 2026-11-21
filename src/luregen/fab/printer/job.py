"""`lure fab --format print`: a print build folder → sliced, paused, verified G-code (#35, #51).

Reads only the build folder (resolved.toml, report.json, model/<part>.stl) plus the
printer's spec folder (machine.toml, flattened profiles, reference/). Writes `<build>/fab/print/`:

- `<part>.stl`: the part oriented for printing (print coordinates), as sliced: head and
  tail upright, a separate tail fin (#123) flat on its side
- `<part>.gcode`: sliced on its own in the part's material (fin: TPU), with the
  cavity-ceiling pause (sections with a cavity) and the thumbnails
- `<section>.slice.log`: the slicer command and output
- `print.json` / `print.md`: plan (slots, supports, overrides), per-part time, grams,
  interface grams, tool changes, layers, pause layer/Z, thumbnails and the reference diff

Parts: a print build prints head, tail and (if any) the fin; a CNC build only its printed
parts (the fin), so `lure fab <cnc build> --format print` makes the fin's G-code. `parts`
(`--parts`) picks a subset.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from luregen import progress
from luregen.config import LureConfig, load_resolved
from luregen.fab.printer import gcode_post, orca
from luregen.fab.printer import thumbnails as th
from luregen.fab.printer.machine import PrinterMachine, load_machine
from luregen.fab.printer.orient import OrientedSection, bed_center, orient_section
from luregen.fab.printer.plan import FLAT_PARTS, JobPlan, part_material, plan_job
from luregen.fab.printer.verify import VerifyReport, verify_against_references

OUT_DIR = "fab/print"  # web PrintView and archive groups expect this path
SECTIONS = ("head", "tail")
PRINTED_PARTS = ("fin",)  # separately printed parts, also for CNC builds (#123)
# Progress: `plan` at 0, then each section gets an equal share of [SECTIONS_START, REPORT_AT],
# split by the per-section stage offsets below (slicing takes most of the time).
SECTIONS_START, REPORT_AT = 0.02, 0.98
SECTION_STAGES = {"orient": 0.0, "slice": 0.05, "post": 0.85, "verify": 0.93}


class PrintJobError(RuntimeError):
    pass


def emit(stage: str, section: int = 0, n_sections: int = 1, msg: str = "") -> None:
    """Overall, monotonic progress for `stage` of section `section` (0-based) of `n_sections`."""
    if stage == "plan":
        frac = 0.0
    elif stage == "report":
        frac = REPORT_AT
    else:
        share = (REPORT_AT - SECTIONS_START) / max(n_sections, 1)
        frac = SECTIONS_START + share * (section + SECTION_STAGES[stage])
    progress.emit(stage, round(frac, 4), msg)


@dataclass
class SectionResult:
    name: str
    gcode: str | None  # build-relative
    stl: str
    ok: bool
    orient: dict[str, Any]
    slice_log: str
    stats: dict[str, Any] = field(default_factory=dict)
    pause: dict[str, Any] | None = None
    material: str | None = None
    tool: str | None = None
    thumbnails: list[str] = field(default_factory=list)
    verify: dict[str, Any] | None = None
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "name"}


@dataclass
class PrintResult:
    out_dir: Path
    sections: list[SectionResult]
    summary: dict[str, Any]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.sections) and all(s.ok for s in self.sections)

    @property
    def files(self) -> list[str]:
        return [s.gcode for s in self.sections if s.gcode]


def default_machine(spec: Path) -> str:
    folders = sorted(p.name for p in (spec / "printers").iterdir() if p.is_dir())
    if len(folders) != 1:
        raise PrintJobError(f"choose a printer with --machine ({', '.join(folders)})")
    return folders[0]


def section_stats(summary: Any, plan: JobPlan, gcode_path: Path | None = None) -> dict[str, Any]:
    """Time, grams (total, body, interface), tool changes and layers of one sliced section."""
    by_slot = summary.filament_g_by_slot

    def grams(slot: int | None) -> float | None:
        if slot is None or slot >= len(by_slot):
            return None
        return round(by_slot[slot], 3)

    return {
        "time_s": summary.estimated_time_s,
        "filament_g": summary.filament_g,
        "filament_mm": summary.filament_mm,
        "filament_g_by_slot": by_slot,
        "body_g": grams(plan.body.slot),
        "interface_g": grams(plan.interface.slot) if plan.interface else 0.0,
        "tool_changes": summary.tool_changes,
        "tools_used": [f"T{t}" for t in summary.tools_used],
        "layers": summary.layer_count,
        "max_z_mm": summary.max_z,
        "generator": f"{summary.generator} {summary.generator_version}"
        if summary.generator
        else None,
    }


def pause_height(report: dict, section: str) -> float | None:
    """Cavity-ceiling Z of a section in lure coordinates, from report.json (F3)."""
    internals = report.get("geometry", {}).get("internals") or {}
    ceiling = (internals.get(section) or {}).get("cavity_ceiling_z_mm")
    return None if ceiling is None else float(ceiling)


def insert_pause(
    gcode: Path, oriented: OrientedSection, ceiling_lure_z: float, command: str
) -> dict[str, Any]:
    """Pause before the first layer at or above the cavity ceiling (after orientation)."""
    z = oriented.print_z(ceiling_lure_z)
    pauses = gcode_post.insert_pauses_file(gcode, [z], command)
    p = pauses[0]
    return {
        "command": command,
        "cavity_ceiling_lure_z_mm": round(ceiling_lure_z, 3),
        "requested_z_mm": round(z, 3),
        "layer": p.layer.index + 1,  # 1-based, as slicers show it
        "layer_z_mm": round(p.layer.z, 3),
        "inserted": p.inserted,
    }


def add_thumbnails(
    gcode: Path, oriented: OrientedSection, sizes: list[tuple[int, int]], color: str
) -> list[str]:
    thumbs = th.render_thumbnails(oriented.mesh, sizes, color)
    gcode.write_text(th.inject_thumbnails(gcode.read_text(), thumbs))
    return [f"{t.width}x{t.height}/PNG" for t in thumbs]


def verify_dict(report: VerifyReport) -> dict[str, Any]:
    out: dict[str, Any] = {"note": report.note, "references": []}
    for d in report.diffs:
        out["references"].append(
            {
                "reference": Path(d.reference).name,
                "clean": d.clean,
                "changed_keys": len(d.changed),
                "only_ours": len(d.only_ours),
                "only_reference": len(d.only_reference),
                "start_macros": d.start_macros,
                "end_macros": d.end_macros,
                "thumbnails": d.thumbnails,
                "changed": {k: list(v) for k, v in d.changed.items()},
            }
        )
    return out


def printable_parts(cfg: LureConfig, report: dict, parts: list[str] | None = None) -> list[str]:
    """Parts this build prints: head, tail (print builds) and the separate printed parts
    (the fin) of any build; `parts` narrows them."""
    geometry = report.get("geometry") or {}
    extra = [p for p in PRINTED_PARTS if geometry.get(p)]
    if cfg.process.method == "print":
        names = [n for n in SECTIONS if n in report["sections"]] or list(report["sections"])
        available = names + extra
    else:
        available = extra
        if not available:
            raise PrintJobError(
                f"this is a {cfg.process.method} build with no printed parts; use --format "
                f"{cfg.process.method}"
            )
    if parts:
        unknown = [p for p in parts if p not in available]
        if unknown:
            raise PrintJobError(
                f"cannot print {', '.join(unknown)} from this {cfg.process.method} build "
                f"(printable: {', '.join(available)})"
            )
        return [p for p in available if p in parts]
    return available


def run(
    build: Path,
    spec: Path,
    machine_name: str | None = None,
    app_key: str | None = None,
    fetch: bool = True,
    parts: list[str] | None = None,
) -> PrintResult:
    build = Path(build)
    cfg: LureConfig = load_resolved(build / "resolved.toml")
    report = json.loads((build / "report.json").read_text())
    if report.get("status") == "config_only" or not report.get("sections"):
        raise PrintJobError(f"{build} has no model (status {report.get('status')!r})")
    names = printable_parts(cfg, report, parts)

    emit("plan", msg="filament slots and supports")
    machine: PrinterMachine = load_machine(
        spec / "printers" / (machine_name or default_machine(spec))
    )
    app = machine.slicer(app_key)
    roundover = ((report.get("envelope") or {}).get("roundover") or {}).get("applied_mm")
    plans = {name: plan_job(cfg, machine, roundover, app.key, part=name) for name in names}
    plan = plans[names[0]]
    warnings = list(dict.fromkeys(w for p in plans.values() for w in p.warnings))
    profile = json.loads(machine.profile_paths(app.key)["machine"].read_text())
    center = bed_center(profile)
    sizes = th.parse_sizes(profile.get("thumbnails"))

    out = build / OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    results: list[SectionResult] = []
    for i, name in enumerate(names):
        plan = plans[name]
        orientation = "flat" if name in FLAT_PARTS else "upright"
        emit("orient", i, len(names), f"{name}: {orientation}")
        stl = out / f"{name}.stl"
        oriented = orient_section(
            name, build / "model" / f"{name}.stl", center, stl, orientation=orientation
        )
        if oriented.shells < 2 and pause_height(report, name) is not None:
            warnings.append(
                f"{name}: expected a sealed cavity (2 shells), mesh has {oriented.shells}"
            )
        res = SectionResult(
            name=name,
            gcode=None,
            stl=f"{OUT_DIR}/{stl.name}",
            ok=False,
            orient=oriented.as_dict(),
            slice_log=f"{OUT_DIR}/{name}.slice.log",
            material=part_material(cfg, name),
            tool=plan.body.tool,
        )
        results.append(res)

        emit("slice", i, len(names), f"{name}: {app.label} {app.version}")
        sliced = orca.slice_stl(
            machine,
            stl,
            out,
            app_key=app.key,
            extra_overrides=plan.overrides,
            replace_overrides=True,
            filaments=plan.filaments,
            filament_ids=plan.filament_ids,
            fetch=fetch,
        )
        if not sliced.ok:
            res.issues.append(f"slicer failed (exit {sliced.returncode}); see {res.slice_log}")
            continue
        gcode = sliced.gcode
        res.gcode = f"{OUT_DIR}/{gcode.name}"

        emit("post", i, len(names), f"{name}: pause and thumbnails")
        ceiling = pause_height(report, name)
        if cfg.print.pause == "cavity_ceiling" and name not in FLAT_PARTS:
            if ceiling is None:
                warnings.append(f"{name}: no cavity ceiling in report.json; no pause inserted")
            else:
                try:
                    res.pause = insert_pause(gcode, oriented, ceiling, machine.pause_command)
                except gcode_post.PauseError as exc:
                    res.issues.append(f"pause: {exc}")
        res.thumbnails = add_thumbnails(gcode, oriented, sizes, plan.body.color)

        emit("verify", i, len(names), f"{name}: parse and reference diff")
        ver = verify_against_references(gcode, machine.root)
        res.stats = section_stats(ver.summary, plan)
        res.verify = verify_dict(ver)
        res.issues += _check(res, plan, sizes)
        res.ok = not res.issues

    emit("report", msg="print.json and print.md")
    summary = _summary(cfg, machine, app, plans[names[0]], results)
    if len({id(p) for p in plans.values()}) > 1:
        summary["part_plans"] = {n: p.as_dict() for n, p in plans.items()}
    payload = {
        "variant": cfg.name,
        "build": build.name,
        **summary,
        "sections": {r.name: r.as_dict() for r in results},
        "warnings": warnings,
    }
    (out / "print.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    (out / "print.md").write_text(render_md(payload))
    return PrintResult(out, results, summary, warnings)


def _check(res: SectionResult, plan: JobPlan, sizes: list[tuple[int, int]]) -> list[str]:
    """Things that make a G-code unusable: missing stats, pause, thumbnails, wrong tools."""
    issues = []
    st = res.stats
    if not st.get("layers"):
        issues.append("no layers parsed")
    if st.get("time_s") is None or st.get("filament_g") is None:
        issues.append("missing time or filament statistics")
    used = {int(t[1:]) for t in st.get("tools_used", [])}
    unexpected = used - set(plan.slots)
    if unexpected:
        issues.append(f"G-code uses unplanned tools {sorted(unexpected)} (planned {plan.slots})")
    if plan.multi_filament and not st.get("tool_changes"):
        issues.append("multi-filament job without tool changes (interface not printed?)")
    want = {f"{w}x{h}/PNG" for w, h in sizes}
    if not want <= set(res.thumbnails):
        issues.append(f"thumbnails missing: {sorted(want - set(res.thumbnails))}")
    return issues


def _summary(cfg, machine, app, plan: JobPlan, results: list[SectionResult]) -> dict[str, Any]:
    def total(key: str) -> float | None:
        vals = [r.stats.get(key) for r in results]
        if not vals or any(v is None for v in vals):
            return None
        return round(sum(vals), 3)

    return {
        "machine": machine.name,
        "slicer": f"{app.label} {app.version}",
        "orientation": "upright",
        "plan": plan.as_dict(),
        "totals": {
            "time_s": total("time_s"),
            "filament_g": total("filament_g"),
            "body_g": total("body_g"),
            "interface_g": total("interface_g"),
            "tool_changes": total("tool_changes"),
        },
    }


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _g(v: Any) -> str:
    return "?" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.2f} g"


def render_md(p: dict[str, Any]) -> str:
    plan = p["plan"]
    body = plan["body"]
    iface = plan["interface"]
    lines = [
        f"# Print: {p['variant']}",
        "",
        f"{p['machine']}, {p['slicer']}, sections {p['orientation']} (z up, belly down)"
        + (
            "; the fin flat on its side."
            if any(s.get("orient", {}).get("orientation") == "flat" for s in p["sections"].values())
            else "."
        ),
        "",
        f"- Body: {body['tool']} {body['material']}",
        f"- Supports: {'yes' if plan['supports'] else 'no'} ({plan['supports_reason']})",
        f"- Interface: {iface['tool'] + ' ' + iface['material'] if iface else 'none'}; "
        f"prime tower {'on' if plan['prime_tower'] else 'off'}",
        "",
        "| Part | Material | Time | Total | Body | Interface | Tool changes | Layers | Pause |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in p["sections"].items():
        st = s["stats"]
        pause = s.get("pause")
        pause_txt = f"layer {pause['layer']} (Z {pause['layer_z_mm']:.2f})" if pause else "none"
        mat = f"{s.get('tool') or ''} {s.get('material') or ''}".strip() or "?"
        lines.append(
            f"| {name} | {mat} | {fmt_time(st.get('time_s'))} | {_g(st.get('filament_g'))} | "
            f"{_g(st.get('body_g'))} | {_g(st.get('interface_g'))} | "
            f"{st.get('tool_changes', '?')} | {st.get('layers', '?')} | {pause_txt} |"
        )
    t = p["totals"]
    lines.append(
        f"| **total** | | {fmt_time(t['time_s'])} | {_g(t['filament_g'])} | {_g(t['body_g'])} | "
        f"{_g(t['interface_g'])} | {t['tool_changes'] if t['tool_changes'] is not None else '?'}"
        " | | |"
    )
    lines.append("")
    for name, pp in (p.get("part_plans") or {}).items():
        if pp["body"]["material"] != body["material"]:
            lines.append(
                f"- {name}: {pp['body']['tool']} {pp['body']['material']}, supports "
                f"{'yes' if pp['supports'] else 'no'} ({pp['supports_reason']}); overrides "
                + ", ".join(f"`{k}={v}`" for k, v in pp["overrides"].items())
            )
    lines.append("")
    lines.append(
        "The pause (`{}`) stops before the layer that closes the cavity roof "
        "(cavity ceiling from report.json, moved into print coordinates).".format(
            next((s["pause"]["command"] for s in p["sections"].values() if s.get("pause")), "M600")
        )
    )
    lines.append("")
    for name, s in p["sections"].items():
        v = s.get("verify") or {}
        if v.get("note"):
            lines.append(f"- {name}: {v['note']}")
        for ref in v.get("references", []):
            if ref["clean"]:
                lines.append(f"- {name} vs {ref['reference']}: clean")
                continue
            lines.append(
                f"- {name} vs {ref['reference']}: {ref['changed_keys']} shared config keys "
                f"differ, {ref['only_ours']} only ours, {ref['only_reference']} only in the "
                "reference"
            )
            for key, (ours, theirs) in ref["changed"].items():
                lines.append(f"  - `{key}`: `{ours}` (reference `{theirs}`)")
            for what in ("start_macros", "end_macros", "thumbnails"):
                if ref[what]:
                    ours, theirs = ref[what]
                    extra = [m for m in ours if m not in theirs]
                    missing = [m for m in theirs if m not in ours]
                    lines.append(f"  - {what}: extra {extra}, missing {missing}")
        for issue in s.get("issues", []):
            lines.append(f"- {name}: **{issue}**")
    for w in p.get("warnings", []):
        lines.append(f"- warning: {w}")
    return "\n".join(lines) + "\n"
