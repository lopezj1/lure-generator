"""`lure fab --format cnc`: a CNC build folder → verified G-code for both setups.

Reads only the build folder (resolved.toml, report.json, model/<section>.stl) plus the
router's spec folder (post, machine, tools, feeds). Writes `<build>/fab/cnc/`:

- `<setup>_<nn>_<tool>.nc` per tool (default, `output.split_by_tool`), else `<setup>.nc`
- `halves/<section>_<side>.stl`: the centerplane halves, in lure coordinates
- `cnc.json`: layout, files in run order, tools, feeds (with chipload status), runtimes,
  verify results, 3D op stats, the simulation result and warnings
- `sim_<setup>.png`: heightmap simulation of each setup vs the halves (gouge/leftover)
- `setup.md`: the setup sheet: stock, zero, files in run order with the tool to load, the
  flip, what goes in before glue-up and what's left after it, the simulation, feeds
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

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

OUT_DIR = "fab/cnc"  # web viewer and archive groups expect this path
OP_KINDS = ("pocket", "groove", "drill", "profile", "face", "rough3d", "finish3d")


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


def auto_thickness(need_mm: float, step_mm: float) -> float:
    """Smallest board of `step_mm` increments (e.g. 1/4in) at least `need_mm` thick."""
    if step_mm <= 0:
        return math.ceil(need_mm * 10) / 10
    # The tolerance keeps an exact fit (12.7 mm needed on a 6.35 step) from rounding up.
    return round(math.ceil(need_mm / step_mm - 1e-9) * step_mm, 4)


def make_cut(
    kind: str,
    cfg: LureConfig,
    lib: ToolLibrary,
    table: fd.FeedsTable,
    safe_z: float,
    rpm: float,
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
        feeds = fd.preset(table, op.preset, material, flutes, rpm)
    else:
        feeds = fd.calculate(table, tool.diameter, material, op.mode, flutes, rpm, tool.shape)
    if kind == "finish3d":
        if op.stepover_mm is not None:
            feeds = replace(feeds, stepover=op.stepover_mm)
        elif tool.shape == "ball":
            from luregen.fab.router.cam.ops3d import scallop_stepover

            feeds = replace(feeds, stepover=scallop_stepover(tool.diameter, op.scallop_mm))
    return Cut(
        tool=tool,
        feeds=feeds,
        safe_z=safe_z,
        ramp_angle_deg=cfg.cnc.ramp_angle_deg,
        climb=cfg.cnc.climb,
    )


def surface_settings(cfg: LureConfig, lib: ToolLibrary) -> st.Surface3D | None:
    """3D outer-face ops from `cnc.ops.rough3d` / `finish3d` (None when both are off)."""
    from luregen.fab.router.cam import ops3d
    from luregen.fab.router.cam.heightmap import Cutter

    r, f = cfg.cnc.ops.rough3d, cfg.cnc.ops.finish3d
    if not (r.enabled or f.enabled):
        return None

    def cutter(tool_id: str) -> Cutter:
        e = lib.tools[tool_id]
        return Cutter(e.shape, e.diameter, e.flute_length, e.shank_diameter)

    sf = st.Surface3D()
    if r.enabled:
        sf.rough = ops3d.Rough3D(
            allowance=r.allowance_mm,
            max_stair=r.max_stair_mm,
            res=r.resolution_mm,
            boundary=r.boundary_mm,
        )
        sf.cutters["rough3d"] = cutter(r.tool)
    if f.enabled:
        sf.finish = ops3d.Finish3D(res=f.resolution_mm, tolerance=f.tolerance_mm)
        sf.cutters["finish3d"] = cutter(f.tool)
    return sf


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
        glue = ch.glue_face_xz(right)
        outline = max(getattr(glue, "geoms", [glue]), key=lambda g: g.area)
        outline = type(outline)(outline.exterior)
        for side, mesh in (("right", right), ("left", left)):
            half = st.Half(name, side, sil, ch.max_half_width(mesh), mesh, glue_outline=outline)
            halves.append(half)
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
    thickness = c.stock.thickness_mm or auto_thickness(
        need + c.stock.top_allowance_mm, c.stock.thickness_step_mm
    )
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
    rpm = c.rpm or table.default_rpm(cfg.process.material, machine.spindle.dial_rpm)
    cuts = {k: make_cut(k, cfg, lib, table, safe_z, rpm) for k in OP_KINDS}
    ctx = st.OpCtx(cuts, c.tabs.count, c.tabs.width_mm, c.tabs.height_mm, c.cutout_overcut_mm)
    ctx.surface = surface_settings(cfg, lib)
    features = glue_features(cfg, report, halves, warnings)

    progress.emit("cnc", 0.35, "toolpaths")
    programs = st.plan(cfg.name, layout, features, ctx, safe_z)
    progress.emit("cnc", 0.7, "post and verify")
    for old in out.glob("*.nc"):
        old.unlink()
    below = max(c.cutout_overcut_mm, reg.into_spoilboard_mm if reg.enabled else 0.0) + 0.05
    files = write_programs(programs, out, build, post, machine, below)
    warnings += dfm_warnings(cfg, lib, layout, cuts)
    warnings += surface_warnings(ctx, lib, cfg)
    progress.emit("cnc", 0.85, "simulate")
    sims = simulate_setups(layout, ctx, features, files, build, out)
    summary = summarize(cfg, machine_name, layout, cuts, features, files, post, warnings)
    summary["surface3d"] = ctx.report.get("surface3d")
    summary["sim"] = sims
    summary["post_glue_up"] = post_glue_up_steps(report)
    summary["glue_up"] = glue_up_items(cfg, report)
    used = {t for f in files for t in f.tools}
    summary["tools"] = {
        cut.tool.id: {
            "name": cut.tool.name,
            "number": cut.tool.number,
            "diameter_mm": cut.tool.diameter,
            "shape": cut.tool.shape,
            "number_verified": lib.tools[cut.tool.id].number_verified,
        }
        for cut in cuts.values()
        if cut.tool.id in used
    }
    (out / "cnc.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out / "setup.md").write_text(setup_sheet(summary))
    progress.emit("cnc", 1.0, "done")
    return CncResult(out, files, summary, warnings)


SIM_RES = 0.15  # mm
SIM_TOL = 0.05  # mm
WALL_JUMP = 1.0  # mm across a 3×3 neighbourhood: a vertical wall, not a slope


def simulate_setups(layout, ctx, features, files: list[NcFile], build: Path, out: Path) -> dict:
    """Heightmap-simulate each setup's files in run order and diff against the halves.

    Gouge: material cut below a half's surface. Leftover: material above it, on the
    surface the 3D ops machine (setup 2: above the floor; setup 1: the glue face and its
    pockets). Setup 2's `rim` is the tab band under the floor, left for the cut-out's
    vertical wall (sanded after the tabs are cut). The wire groove isn't in the meshes, so
    setup 1's gouge check skips it.
    """
    from luregen.fab.router.cam import ops3d
    from luregen.fab.router.cam.heightmap import Grid, raster_top
    from luregen.fab.router.sim import Comparison, Heightmap, save_png, simulate_fast

    results: dict[str, dict] = {}
    for setup in st.SETUP_NAMES:
        texts = [(build / f.path).read_text() for f in files if f.setup == setup]
        if not texts:
            continue
        stock = Heightmap.block((0.0, 0.0), (layout.length, layout.width), 0.0, SIM_RES)
        simulate_fast(texts, stock)
        ny, nx = stock.z.shape
        grid = Grid(0.0, 0.0, SIM_RES, SIM_RES, nx, ny)
        wm = ops3d.WorkMesh.of(layout, setup)
        target = raster_top(wm.vertices, wm.faces, grid, fill=-np.inf)
        part = np.isfinite(target)
        # Gouge against the target's 3×3 minimum, so a vertical wall falling between cell
        # centres (the cut-out, pocket walls) doesn't count as a gouge of the wall's height.
        # Leftover is the plain difference, except on cells next to a vertical wall (the
        # target jumps > WALL_JUMP within one cell), where the wall's cell edge decides.
        from scipy.ndimage import maximum_filter, minimum_filter

        lo = minimum_filter(np.where(part, target, -1e3), 3, mode="nearest")
        cut_below = np.where(part, np.maximum(lo - stock.z, 0.0), 0.0)
        left_above = np.where(part, np.maximum(stock.z - np.where(part, target, 0.0), 0.0), 0.0)
        check_gouge = part.copy()
        if setup == "setup1":
            surface = part
            grooves = [f for f in features if f.kind == "groove"]
            if grooves:
                import shapely

                r = ctx.cuts["groove"].r + 2 * SIM_RES
                zones = shapely.union_all(
                    [
                        layout.to_work(h, f.geom, "setup1").buffer(r)
                        for h in layout.halves
                        for f in grooves
                        if f.section == h.section
                    ]
                )
                xs, ys = np.meshgrid(stock.xs, stock.ys)
                inside = shapely.contains_xy(zones, xs, ys)
                check_gouge &= ~inside
        else:
            surface = part & (target >= st.floor_z(layout, ctx) + 0.01)
        rim = part & ~surface
        gouge = np.where(check_gouge, cut_below, 0.0)
        hi = maximum_filter(np.where(part, target, 1e3), 3, mode="nearest")
        wall = (hi - lo) > WALL_JUMP
        left = np.where(surface & ~wall, left_above, 0.0)
        res = {
            "resolution_mm": SIM_RES,
            "tolerance_mm": SIM_TOL,
            "gouge_max_mm": round(float(max(gouge.max(), 0.0)), 3),
            "gouge_area_frac": round(float((gouge > SIM_TOL).sum() / max(part.sum(), 1)), 5),
            "leftover_max_mm": round(float(max(left.max(), 0.0)), 3),
            "leftover_p95_mm": round(float(np.percentile(left[surface], 95)), 3)
            if surface.any()
            else 0.0,
            "leftover_area_frac": round(float((left > SIM_TOL).sum() / max(surface.sum(), 1)), 5),
        }
        if rim.any():
            res["rim_leftover_max_mm"] = round(float(left_above[rim].max()), 3)
        png = out / f"sim_{setup}.png"
        shown = np.where(check_gouge, -gouge, 0.0) + np.where(surface, left, 0.0)
        cmp = Comparison(shown, SIM_TOL, res["gouge_max_mm"], res["leftover_max_mm"], 0.0, 0.0)
        save_png(png, stock, cmp, f"{setup}: simulated cut vs the halves")
        res["png"] = png.relative_to(build).as_posix()
        results[setup] = res
    return results


def surface_warnings(ctx: st.OpCtx, lib: ToolLibrary, cfg: LureConfig) -> list[str]:
    out = []
    sf = ctx.surface
    s3 = ctx.report.get("surface3d") or {}
    stats = {f"{k} {lab}": v for k in st.SETUP_NAMES for lab, v in (s3.get(k) or {}).items()}
    if sf is not None and sf.finish is not None:
        tool = cfg.cnc.ops.finish3d.tool
        entry = lib.tools[tool]
        if entry.flute_length is None:
            out.append(
                f"finish3d: no flute_length for {tool} in tools.toml; the shank isn't "
                "checked against deep walls"
            )
        deepest = min((v.get("finish_min_z") or 0.0) for v in stats.values()) if stats else 0
        if entry.flute_length and -deepest > entry.flute_length:
            out.append(
                f"finish3d: {tool} reaches {-deepest:.1f} mm below the stock top with "
                f"{entry.flute_length:g} mm flutes (assumed, #28): the shank limits deep "
                "walls (see sim leftover)"
            )
    return out


def glue_up_items(cfg: LureConfig, report: dict) -> list[str]:
    """What goes between the halves before they're glued."""
    geo = report.get("geometry", {})
    items = []
    b = geo.get("ballast") or {}
    if b.get("glue_face_pocket"):
        p = b["glue_face_pocket"]
        items.append(
            f"Ballast: {b['mass_g']} g of {b['material']} in the {b['section']} pocket "
            f"({p['length']:.1f} × {p['height']:.1f} mm, {p['depth']:.2f} mm deep per half), "
            "split between the halves; bed it in epoxy flush with the glue face."
        )
    for p in geo.get("cnc_air_pockets") or []:
        items.append(f"Air pocket ({p['kind']}): keep glue out of it; seal its rim.")
    if cfg.process.rigging == "through_wire":
        w = cfg.cnc.wire
        items.append(
            f"Through-wire: ⌀{w.diameter_mm} mm wire in the groove of both sections, with the "
            "line tie, hinge and hook loops formed before glue-up."
        )
    items.append("Glue matching right and left halves on the flip-pin dowels, then clamp.")
    return items


