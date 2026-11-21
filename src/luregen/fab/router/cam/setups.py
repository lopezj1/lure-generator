"""Setup planner for CNC halves: stock layout, part → work transforms, the flip, programs.

**Layout.** One blank holds all four halves: the right halves (y ≥ 0) in the front row, the
left halves (y ≤ 0) in the back row, head then tail along X. Zero is the front-left corner of
the stock top in both setups.

**Setup 1, glue face up.** A half's glue face (y = 0) is the stock top (less any facing) and
its outer surface points down, so for side s (+1 right, −1 left):

    X = x + ox      Y = s·z + oy      Z = −face − s·y

(a proper rotation: right halves keep the dorsal side at +Y, left halves are mirrored in Y).
Ops: optional facing, glue-face pockets, flip-pin holes through the stock into the
spoilboard, the wire groove, then 3D roughing/finishing of any **overhang**: a
part of the side view the glue face doesn't cover (the tail's V-notch faces lean over its
glue face, so setup 2 can't reach them from above).

**Flip.** Turn the blank over about the X axis, onto the pins: (X, Y, Z) → (X, W − Y, −T − Z)
with the same XY zero (front-left corner) and Z re-zeroed on the new top. The pins sit on the
flip axis Y = W/2, so their holes map onto themselves.

**Setup 2, outer face up.** 3D roughing of the outer surface down to the floor (the top of
the tabs), the cut-out around each half's side-view silhouette with tabs (starting at the
depth roughing left along its path), then 3D finishing (`ops3d`, #42). Ops are grouped by
tool, so the roughing tool also cuts out before the finishing tool goes in, as in the
owner's jobs. `SETUP2_3D_OPS` hooks still run first.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry import box as shp_box

from luregen.fab.router.cam import ops2d
from luregen.fab.router.cam.ops2d import Cut, Tabs
from luregen.fab.router.ir import Operation, Program, Setup

Side = Literal["right", "left"]
SIGN = {"right": 1.0, "left": -1.0}
SETUP_NAMES = ("setup1", "setup2")


@dataclass
class Half:
    """One half of a section, in lure coordinates, plus its placement in the stock."""

    section: str
    side: Side
    silhouette: Polygon  # side view (x, z)
    half_width: float  # max |y|
    mesh: Any = None  # trimesh of the half, for 3D ops (#42)
    glue_outline: Polygon | None = None  # glue face (y = 0) outline in (x, z), holes dropped
    ox: float = 0.0
    oy: float = 0.0

    @property
    def s(self) -> float:
        return SIGN[self.side]

    @property
    def label(self) -> str:
        return f"{self.section} {self.side[0].upper()}"


@dataclass
class GlueFeature:
    """A glue-face feature of a section, in lure (x, z): a pocket region or a groove path."""

    section: str
    kind: Literal["pocket", "groove"]
    name: str
    geom: Polygon | LineString
    depth: float  # below the glue face; for a groove, the tool-tip depth


@dataclass
class Layout:
    halves: list[Half]
    length: float  # stock X
    width: float  # stock Y
    thickness: float  # stock Z
    face: float = 0.0
    pins: list[tuple[float, float]] = field(default_factory=list)  # setup-1 XY
    pin_d: float = 0.0
    pin_depth: float = 0.0  # below the stock bottom

    @property
    def glue_z(self) -> float:
        return -self.face

    def setup(self, name: str, safe_z: float) -> Setup:
        zero = "front-left corner, stock top"
        if name == "setup2":
            zero += " (after the flip about X)"
        return Setup(
            name=name,
            stock_min=(0.0, 0.0, -self.thickness),
            stock_max=(self.length, self.width, 0.0),
            zero=zero,
            safe_z=safe_z,
        )

    # --- transforms -------------------------------------------------------------------

    def matrix(self, half: Half, setup: str) -> np.ndarray:
        """4×4 lure → work transform of a half in a setup (for meshes, #42)."""
        s = half.s
        m = np.array(
            [
                [1.0, 0.0, 0.0, half.ox],
                [0.0, 0.0, s, half.oy],
                [0.0, -s, 0.0, -self.face],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )
        return self.flip_matrix() @ m if setup == "setup2" else m

    def flip_matrix(self) -> np.ndarray:
        """Setup 1 → setup 2: rotate 180° about X, keep the front-left XY zero."""
        return np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, -1.0, 0.0, self.width],
                [0.0, 0.0, -1.0, -self.thickness],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )

    def flip_point(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        return x, self.width - y, -self.thickness - z

    def to_work(self, half: Half, geom, setup: str):
        """Side-view (x, z) geometry → work XY of a setup."""
        m = self.matrix(half, setup)
        # (x, z) plane: X = m00·x + m02·z + m03, Y = m10·x + m12·z + m13
        return affinity.affine_transform(
            geom, [m[0, 0], m[0, 2], m[1, 0], m[1, 2], m[0, 3], m[1, 3]]
        )


def plan_layout(
    halves: list[Half],
    margin: float,
    spacing: float,
    thickness: float,
    face: float,
    pin_d: float = 0.0,
    pin_depth: float = 0.0,
) -> Layout:
    """Place the halves (rows: right then left; columns: sections in order)."""
    sections = list(dict.fromkeys(h.section for h in halves))
    bounds = {h.section: h.silhouette.bounds for h in halves}
    height = max(b[3] - b[1] for b in bounds.values())
    x = margin
    for name in sections:
        x0, z0, x1, z1 = bounds[name]
        for h in halves:
            if h.section != name:
                continue
            h.ox = x - x0
            if h.side == "right":  # Y = z + oy, bottom at the margin
                h.oy = margin - z0
            else:  # Y = −z + oy, bottom at margin + height + spacing
                h.oy = margin + height + spacing + z1
        x += (x1 - x0) + spacing
    length = x - spacing + margin
    width = 2 * margin + 2 * height + spacing
    pins = [(margin / 2, width / 2), (length - margin / 2, width / 2)] if pin_d > 0 else []
    return Layout(halves, length, width, thickness, face, pins, pin_d, pin_depth)


# --- planning ---------------------------------------------------------------------------


@dataclass
class OpCtx:
    """Per-op cut parameters by op kind (`pocket`, `groove`, `drill`, `profile`, `face`)."""

    cuts: dict[str, Cut]
    tabs_count: int = 4
    tabs_width: float = 5.0
    tabs_height: float = 1.5
    overcut: float = 0.3
    surface: Surface3D | None = None  # 3D outer-face ops in setup 2 (#42)
    report: dict = field(default_factory=dict)  # filled by the planner (3D stats, notes)


@dataclass
class Surface3D:
    """3D roughing/finishing of the outer face: settings and cutter geometry per op kind
    (`rough3d`, `finish3d`; either may be None)."""

    rough: Any = None  # ops3d.Rough3D
    finish: Any = None  # ops3d.Finish3D
    cutters: dict[str, Any] = field(default_factory=dict)  # kind -> heightmap.Cutter


# Extension point for #42: functions (layout, ctx) → list[Operation] run in setup 2 before
# the cut-out (3D roughing and finishing of the outer surface from the half meshes, using
# `Layout.matrix(half, "setup2")` to place them).
SETUP2_3D_OPS: list[Callable[[Layout, OpCtx], list[Operation]]] = []


def _op(name: str, kind: str, ctx: OpCtx, moves: list) -> Operation:
    cut = ctx.cuts[kind]
    return Operation(name=name, tool=cut.tool.id, rpm=cut.feeds.rpm, moves=moves)


def setup1_ops(layout: Layout, features: list[GlueFeature], ctx: OpCtx) -> list[Operation]:
    ops: list[Operation] = []
    top = 0.0
    glue = layout.glue_z
    if layout.face > 0:
        c = ctx.cuts["face"]
        rect = shp_box(0, 0, layout.length, layout.width)
        ops.append(_op("Face glue side", "face", ctx, ops2d.face(rect, top, glue, c)))
    for h in layout.halves:
        for f in features:
            if f.section == h.section and f.kind == "pocket":
                region = layout.to_work(h, f.geom, "setup1")
                moves = ops2d.pocket(region, glue, glue - f.depth, ctx.cuts["pocket"])
                ops.append(_op(f"{h.label}: {f.name} pocket", "pocket", ctx, moves))
    for i, (x, y) in enumerate(layout.pins):
        bottom = -layout.thickness - layout.pin_depth
        moves = ops2d.hole(x, y, layout.pin_d, top, bottom, ctx.cuts["drill"])
        ops.append(_op(f"Flip pin hole {i + 1}", "drill", ctx, moves))
    for h in layout.halves:
        for f in features:
            if f.section == h.section and f.kind == "groove":
                path = layout.to_work(h, f.geom, "setup1")
                moves = ops2d.groove(path, glue, glue - f.depth, ctx.cuts["groove"])
                ops.append(_op(f"{h.label}: {f.name} groove", "groove", ctx, moves))
    if ctx.surface is not None:
        regions = {h.label: r for h in layout.halves if (r := overhang(layout, h)) is not None}
        if regions:
            ops += surface_ops(layout, ctx, "setup1", regions)[0]
    return group_by_tool(ops)


def overhang(layout: Layout, half: Half, min_area: float = 1.0):
    """Setup-1 work region of a half that its outer face can't reach from setup 2: where
    the silhouette sticks out past the glue face (e.g. the tail's V-notch faces, which lean
    over the glue face). None if there's none."""
    if half.glue_outline is None:
        return None
    rest = half.silhouette.difference(half.glue_outline.buffer(0.2))
    parts = [p for p in ops2d._polys(rest) if p.area >= min_area and p.minimum_clearance > 0]
    parts = [p for p in parts if p.buffer(-0.3).area > 0]
    if not parts:
        return None
    return layout.to_work(half, shapely.union_all(parts), "setup1")


def setup2_ops(layout: Layout, ctx: OpCtx) -> list[Operation]:
    ops: list[Operation] = []
    for fn in SETUP2_3D_OPS:
        ops += fn(layout, ctx)
    tops: dict[str, float] = {}
    if ctx.surface is not None and (ctx.surface.rough or ctx.surface.finish):
        regions = {h.label: layout.to_work(h, h.silhouette, "setup2") for h in layout.halves}
        more, tops = surface_ops(layout, ctx, "setup2", regions)
        ops += more
    bottom = -layout.thickness - ctx.overcut
    tabs = None
    if ctx.tabs_count > 0:
        tabs = Tabs(ctx.tabs_count, ctx.tabs_width, -layout.thickness + ctx.tabs_height)
    for h in layout.halves:
        region = layout.to_work(h, h.silhouette, "setup2")
        moves = ops2d.profile(region, tops.get(h.label, 0.0), bottom, ctx.cuts["profile"], tabs)
        ops.append(_op(f"{h.label}: cut-out", "profile", ctx, moves))
    return group_by_tool(ops)


def floor_z(layout: Layout, ctx: OpCtx) -> float:
    """Lowest Z of the 3D ops: the tab band above the glue face is left for the cut-out."""
    return -layout.thickness + (ctx.tabs_height if ctx.tabs_count > 0 else 0.5)


def surface_ops(
    layout: Layout, ctx: OpCtx, setup: str, regions: dict[str, Any]
) -> tuple[list[Operation], dict[str, float]]:
    """3D roughing and finishing over each half's work region (setup 2: its silhouette, the
    whole outer face; setup 1: its overhang), and, in setup 2, the Z where each half's
    cut-out can start (the material top left by roughing along its path)."""
    from luregen.fab.router.cam import ops3d
    from luregen.fab.router.cam.heightmap import dilate_flat

    sf = ctx.surface
    assert sf is not None
    wm = ops3d.WorkMesh.of(layout, setup)
    floor = floor_z(layout, ctx)
    top = 0.0 if setup == "setup2" else layout.glue_z
    ops: list[Operation] = []
    tops: dict[str, float] = {}
    stats = ctx.report.setdefault("surface3d", {"floor_z": round(floor, 3)})
    stats = stats.setdefault(setup, {})
    prof = ctx.cuts["profile"]
    for h in layout.halves:
        if h.label not in regions:
            continue
        region = regions[h.label]
        what = "3D" if setup == "setup2" else "3D overhang"
        info: dict[str, Any] = {}
        if sf.rough is not None:
            cut, cutter = ctx.cuts["rough3d"], sf.cutters["rough3d"]
            boundary = region.buffer(cutter.r + sf.rough.allowance + sf.rough.boundary)
            res = ops3d.rough(wm, boundary, floor, cut, cutter, sf.rough, top)
            ops.append(_op(f"{h.label}: {what} rough", "rough3d", ctx, res.moves))
            info["rough_levels"] = len(res.levels)
            info["rough_main_levels"] = sum(1 for _, m in res.levels if m)
            if setup == "setup2":
                # Material top under the cut-out tool along its path, from the roughing sweep
                sil = layout.to_work(h, h.silhouette, setup)
                path = sil.buffer(prof.r, quad_segs=ops2d.QUAD_SEGS).exterior.segmentize(0.5)
                mat = dilate_flat(res.swept, prof.r + res.grid.rx, res.grid)
                i, j = res.grid.index(*np.asarray(path.coords).T)
                ok = (i >= 0) & (i < res.grid.nx) & (j >= 0) & (j < res.grid.ny)
                zmax = float(mat[j[ok], i[ok]].max()) if ok.all() else 0.0
                tops[h.label] = min(0.0, zmax + 0.2)
                info["cutout_start_z"] = round(tops[h.label], 3)
        if sf.finish is not None:
            cut, cutter = ctx.cuts["finish3d"], sf.cutters["finish3d"]
            area = region.buffer(cutter.r + sf.finish.margin)
            clear = (sf.rough.allowance + sf.rough.max_stair + 0.25) if sf.rough else 1.0
            moves = ops3d.finish(wm, area, floor, cut, cutter, sf.finish, clear)
            ops.append(_op(f"{h.label}: {what} finish", "finish3d", ctx, moves))
            zs = [mv.z for mv in moves if getattr(mv, "z", None) is not None]
            info["finish_moves"] = len(moves)
            info["finish_min_z"] = round(min(zs), 3) if zs else None
        stats[h.label] = info
    return ops, tops


def group_by_tool(ops: list[Operation]) -> list[Operation]:
    """Stable reorder so each tool's ops run together (in order of first use)."""
    order = list(dict.fromkeys(op.tool for op in ops))
    return sorted(ops, key=lambda op: order.index(op.tool))


def plan(
    design: str,
    layout: Layout,
    features: list[GlueFeature],
    ctx: OpCtx,
    safe_z: float = 5.0,
) -> list[Program]:
    """The two setups as IR programs (setup 1: glue face; setup 2: outer face)."""
    tools = {c.tool.id: c.tool for c in ctx.cuts.values()}
    programs = []
    for name, ops in (
        ("setup1", setup1_ops(layout, features, ctx)),
        ("setup2", setup2_ops(layout, ctx)),
    ):
        used = {op.tool: tools[op.tool] for op in ops}
        programs.append(
            Program(design=design, setup=layout.setup(name, safe_z), tools=used, operations=ops)
        )
    return programs


def wire_paths(silhouette: Polygon, z: float, exit_mm: float) -> list[LineString]:
    """Straight wire path(s) at height z across the side view, run out `exit_mm` each end."""
    x0, _, x1, _ = silhouette.bounds
    line = LineString([(x0 - 1, z), (x1 + 1, z)])
    pieces = silhouette.intersection(line)
    parts = [
        g
        for g in getattr(pieces, "geoms", [pieces])
        if isinstance(g, LineString) and not g.is_empty
    ]
    out = []
    for p in parts:
        (a, _), (b, _) = p.coords[0], p.coords[-1]
        a, b = min(a, b), max(a, b)
        if b - a > 1e-3:
            out.append(LineString([(a - exit_mm, z), (b + exit_mm, z)]))
    return out


def groove_depth(wire_d: float, clearance: float) -> float:
    """Depth per half of the wire channel: half of `wire_d` plus clearance."""
    return wire_d / 2 + clearance


def ball_groove_width(tool_r: float, depth: float) -> float:
    d = min(depth, tool_r)
    return 2 * math.sqrt(max(2 * tool_r * d - d * d, 0.0))
