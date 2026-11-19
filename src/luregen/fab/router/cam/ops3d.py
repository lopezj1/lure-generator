"""3D roughing and finishing of the outer face from the half meshes (setup 2, #42).

Both ops work on heightmaps of the halves in setup-2 work coordinates (`Layout.matrix`):
the target is the halves' top surface, never lower than the **floor** `−T + tabs.height`,
which leaves the tab band for the cut-out. Each heightmap covers every half (not only the
one being cut), so a tool near a neighbour can't gouge it.

**Roughing** (z-level 3D pocket, default 1/8in flat): the tool-tip surface for the tool
grown by the allowance (`tip_surface`, flat or ball kernel) is sliced at each Z level; the
slice ∩ the boundary (silhouette + tool radius + allowance + `boundary_mm`) is the region the
tool centre may visit, cleared with offset rings from the inside out, entered by a ramp
along the innermost ring (`ops2d.ring_pass`). Main levels are equal steps of the feeds' DOC
from the stock top to the floor. Between two main levels, extra levels every
`max_stair_mm` clear only the region the next main level won't reach, so the stair left on
shallow areas (the body's top) stays below `max_stair_mm` + allowance: the owner's job used
0.127 mm steps near the top and 0.5–1.0 mm below.

**Finishing** (parallel raster along X, default 1/32in flat at 0.076 mm stepover): the
tool-tip surface for the real tool (flat or ball kernel, plus the shank above the flutes
when the tool library gives one) on a grid whose rows coincide with the raster lines, so
lines need no interpolation across Y. Each line is clipped to the silhouette + tool radius,
then its (x, z) points are decimated with `tolerance_mm` (collinear points dropped). Lines
run zigzag; a short link to the next line stays down (above the tip surface between them),
a long one retracts. Ball nose: stepover = √(8·R·scallop). Output is plain G1: arcs (G18 for
an X raster, G19 for a Y raster) aren't fitted yet, and the post only emits the planes
in `arcs.planes` anyway.

**profile25d bodies** use the same two ops: their outer face is a flat top with a roundover
(`dfm.cnc.roundover_r_mm`) to the rim, which the raster reproduces; no roundover bit is on
hand (#28), so there's no dedicated 2.5D roundover op.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import reduce

import numpy as np
import shapely
from shapely.geometry import LinearRing, LineString, Polygon

from luregen.fab.router.cam import ops2d
from luregen.fab.router.cam.heightmap import Cutter, Grid, raster_top, sweep_surface, tip_surface
from luregen.fab.router.cam.ops2d import CamError, Cut, Ring, oriented, ring_pass
from luregen.fab.router.ir import Feed, Move, Rapid

_EPS = 1e-6
RING_TOL = 0.01  # mm; roughing rings are decimated to this (they keep ≥ the allowance)


@dataclass(frozen=True)
class Rough3D:
    allowance: float = 0.5
    max_stair: float = 0.25
    res: float = 0.15
    boundary: float = 1.0
    min_region: float = 0.3  # drop rest regions narrower than this


@dataclass(frozen=True)
class Finish3D:
    res: float = 0.05
    tolerance: float = 0.005
    margin: float = 0.2  # tool centres run to silhouette + r + margin
    link_max: float = 3.0  # longer links between lines retract


@dataclass
class WorkMesh:
    """All halves of a setup in work coordinates, as one triangle soup."""

    vertices: np.ndarray
    faces: np.ndarray

    @classmethod
    def of(cls, layout, setup: str) -> WorkMesh:
        verts, faces, n = [], [], 0
        for h in layout.halves:
            if h.mesh is None:
                raise CamError(f"{h.label}: no mesh for 3D ops")
            v = np.asarray(h.mesh.vertices, float)
            m = layout.matrix(h, setup)
            verts.append(v @ m[:3, :3].T + m[:3, 3])
            faces.append(np.asarray(h.mesh.faces) + n)
            n += len(v)
        return cls(np.vstack(verts), np.vstack(faces))

    def target(self, grid: Grid, floor: float) -> np.ndarray:
        """Top surface over the grid, not below `floor` (float32 to halve the memory)."""
        lo = (grid.x0, grid.y0)
        hi = (grid.x0 + grid.nx * grid.rx, grid.y0 + grid.ny * grid.ry)
        tri = self.vertices[self.faces]
        keep = (
            (tri[:, :, 0].max(1) >= lo[0])
            & (tri[:, :, 0].min(1) <= hi[0])
            & (tri[:, :, 1].max(1) >= lo[1])
            & (tri[:, :, 1].min(1) <= hi[1])
        )
        z = raster_top(self.vertices, self.faces[keep], grid, fill=floor)
        return np.maximum(z, floor)


def cutter_for(cut: Cut, flute_length: float | None, shank_d: float | None) -> Cutter:
    return Cutter(cut.tool.shape, cut.tool.diameter, flute_length, shank_d)


def scallop_stepover(tool_d: float, scallop: float) -> float:
    """Ball-nose stepover for a scallop height h: s = √(8·R·h) (R = tool radius)."""
    return math.sqrt(8 * (tool_d / 2) * scallop)


# --- regions ------------------------------------------------------------------------------


def below(field: np.ndarray, grid: Grid, z: float) -> Polygon | shapely.MultiPolygon:
    """Region where `field` ≤ z, as polygons in work XY (marching squares, even-odd)."""
    from skimage.measure import find_contours

    big = float(np.nanmax(field)) + 1e3
    pad = np.pad(field.astype(float), 1, constant_values=big)
    if not (pad <= z).any():
        return Polygon()
    polys = []
    for c in find_contours(pad, z):
        if len(c) < 4:
            continue
        xy = np.column_stack(
            [grid.x0 + (c[:, 1] - 0.5) * grid.rx, grid.y0 + (c[:, 0] - 0.5) * grid.ry]
        )
        p = Polygon(xy).buffer(0)
        if not p.is_empty:
            polys.append(p)
    if not polys:
        return Polygon()
    return reduce(lambda a, b: a.symmetric_difference(b), polys)


def offset_rings(region, r: float, stepover: float) -> list[tuple[list, bool]]:
    """Tool-centre rings for a region the centre may cover: its outline, then inward offsets
    every `stepover`, innermost first (like `ops2d.pocket_rings`, but offsets start at 0)."""
    stepover = min(stepover, 1.8 * r)
    offsets: list[float] = []
    d = 0.0
    while True:
        poly = shapely.buffer(region, -d, quad_segs=ops2d.QUAD_SEGS) if d else region
        if poly.is_empty or poly.area < 1e-6:
            break
        offsets.append(d)
        nxt = d + stepover
        d = nxt if not shapely.buffer(region, -nxt).is_empty else d + 0.9 * r
    rings: list[tuple[list, bool]] = []
    for d in reversed(offsets):
        poly = shapely.buffer(region, -d, quad_segs=ops2d.QUAD_SEGS) if d else region
        for p in ops2d._polys(poly):
            rings.append((list(p.exterior.coords), False))
            rings += [(list(i.coords), True) for i in p.interiors]
    return rings


def clear_region(region, z: float, z_from: float, cut: Cut, approach: float) -> list[Move]:
    """Clear a tool-centre region at depth z (material at most at `z_from` above it)."""
    rings = offset_rings(region, cut.r, cut.feeds.stepover)
    reach = shapely.buffer(region, 1e-3)
    moves: list[Move] = []
    pos = None
    for coords, hole in rings:
        if len(coords) < 4:
            continue
        lr = LinearRing(coords).simplify(RING_TOL)
        if lr.is_empty or len(lr.coords) < 4:
            continue
        ring = Ring(oriented(lr, ccw=cut.climb != hole))
        if ring.length < 0.05:
            continue
        k = 0 if pos is None else int(np.argmin(np.linalg.norm(ring.pts - pos, axis=1)))
        start = float(ring.cum[k])
        sx, sy = ring.at(start)
        link = None if pos is None else LineString([tuple(pos), (sx, sy)])
        if link is not None and (link.length < 1e-3 or reach.contains(link)):
            moves.append(Feed(x=sx, y=sy, f=cut.feeds.feed))
            pass_moves, s_end = ring_pass(ring, start, z, z, cut)
        else:
            moves += [
                Rapid(z=cut.safe_z),
                Rapid(x=sx, y=sy),
                Rapid(z=z_from + approach),
                Feed(z=z_from, f=cut.feeds.plunge),
            ]
            pass_moves, s_end = ring_pass(ring, start, z_from, z, cut)
        moves += pass_moves
        pos = np.array(ring.at(s_end))  # a ramped pass ends past its start
    return moves


# --- roughing -----------------------------------------------------------------------------


@dataclass
class RoughResult:
    moves: list[Move]
    levels: list[tuple[float, bool]]  # (z, is_main)
    grid: Grid
    swept: np.ndarray  # material top after roughing (fast sim of the moves)


def rough_levels(top: float, floor: float, doc: float, stair: float) -> list[tuple[float, float]]:
    """(z, next main level below or z itself) top-down: main levels at `doc`, plus rest
    levels every `stair` between them."""
    mains = ops2d.levels(top, floor, doc)
    out: list[tuple[float, float]] = []
    upper = top
    for m in mains:
        n = math.ceil((upper - m) / stair - 1e-9) if stair > 0 else 1
        for k in range(1, n):
            out.append((upper - (upper - m) * k / n, m))
        out.append((m, m))
        upper = m
    return out


def rough(
    wm: WorkMesh,
    boundary,
    floor: float,
    cut: Cut,
    cutter: Cutter,
    cfg: Rough3D,
    top: float = 0.0,
) -> RoughResult:
    """Z-level roughing of everything above `floor` inside `boundary` (tool-centre region)."""
    a = cfg.allowance
    pad = cutter.r + a + 2 * cfg.res + (cutter.shank_diameter or 0)
    grid = Grid.cover(shapely.buffer(boundary, pad).bounds, cfg.res)
    target = wm.target(grid, floor)
    tip = tip_surface(target, cutter, grid, a)
    approach = a + cfg.max_stair + 0.25
    moves: list[Move] = []
    plan: list[tuple[float, bool]] = []
    regions: dict[float, object] = {}

    def region_at(z: float):
        if z not in regions:
            regions[z] = shapely.intersection(below(tip, grid, z - 0.01), boundary).simplify(0.02)
        return regions[z]

    main_from = top  # material top over a main level's region
    prev = top
    for z, main_z in rough_levels(top, floor, cut.feeds.doc, cfg.max_stair):
        is_main = abs(z - main_z) < _EPS
        reg = region_at(z)
        if not is_main:
            h = cfg.min_region / 2
            rest = shapely.difference(reg, region_at(main_z))
            reg = shapely.buffer(shapely.buffer(rest, -h), h).intersection(reg)
        if reg.is_empty or reg.area < 1e-3:
            if is_main:
                main_from = prev = z
            continue
        z_from = main_from if is_main else prev
        moves += clear_region(reg, z, z_from, cut, approach)
        plan.append((z, is_main))
        prev = z
        if is_main:
            main_from = z
    moves.append(Rapid(z=cut.safe_z))
    swept = np.minimum(top, sweep_surface(tip_positions(moves, grid), cutter, grid))
    return RoughResult(moves, plan, grid, swept)


# --- finishing ----------------------------------------------------------------------------


def finish(
    wm: WorkMesh,
    region,
    floor: float,
    cut: Cut,
    cutter: Cutter,
    cfg: Finish3D,
    clear_z: float,
) -> list[Move]:
    """Parallel raster along X over the tool-centre `region`, zigzag, decimated."""
    so = cut.feeds.stepover
    m = max(1, math.ceil(so / cfg.res - 1e-9))
    ry = so / m
    pad = cutter.r + 2 * cfg.res + max(cutter.shank_diameter or 0, cutter.diameter)
    x0, y0, x1, y1 = shapely.buffer(region, pad).bounds
    # rows of the grid at the raster lines: line k at y0r + k·so
    ry0 = region.bounds[1]
    k0 = math.floor((ry0 - y0) / so) + 1
    gy0 = ry0 - k0 * so - ry / 2
    grid = Grid.cover((x0, gy0, x1, y1), cfg.res, ry)
    target = wm.target(grid, floor)
    tip = tip_surface(target, cutter, grid, 0.0)
    xs = grid.xs
    passes: list[list[np.ndarray]] = []  # per line: list of (n, 3) point arrays
    rb = region.bounds
    for j in range(0, grid.ny):
        y = grid.ys[j]
        if (j - k0 * m) % m or y < rb[1] - _EPS or y > rb[3] + _EPS:
            continue
        hit = region.intersection(LineString([(x0 - 1, y), (x1 + 1, y)]))
        segs = [g for g in getattr(hit, "geoms", [hit]) if isinstance(g, LineString)]
        line = []
        for g in sorted(segs, key=lambda g: g.bounds[0]):
            a, b = g.bounds[0], g.bounds[2]
            sel = (xs >= a) & (xs <= b)
            if sel.sum() < 2:
                continue
            pts = np.column_stack([xs[sel], tip[j, sel]])
            dec = LineString(pts).simplify(cfg.tolerance, preserve_topology=False)
            dec = np.asarray(dec.coords)
            line.append(np.column_stack([dec[:, 0], np.full(len(dec), y), dec[:, 1]]))
        if line:
            passes.append(line)
    moves: list[Move] = []
    pos = None
    for n, line in enumerate(passes):
        if n % 2:
            line = [seg[::-1] for seg in reversed(line)]
        for seg in line:
            moves += _link(pos, seg[0], tip, grid, cut, clear_z, cfg.link_max)
            moves += _raster(seg, cut)
            pos = seg[-1]
    moves.append(Rapid(z=cut.safe_z))
    return moves


def _link(pos, nxt, tip, grid: Grid, cut: Cut, clear_z: float, link_max: float) -> list[Move]:
    x, y, z = (float(v) for v in nxt)
    if pos is not None and math.dist(pos[:2], nxt[:2]) <= link_max:
        # stay down: rise to the highest tip within the link's box, cross, drop
        i, j = grid.index([pos[0], x], [pos[1], y])
        i0, i1 = sorted(np.clip(i, 0, grid.nx - 1))
        j0, j1 = sorted(np.clip(j, 0, grid.ny - 1))
        zl = max(float(tip[j0 : j1 + 1, max(i0 - 1, 0) : i1 + 2].max()), float(pos[2]), z)
        out: list[Move] = []
        if zl > pos[2] + _EPS:
            out.append(Feed(z=zl, f=cut.feeds.feed))
        out.append(Feed(x=x, y=y, f=cut.feeds.feed))
        if z < zl - _EPS:
            out.append(Feed(z=z, f=cut.feeds.plunge))
        return out
    return [
        Rapid(z=cut.safe_z),
        Rapid(x=x, y=y),
        Rapid(z=min(z + clear_z, cut.safe_z)),
        Feed(z=z, f=cut.feeds.plunge),
    ]


def _raster(seg: np.ndarray, cut: Cut) -> list[Move]:
    out: list[Move] = []
    for p0, p1 in zip(seg, seg[1:], strict=False):
        dz = p1[2] - p0[2]
        steep = dz < 0 and -dz > abs(p1[0] - p0[0])  # descending steeper than 45°
        f = cut.feeds.plunge if steep else cut.feeds.feed
        out.append(Feed(x=float(p1[0]), z=float(p1[2]), f=f))
    return out


# --- fast simulation of IR moves ----------------------------------------------------------


def polyline_samples(pts: np.ndarray, step: float) -> np.ndarray:
    """Points every ≤ `step` along a 3D polyline (N, 3), endpoints included."""
    if len(pts) < 2:
        return pts
    d = np.diff(pts, axis=0)
    n = np.maximum(np.ceil(np.linalg.norm(d[:, :2], axis=1) / step).astype(int), 1)
    seg = np.repeat(np.arange(len(d)), n)
    t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
    out = pts[seg] + d[seg] * t[:, None]
    return np.vstack([out, pts[-1:]])


def tip_positions(moves: list[Move], grid: Grid, start=(0.0, 0.0, None)) -> np.ndarray:
    """Lowest tool-tip Z per cell along `moves` (+inf where never visited). Rapids and feeds
    only (3D ops emit no arcs); unknown start coordinates skip the first move."""
    x, y, z = start
    pts: list[tuple[float, float, float]] = []
    for mv in moves:
        if mv.kind not in ("rapid", "feed"):
            continue
        x = mv.x if mv.x is not None else x
        y = mv.y if mv.y is not None else y
        z = mv.z if mv.z is not None else z
        if z is not None:
            pts.append((x, y, z))
    return tipmin_grid([np.asarray(pts, float)], grid)


def tipmin_grid(polylines: list[np.ndarray], grid: Grid) -> np.ndarray:
    out = np.full(grid.shape, np.inf)
    step = min(grid.rx, grid.ry) / 2
    for pl in polylines:
        if len(pl) == 0:
            continue
        s = polyline_samples(pl, step)
        i, j = grid.index(s[:, 0], s[:, 1])
        ok = (i >= 0) & (i < grid.nx) & (j >= 0) & (j < grid.ny)
        np.minimum.at(out, (j[ok], i[ok]), s[ok, 2])
    return out