def post_glue_up_steps(report: dict) -> list[str]:
    """Steps after glue-up: the joint and trim features stay in the sections (#40) but
    aren't glue-face 2.5D ops, so they're drilled or cut on the glued sections."""
    geo = report.get("geometry", {})
    j = geo.get("joint") or {}
    steps = ["Cut the tabs and sand the tab band (the rim under the 3D floor) and the seam."]
    if j:
        steps.append(
            f"Tail: drill the ⌀{j['pin_bore_mm']} mm hinge-pin bore vertically (along z) "
            f"through the glue seam at x = {j['pin_x_mm']} mm from the nose."
        )
        zs = ", ".join(f"{e[1]:g}" for e in j.get("hinge_eyes_mm", []))
        steps.append(
            f"Tail: cut the eye slots into the notch face at z = {zs} mm; head: screw the "
            f"hinge eyes into the V face at x = {j['head_apex_x_mm']} mm, the same heights."
        )
        if j.get("line_tie_mm"):
            x, z = j["line_tie_mm"]
            steps.append(f"Head: line tie at x = {x:g}, z = {z:g} mm.")
    tp = geo.get("trim_pocket")
    if tp:
        steps.append(
            f"Tail: drill the trim pocket ⌀{2 * tp['r_mm']:.1f} mm up from the belly at "
            f"x = {tp['x_mm']} mm to z = {tp['z_top_mm']} mm, fill it with "
            f"{tp.get('fill_g', 0)} g of {tp.get('material', 'putty')}."
        )
    steps.append("Seal, fit the hinge pin, hooks and hardware, then check the float/sink trim.")
    return steps


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
    tools = summary.get("tools", {})

    def tool_cell(tid: str) -> str:
        t = tools.get(tid)
        if not t:
            return tid
        mark = "" if t["number_verified"] else " (T unverified)"
        return f"T{t['number']} {t['name'] or tid}{mark}"

    lines = [
        f"# CNC setup sheet: {summary['variant']}",
        "",
        f"Machine: {summary['machine']}. Material: {summary['material']}.",
        f"Stock: {s['length']} × {s['width']} × {s['thickness']} mm (X × Y × Z); "
        f"zero: {summary['zero']}.",
        f"Output: {'one file per tool' if summary['split_by_tool'] else 'one file per setup'}"
        "; each file starts with its own M6 tool change (the BitSetter measures every tool).",
        "",
        "## Run order",
        "",
        "| # | File | Setup | Load tool | Operations | Est. time | Verify |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, f in enumerate(summary["files"], 1):
        ok = "ok" if f["ok"] else f"{len(f['issues'])} issue(s)"
        ops = _ops_brief(f["operations"])
        lines.append(
            f"| {i} | `{f['path']}` | {f['setup']} | {', '.join(map(tool_cell, f['tools']))} "
            f"| {ops} | {_hms(f['runtime_s'])} | {ok} |"
        )
    lines += [
        "",
        f"Total estimated runtime: {_hms(summary['runtime_s'])} (tool changes included).",
        "",
        "## Steps",
        "",
        "1. **Setup 1, glue face up.** Clamp the blank, zero XY on the front-left corner and Z "
        "on the top. Run the setup1 files in order: glue-face pockets, the flip-pin holes "
        "(through, into the spoilboard), the wire groove"
        + (
            ", and the 3D overhang (faces that lean over the glue face)."
            if (summary.get("surface3d") or {}).get("setup1")
            else "."
        ),
        f"2. **Flip.** Put ⌀{summary['pins']['diameter_mm']} mm dowel pins in the holes and "
        "turn the blank over about X onto them, (X, Y, Z) → (X, W − Y, −T − Z). Keep the XY "
        "zero on the (new) front-left corner and re-zero Z on the new top.",
    ]
    sf = summary.get("surface3d")
    if sf:
        lines.append(
            "3. **Setup 2, outer face up.** Run the setup2 files in order: 3D roughing and the "
            "cut-out with tabs (same tool), then 3D finishing. The 3D ops stop at the floor "
            f"Z {sf['floor_z']} mm, the top of the tabs."
        )
    else:
        lines.append("3. **Setup 2, outer face up.** Cut out the halves (tabs hold them).")
    lines += ["4. **Before glue-up.**", ""]
    lines += [f"   - {item}" for item in summary.get("glue_up", [])]
    lines += ["", "5. **After glue-up.**", ""]
    lines += [f"   - {item}" for item in summary.get("post_glue_up", [])]
    sims = summary.get("sim") or {}
    if sims:
        lines += [
            "",
            "## Simulation",
            "",
            "Heightmap cut simulation of each setup's files against the halves "
            f"({next(iter(sims.values()))['resolution_mm']} mm cells). Gouge: cut below the "
            "surface. Leftover: material above it on the machined surface (cells on a vertical "
            "wall's edge excluded). Rim: the tab band under setup 2's floor.",
            "",
            "| Setup | Gouge max mm | Leftover max mm | Leftover p95 mm | "
            "Leftover > tol | Rim leftover max mm | Plot |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, r in sims.items():
            rim = r.get("rim_leftover_max_mm", "")
            lines.append(
                f"| {name} | {r['gouge_max_mm']} | {r['leftover_max_mm']} | "
                f"{r['leftover_p95_mm']} | {100 * r['leftover_area_frac']:.1f}% | {rim} | "
                f"`{r['png']}` |"
            )
        if "setup1" in sims:
            lines += [
                "",
                "Setup 1 leftover includes the hinge-pin bore and eye slots on the glue face, "
                "which are cut after glue-up.",
            ]
    lines += [
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


def _ops_brief(names: list[str]) -> str:
    """Operation names grouped by what follows the half label: `cut-out ×4`."""
    counts: dict[str, int] = {}
    for n in names:
        key = n.split(": ", 1)[-1]
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(k if c == 1 else f"{k} ×{c}" for k, c in counts.items())
