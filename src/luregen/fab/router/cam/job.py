"""`lure fab --format cnc`: a CNC build folder → verified G-code for both setups.

Reads only the build folder (resolved.toml, report.json, model/<section>.stl) plus the
router's spec folder (post, machine, tools, feeds). Writes `<build>/cnc/`:

- `<setup>_<nn>_<tool>.nc` per tool (default, `output.split_by_tool`), else `<setup>.nc`
- `halves/<section>_<side>.stl`: the centerplane halves, in lure coordinates
- `cnc.json`: layout, files in run order, tools, feeds (with chipload status), runtimes,
  verify results and warnings
- `setup.md`: a first setup sheet (#43 extends it)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from luregen import progress
from luregen.config import LureConfig, load_resolved
from luregen.fab.router import feeds as fd
from luregen.fab.router.cam import setups as st
from luregen.fab.router.cam.ops2d import Cut
from luregen.fab.router.ir import Operation, Program
from luregen.fab.router.post import post_program
from luregen.fab.router.post_config import (
    MachineConfig,
    PostConfig,
    ToolLibrary,
    load_machine,
    load_post,
    load_tools,
    router_dir,
)
from luregen.fab.router.verify import verify

OUT_DIR = "cnc"
OP_KINDS = ("pocket", "groove", "drill", "profile", "face")


class CncJobError(RuntimeError):
    pass


@dataclass
class NcFile:
    path: str  # build-relative
    setup: str
    tools: list[str]
    operations: list[str]
    runtime_s: float
    ok: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class CncResult:
    out_dir: Path
    files: list[NcFile]
    summary: dict
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.files)


def default_machine(spec: Path) -> str:
    folders = sorted(p.name for p in (spec / "routers").iterdir() if p.is_dir())
    if len(folders) != 1:
        raise CncJobError(f"choose a router with --machine ({', '.join(folders)})")
    return folders[0]


def make_cut(
    kind: str,
    cfg: LureConfig,
    lib: ToolLibrary,
    table: fd.FeedsTable,
    safe_z: float,
) -> Cut:
    op = getattr(cfg.cnc.ops, kind)
    if op.tool not in lib.tools:
        raise CncJobError(f"cnc.ops.{kind}.tool {op.tool!r} is not in tools.toml")
    entry = lib.tools[op.tool]
    tool = lib.get(op.tool)
    flutes = entry.flutes or 2
    material = cfg.process.material
    if op.preset:
        p = table.presets.get(op.preset)
        if p is not None and abs(p.tool_diameter_in * fd.IN - tool.diameter) > 0.05:
            raise CncJobError(f"preset {op.preset!r} is for a different tool than {op.tool!r}")
        feeds = fd.preset(table, op.preset, material, flutes, cfg.cnc.rpm)
    else:
        feeds = fd.calculate(
            table, tool.diameter, material, op.mode, flutes, cfg.cnc.rpm, tool.shape
        )
    return Cut(
        tool=tool,
        feeds=feeds,
        safe_z=safe_z,
        ramp_angle_deg=cfg.cnc.ramp_angle_deg,
        climb=cfg.cnc.climb,
    )


def load_halves(build: Path, sections: list[str]) -> list[st.Half]:
    import trimesh

    from luregen.geometry import cnc_halves as ch

    halves = []
    for name in sections:
        path = build / "model" / f"{name}.stl"
        if not path.is_file():
            raise CncJobError(f"missing {path}: run `lure build` first")
        right, left = ch.split_halves(trimesh.load(path, force="mesh"))
        sil = ch.silhouette_xz(right)
        for side, mesh in (("right", right), ("left", left)):
            halves.append(st.Half(name, side, sil, ch.max_half_width(mesh), mesh))
    return halves


def glue_features(cfg: LureConfig, report: dict, halves: list[st.Half], warnings: list[str]):
    from luregen.geometry.cnc_halves import rounded_rect

    geo = report.get("geometry", {})
    features: list[st.GlueFeature] = []
    pockets = []
    gp = geo.get("ballast", {}).get("glue_face_pocket")
    if gp:
        pockets.append(("head", "ballast", gp))
    for p in geo.get("cnc_air_pockets") or []:
        pockets.append((p["kind"].split(":")[1], "air", p))
    for section, name, p in pockets:
        region = rounded_rect(p["x"], p["z"], p["length"], p["height"], p["corner_r"])
        features.append(st.GlueFeature(section, "pocket", name, region, p["depth"]))
    if cfg.process.rigging == "through_wire":
        w = cfg.cnc.wire
        z = w.z_mm if w.z_mm is not None else (cfg.joint.line_tie_z if cfg.joint else 0.0)
        depth = st.groove_depth(w.diameter_mm, w.clearance_mm)
        sil = {h.section: h.silhouette for h in halves}
        for section, outline in sil.items():
            paths = st.wire_paths(outline, z, w.exit_mm)
            if not paths:
                warnings.append(f"{section}: wire height z = {z} misses the outline")
            for path in paths:
                for f in features:
                    if f.section == section and f.kind == "pocket" and f.geom.distance(path) < 1:
                        warnings.append(f"{section}: wire groove runs into the {f.name} pocket")
                features.append(st.GlueFeature(section, "groove", "wire", path, depth))
    return features


def split_by_tool(program: Program) -> list[tuple[str, Program]]:
    """Consecutive runs of one tool → (tool id, sub-program), in run order."""
    runs: list[list[Operation]] = []
    for op in program.operations:
        if runs and runs[-1][-1].tool == op.tool:
            runs[-1].append(op)
        else:
            runs.append([op])
    out = []
    for ops in runs:
        tool = ops[0].tool
        sub = program.model_copy(
            update={"tools": {tool: program.tools[tool]}, "operations": ops}, deep=True
        )
        out.append((tool, sub))
    return out


def write_programs(
    programs: list[Program],
    out: Path,
    rel_root: Path,
    post: PostConfig,
    machine: MachineConfig,
    below_stock_mm: float,
) -> list[NcFile]:
    files = []
    for program in programs:
        setup = program.setup.name
        if not program.operations:
            continue
        if post.output.split_by_tool:
            parts = [
                (f"{setup}_{i + 1:02d}_{tool}.nc", sub)
                for i, (tool, sub) in enumerate(split_by_tool(program))
            ]
        else:
            parts = [(f"{setup}.nc", program)]
        for name, sub in parts:
            text = post_program(sub, post, machine)
            path = out / name
            path.write_text(text)
            rep = verify(text, post, machine, safe_z=None, below_stock_mm=below_stock_mm)
            files.append(
                NcFile(
                    path=path.relative_to(rel_root).as_posix(),
                    setup=setup,
                    tools=list(dict.fromkeys(op.tool for op in sub.operations)),
                    operations=[op.name for op in sub.operations],
                    runtime_s=round(rep.runtime_s, 1),
                    ok=rep.ok,
                    issues=[str(i) for i in rep.issues],
                )
            )
    return files


def run(build: Path, spec: Path, machine_name: str | None = None) -> CncResult:
    """Plan, post and verify the CNC programs for a build folder."""
    cfg = load_resolved(build / "resolved.toml")
    if cfg.process.method != "cnc":
        raise CncJobError(
            f"this build is process.method = {cfg.process.method!r}; CNC needs a cnc build "
            "(e.g. `lure build <variant> --set process.method=cnc --set process.material=basswood`)"
        )
    report = json.loads((build / "report.json").read_text())
    if report.get("status") != "built":
        raise CncJobError(f"build status is {report.get('status')!r}, not 'built'")
    machine_name = machine_name or default_machine(spec)
    rdir = router_dir(spec, machine_name)
    post, machine = load_post(rdir / "post.toml"), load_machine(rdir / "machine.toml")
    lib = load_tools(rdir / "tools.toml")
    table = fd.load_feeds(rdir / "feeds.toml")
    warnings: list[str] = []

    progress.emit("cnc", 0.05, "halves")
    sections = [s for s in ("head", "tail") if s in report.get("sections", {})] or ["body"]
    halves = load_halves(build, sections)
    out = build / OUT_DIR
    (out / "halves").mkdir(parents=True, exist_ok=True)
    for h in halves:
        h.mesh.export(out / "halves" / f"{h.section}_{h.side}.stl")

    progress.emit("cnc", 0.2, "layout")
    c = cfg.cnc
    hw = max(h.half_width for h in halves)
    need = c.stock.face_mm + hw
    thickness = c.stock.thickness_mm or math.ceil((need + c.stock.top_allowance_mm) * 10) / 10
    if thickness < need:
        raise CncJobError(f"stock {thickness} mm is thinner than face + half width {need:.2f} mm")
    reg = c.registration
    layout = st.plan_layout(
        halves,
        c.stock.margin_mm,
        c.stock.spacing_mm,
        thickness,
        c.stock.face_mm,
        reg.pin_d_mm if reg.enabled else 0.0,
        reg.into_spoilboard_mm,
    )
    safe_z = 5.0
    cuts = {k: make_cut(k, cfg, lib, table, safe_z) for k in OP_KINDS}
    ctx = st.OpCtx(cuts, c.tabs.count, c.tabs.width_mm, c.tabs.height_mm, c.cutout_overcut_mm)
    features = glue_features(cfg, report, halves, warnings)

    progress.emit("cnc", 0.35, "toolpaths")
    programs = st.plan(cfg.name, layout, features, ctx, safe_z)
    progress.emit("cnc", 0.7, "post and verify")
    for old in out.glob("*.nc"):
        old.unlink()
    below = max(c.cutout_overcut_mm, reg.into_spoilboard_mm if reg.enabled else 0.0) + 0.05
    files = write_programs(programs, out, build, post, machine, below)
    warnings += dfm_warnings(cfg, lib, layout, cuts)
    summary = summarize(cfg, machine_name, layout, cuts, features, files, post, warnings)
    (out / "cnc.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "setup.md").write_text(setup_sheet(summary))
    progress.emit("cnc", 1.0, "done")
    return CncResult(out, files, summary, warnings)


def dfm_warnings(cfg: LureConfig, lib: ToolLibrary, layout: st.Layout, cuts: dict) -> list[str]:
    out = []
    limit = cfg.dfm.cnc.max_pocket_depth_x_dia
    cut_depth = layout.thickness + cfg.cnc.cutout_overcut_mm
    d = cuts["profile"].tool.diameter
    if cut_depth > limit * d:
        out.append(
            f"cut-out depth {cut_depth:.1f} mm > {limit:g} × {d:g} mm tool "
            "(dfm.cnc.max_pocket_depth_x_dia): check the flute length"
        )
    for kind, cut in cuts.items():
        if kind == "face" and layout.face <= 0:
            continue
        entry = lib.tools[cut.tool.id]
        if not entry.number_verified:
            out.append(f"tool {cut.tool.id} ({kind}): tool number T{entry.number} unverified")
        if cut.feeds.status in ("below", "above"):
            out.append(
                f"{kind}: chipload {cut.feeds.chipload_in:.4f} in is {cut.feeds.status} the "
                f"{cut.feeds.material} range at {cut.feeds.rpm:g} rpm"
            )
    return list(dict.fromkeys(out))


def summarize(cfg, machine_name, layout, cuts, features, files, post, warnings) -> dict:
    used = {t for f in files for t in f.tools}
    feeds = {
        kind: {"tool": cut.tool.id, **cut.feeds.as_dict()}
        for kind, cut in cuts.items()
        if cut.tool.id in used and (kind != "face" or layout.face > 0)
    }
    return {
        "variant": cfg.name,
        "machine": machine_name,
        "split_by_tool": post.output.split_by_tool,
        "material": cfg.process.material,
        "stock_mm": {
            "length": round(layout.length, 2),
            "width": round(layout.width, 2),
            "thickness": round(layout.thickness, 2),
            "face": layout.face,
        },
        "zero": "front-left corner of the stock top (both setups)",
        "flip": "turn over about X onto the pins: (X, Y, Z) -> (X, W - Y, -T - Z)",
        "pins": {
            "diameter_mm": layout.pin_d,
            "xy_setup1": [[round(x, 3), round(y, 3)] for x, y in layout.pins],
            "into_spoilboard_mm": layout.pin_depth,
        },
        "halves": [
            {
                "section": h.section,
                "side": h.side,
                "offset_mm": [round(h.ox, 3), round(h.oy, 3)],
                "half_width_mm": round(h.half_width, 3),
            }
            for h in layout.halves
        ],
        "glue_features": [
            {"section": f.section, "kind": f.kind, "name": f.name, "depth_mm": round(f.depth, 3)}
            for f in features
        ],
        "files": [vars(f) for f in files],
        "runtime_s": round(sum(f.runtime_s for f in files), 1),
        "feeds": feeds,
        "warnings": warnings,
        "ok": all(f.ok for f in files),
    }


def _hms(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}"


def setup_sheet(summary: dict) -> str:
    s = summary["stock_mm"]
    lines = [
        f"# CNC setup sheet: {summary['variant']}",
        "",
        f"Machine: {summary['machine']}. Material: {summary['material']}.",
        f"Stock: {s['length']} × {s['width']} × {s['thickness']} mm (X × Y × Z); "
        f"zero: {summary['zero']}.",
        "",
        "## Run order",
        "",
        "| # | File | Setup | Tool | Est. time | Verify |",
        "|---|---|---|---|---|---|",
    ]
    for i, f in enumerate(summary["files"], 1):
        ok = "ok" if f["ok"] else f"{len(f['issues'])} issue(s)"
        lines.append(
            f"| {i} | `{f['path']}` | {f['setup']} | {', '.join(f['tools'])} | "
            f"{_hms(f['runtime_s'])} | {ok} |"
        )
    lines += [
        "",
        f"Total estimated runtime: {_hms(summary['runtime_s'])}.",
        "",
        "## Setups",
        "",
        "1. **Setup 1, glue face up.** Clamp the blank, zero XY on the front-left corner and Z "
        "on the top. Cut the glue-face pockets, the flip-pin holes (through, into the "
        "spoilboard) and the wire groove.",
        f"2. **Flip.** Put pins of ⌀{summary['pins']['diameter_mm']} mm in the holes and turn "
        "the blank over about X onto them, (X, Y, Z) → (X, W − Y, −T − Z). Keep the XY zero "
        "on the (new) front-left corner and re-zero Z on the new top.",
        "3. **Setup 2, outer face up.** Cut out the halves (tabs hold them).",
        "4. **Glue-up.** Set the ballast in its pocket and the wire in its groove, then glue "
        "the matching right and left halves.",
        "",
        "## Feeds",
        "",
        "| Op | Tool | rpm | Feed mm/min (IPM) | Plunge mm/min | DOC mm | Stepover mm | "
        "Chipload in | Status |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for kind, f in summary["feeds"].items():
        src = f["preset"] or f["mode"]
        cl = "n/a" if f["chipload_in"] is None else f"{f['chipload_in']:.4f}"
        lines.append(
            f"| {kind} ({src}) | {f['tool']} | {f['rpm']:g} | {f['feed_mm_min']} "
            f"({f['feed_ipm']}) | {f['plunge_mm_min']} | {f['doc_mm']} | {f['stepover_mm']} | "
            f"{cl} | {f['chipload_status']} |"
        )
    if summary["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in summary["warnings"]]
    return "\n".join(lines) + "\n"
