"""Balance report: per-section and whole-lure mass properties, verdict and CG/CB diagram.

The verdict only compares the built lure with the targets; solving the internals to meet
them is F4. Every check is evaluated at both ends of the hardware mass band.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from luregen.config import LureConfig
from luregen.physics.massprops import BANDS, Band, LureProps
from luregen.profile.table import StationTable
from luregen.units import g_to_oz

RATIO_TOL = 0.10  # head:tail mass ratio counts as on target within ±10 %
EDGE_BANDS: tuple[Band, ...] = ("min", "max")
DIAGRAM_PATH = "cg_cb.png"
HEAD_COLOR, TAIL_COLOR = "#2a78d6", "#eb6834"  # categorical slots 1 and 2
INK, MUTED = "#2b2b29", "#8a8984"


def cb_above_cg_target(cfg: LureConfig, table: StationTable) -> float:
    """Self-righting target: CB above CG by `cb_above_cg_frac` of the max body depth."""
    return cfg.buoyancy.cb_above_cg_frac * 2 * float(table.v_radius.max())


def _check(cid: str, label: str, values: dict[str, float], target: Any, ok: dict, unit: str):
    return {
        "id": cid,
        "label": label,
        "target": target,
        "unit": unit,
        "values": {b: round(v, 4) for b, v in values.items()},
        "ok": all(ok.values()),
    }


def verdict(cfg: LureConfig, table: StationTable, props: dict[Band, LureProps]) -> dict:
    """Pass/fail of each balance target at both hardware band ends."""
    b = cfg.buoyancy
    sink = list(b.sink_bands[b.target_sink])
    tol = b.level_tolerance_mm
    righting = cb_above_cg_target(cfg, table)
    lo_r, hi_r = b.head_tail_mass_ratio * (1 - RATIO_TOL), b.head_tail_mass_ratio * (1 + RATIO_TOL)

    def inside(v: float, band: list[float] | tuple[float, float]) -> bool:
        return band[0] <= v <= band[1]

    def per(fn) -> dict[str, float]:
        return {e: fn(props[e]) for e in EDGE_BANDS}

    density = per(lambda p: p.lure.density)
    level = per(lambda p: p.lure.cg_minus_cb_x)
    upright = per(lambda p: p.lure.cb_minus_cg_z)
    head_d = per(lambda p: p.sections["head"].density)
    tail_d = per(lambda p: p.sections["tail"].density)
    ratio = per(lambda p: p.ratio())
    checks = [
        _check(
            "sink",
            f"Overall density ({b.target_sink}, {b.water} water)",
            density,
            sink,
            {k: inside(v, sink) for k, v in density.items()},
            "g/cm³",
        ),
        _check(
            "level",
            "Level hang: CG − CB fore/aft",
            level,
            [-tol, tol],
            {k: abs(v) <= tol for k, v in level.items()},
            "mm",
        ),
        _check(
            "self_righting",
            "Self-righting: CB above CG",
            upright,
            [round(righting, 3), None],
            {k: v >= righting for k, v in upright.items()},
            "mm",
        ),
        _check(
            "head_density",
            "Head density",
            head_d,
            list(b.head_density),
            {k: inside(v, b.head_density) for k, v in head_d.items()},
            "g/cm³",
        ),
        _check(
            "tail_density",
            "Tail density",
            tail_d,
            list(b.tail_density),
            {k: inside(v, b.tail_density) for k, v in tail_d.items()},
            "g/cm³",
        ),
        _check(
            "head_tail_ratio",
            f"Head:tail mass ratio (±{RATIO_TOL:.0%})",
            ratio,
            [round(lo_r, 3), round(hi_r, 3)],
            {k: lo_r <= v <= hi_r for k, v in ratio.items()},
            "",
        ),
    ]
    failed = [c["id"] for c in checks if not c["ok"]]
    status = "pass" if not failed else "fail"
    label = (
        "meets all balance targets at both hardware band ends"
        if not failed
        else f"{len(failed)} of {len(checks)} targets missed ({', '.join(failed)}); "
        "the F4 solver tunes the internals"
    )
    return {"status": status, "label": label, "failed": failed, "checks": checks}


def balance_blocks(
    cfg: LureConfig, table: StationTable, props: dict[Band, LureProps]
) -> dict[str, Any]:
    """`sections`, `lure`, `verdict` and `summary` report blocks (nominal = band mid)."""
    mid = props["mid"].as_dict()
    sections = {}
    for name, sec in mid["sections"].items():
        sections[name] = {
            **sec,
            "density_g_cm3": sec["density"],
            "band": {b: props[b].as_dict()["sections"][name] for b in BANDS},
        }
    lure = {
        **mid["lure"],
        "density_g_cm3": mid["lure"]["density"],
        "cb_above_cg_target_mm": round(cb_above_cg_target(cfg, table), 3),
        "band": {b: props[b].as_dict()["lure"] for b in BANDS},
    }
    v = verdict(cfg, table, props)
    summary = {
        "mass_g": lure["mass_g"],
        "mass_oz": lure["mass_oz"],
        "density_g_cm3": lure["density"],
        "cg_mm": lure["cg_mm"],
        "cb_mm": lure["cb_mm"],
        "verdict": {"status": v["status"], "label": v["label"]},
    }
    return {"sections": sections, "lure": lure, "verdict": v, "summary": summary}


# --- markdown -------------------------------------------------------------------------


def balance_md(report: dict) -> list[str]:
    lines: list[str] = []
    if sections := report.get("sections"):
        lines += [
            "## Sections (hardware band mid)",
            "",
            "| Section | Mass (g) | Mass (oz) | Displacement (cm³) | Density (g/cm³) "
            "| CG x, z (mm) | CB x, z (mm) |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, s in sections.items():
            lines.append(
                f"| {name} | {s['mass_g']:.2f} | {s['mass_oz']:.3f} | {s['volume_cm3']:.2f} "
                f"| {s['density']:.3f} | {s['cg_mm'][0]:.2f}, {s['cg_mm'][2]:.2f} "
                f"| {s['cb_mm'][0]:.2f}, {s['cb_mm'][2]:.2f} |"
            )
    if lure := report.get("lure"):
        lines += [
            "",
            "## Whole lure",
            "",
            "| Band | Mass (g) | Mass (oz) | Density (g/cm³) | CG − CB fore/aft (mm) "
            "| CB − CG vertical (mm) |",
            "|---|---|---|---|---|---|",
        ]
        for band, p in lure["band"].items():
            lines.append(
                f"| {band} | {p['mass_g']:.2f} | {g_to_oz(p['mass_g']):.3f} | {p['density']:.4f} "
                f"| {p['cg_minus_cb_x_mm']:+.2f} | {p['cb_minus_cg_z_mm']:+.2f} |"
            )
        if "head_tail_mass_ratio" in lure:
            lines.append("")
            lines.append(
                f"Head:tail mass ratio {lure['head_tail_mass_ratio']:.2f}; self-righting target "
                f"CB − CG ≥ {lure['cb_above_cg_target_mm']:.2f} mm."
            )
    if v := report.get("verdict"):
        lines += [
            "",
            f"## Verdict: {v['status'].upper()}",
            "",
            v["label"],
            "",
            "| Check | min band | max band | Target | OK |",
            "|---|---|---|---|---|",
        ]
        for c in v["checks"]:
            tgt = c["target"]
            if isinstance(tgt, list):
                tgt = f"≥ {tgt[0]}" if tgt[1] is None else f"{tgt[0]} – {tgt[1]}"
            lines.append(
                f"| {c['label']} | {c['values']['min']:.3f} | {c['values']['max']:.3f} "
                f"| {tgt} {c['unit']} | {'yes' if c['ok'] else 'no'} |"
            )
    if geo := report.get("geometry"):
        j = geo["joint"]
        lines += [
            "",
            "## Joint and internals",
            "",
            f"- V-joint at x = {j['x_joint_mm']:.1f} mm, gap {j['gap_mm']} mm, arms "
            f"{j['angle_deg']}° from lateral; pin at x = {j['pin_x_mm']:.1f} mm "
            f"(bore {j['pin_bore_mm']} mm); gap wedge {j['gap_volume_cm3']:.2f} cm³",
        ]
        b = geo["ballast"]
        lines.append(
            f"- Ballast: {b['mass_g']:.2f} g {b['material']} at x = {b['x_mm']:.1f}, "
            f"z = {b['z_axis_mm']:.1f} mm ({b['volume_cm3']:.3f} cm³"
            + (", pinned" if b["pinned"] else ", seed value until F4")
            + ")"
        )
        if t := geo.get("trim_pocket"):
            lines.append(
                f"- Trim pocket: x = {t['x_mm']:.1f} mm, {t['volume_cm3']:.3f} cm³ "
                f"(holds {t['capacity_g']:.2f} g {t['material']}), empty"
            )
        for name, s in (geo.get("internals") or {}).items():
            lines.append(
                f"- {name} cavity: {s['cavity_volume_cm3']:.2f} cm³, wall {s['wall_thk_mm']} mm, "
                f"keel top z = {s['keel_top_z_mm']} mm, {s['roof']} roof, ceiling z = "
                f"{s['cavity_ceiling_z_mm']:.1f} mm, min skin {s['min_skin_mm']:.2f} mm"
            )
    if report.get("diagram"):
        lines += ["", f"![CG/CB side view]({report['diagram']})"]
    return lines


# --- diagram --------------------------------------------------------------------------


def render_diagram(path: Path, diagram: dict, props: dict[Band, LureProps], title: str) -> Path:
    """Side view (x–z cut at y = 0) with sections, cavities, hardware, CG and CB."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MPath

    colors = {"head": HEAD_COLOR, "tail": TAIL_COLOR}
    fig, ax = plt.subplots(figsize=(10, 4), dpi=150)
    for name, loops in diagram["slices"].items():
        verts, codes = [], []
        for loop in loops:
            verts += [*loop, loop[0]]
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(loop) - 1) + [MPath.CLOSEPOLY]
        if verts:
            c = colors.get(name, MUTED)
            patch = PathPatch(MPath(verts, codes), facecolor=c, alpha=0.22, edgecolor=c, lw=1.2)
            ax.add_patch(patch)
    for loops in diagram.get("fills", {}).values():
        for loop in loops:
            ax.fill(*zip(*loop, strict=True), color=MUTED, alpha=0.9, lw=0)
    ax.axvline(diagram["x_joint"], color=MUTED, lw=0.8, ls="--")
    for hw in diagram["hardware"]:
        ax.plot(hw["x"], hw["z"], "o", ms=4, color=INK, mfc="white", mew=1)
    mid = props["mid"]
    for name, p in mid.sections.items():
        c = colors.get(name, MUTED)
        ax.plot(p.cg[0], p.cg[2], "x", ms=9, mew=2, color=c)
        ax.plot(p.cb[0], p.cb[2], "+", ms=11, mew=2, color=c)
    lure = mid.lure
    ax.plot(lure.cg[0], lure.cg[2], "X", ms=10, color=INK, label="CG (×)")
    ax.plot(lure.cb[0], lure.cb[2], "P", ms=10, color=INK, mfc="white", label="CB (+)")
    handles = [
        plt.Line2D([], [], color=HEAD_COLOR, lw=6, alpha=0.4, label="head"),
        plt.Line2D([], [], color=TAIL_COLOR, lw=6, alpha=0.4, label="tail"),
        plt.Line2D([], [], ls="", marker="x", color=INK, mew=2, label="CG"),
        plt.Line2D([], [], ls="", marker="+", color=INK, ms=10, mew=2, label="CB"),
        plt.Line2D([], [], ls="", marker="o", color=INK, mfc="white", label="hardware"),
        plt.Line2D([], [], color=MUTED, lw=6, label="ballast"),
    ]
    ax.legend(handles=handles, loc="upper right", fontsize=8, frameon=False, ncols=6)
    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.set_xlabel("x from nose (mm)", color=INK)
    ax.set_ylabel("z (mm)", color=INK)
    ax.set_title(
        f"{title}\n{lure.mass_g:.1f} g, {lure.density:.3f} g/cm³, "
        f"CG−CB {lure.cg_minus_cb_x:+.1f} mm fore/aft, CB−CG {lure.cb_minus_cg_z:+.1f} mm "
        "(large markers: whole lure; small: per section)",
        fontsize=9,
        color=INK,
        loc="left",
    )
    ax.grid(color="#e4e3df", lw=0.5)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
    ax.tick_params(colors=MUTED, labelsize=8)
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo - 4, hi + 12)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path
