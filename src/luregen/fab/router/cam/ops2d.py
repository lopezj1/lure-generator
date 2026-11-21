"""2.5D toolpath ops on shapely geometry, emitting toolpath IR moves (#41).

Conventions: work coordinates in mm; `z_top` is the material surface the op starts from and
`z_bottom` the final tool-tip depth. Every op starts and ends at `safe_z` and moves in XY only
at `safe_z`, so `verify`'s rapid check holds. Depth passes are equal steps no deeper than
the feeds' DOC. Climb milling (spindle CW): pocket walls run counter-clockwise, outside
profiles clockwise; `climb=False` reverses both.

- `pocket`: offset rings (shapely `buffer`) from the inside out, entered with a ramp along
  the innermost ring; the core gets extra rings so no island is left when the stepover is
  wider than the tool radius.
- `profile`: an outside (or inside) contour with ramped depth passes and holding tabs.
- `groove`: an open path at constant depth (the wire half-groove).
- `hole`: a peck drill when the hole is the tool's size, else a helical bore.
- `face`: a zigzag raster over a rectangle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LinearRing, LineString, MultiPolygon, Polygon

from luregen.fab.router.feeds import Feeds
from luregen.fab.router.ir import Arc, Drill, Feed, Move, Rapid, Tool

QUAD_SEGS = 16
SIMPLIFY_TOL = 0.005  # mm; drops collinear mesh-outline vertices from contours
_EPS = 1e-6


class CamError(ValueError):
    pass


@dataclass(frozen=True)
class Cut:
    """Everything an op needs besides its geometry."""

    tool: Tool
    feeds: Feeds
    safe_z: float = 5.0
    approach: float = 0.5  # rapid down to this far above the material, then feed
    ramp_angle_deg: float = 3.0
    climb: bool = True

    @property
    def r(self) -> float:
        return self.tool.radius


@dataclass(frozen=True)
class Tabs:
    count: int
    width: float  # material left between cuts, mm (the path gap adds the tool diameter)
    top_z: float  # tab top, work Z


def levels(z_top: float, z_bottom: float, doc: float) -> list[float]:
    """Equal depth passes from just below `z_top` to `z_bottom`."""
    depth = z_top - z_bottom
    if depth <= _EPS:
        return []
    n = max(1, math.ceil(depth / doc - 1e-9))
    return [z_top - depth * (k + 1) / n for k in range(n)]


class Ring:
    """A closed polyline parametrized by arc length (wraps around)."""

    def __init__(self, coords):
        pts = np.asarray(coords, float)[:, :2]
        if np.allclose(pts[0], pts[-1]):
            pts = pts[:-1]
        self.pts = pts
        seg = np.linalg.norm(np.roll(pts, -1, axis=0) - pts, axis=1)
        self.cum = np.concatenate([[0.0], np.cumsum(seg)])
        self.length = float(self.cum[-1])

    def at(self, s: float) -> tuple[float, float]:
        s %= self.length
        i = int(np.searchsorted(self.cum, s, side="right") - 1)
        i = min(i, len(self.pts) - 1)
        t = (s - self.cum[i]) / max(self.cum[i + 1] - self.cum[i], 1e-12)
        a, b = self.pts[i], self.pts[(i + 1) % len(self.pts)]
        return float(a[0] + t * (b[0] - a[0])), float(a[1] + t * (b[1] - a[1]))

    def breaks(self, s0: float, s1: float) -> list[float]:
        """Vertex positions strictly between s0 and s1 (s1 may exceed s0 by laps)."""
        out = []
        lap = math.floor(s0 / self.length)
        while lap * self.length < s1:
            for c in self.cum[:-1]:
                s = lap * self.length + c
                if s0 + _EPS < s < s1 - _EPS:
                    out.append(float(s))
            lap += 1
        return out


def oriented(ring: LinearRing | LineString, ccw: bool) -> list:
    coords = list(ring.coords)
    is_ccw = LinearRing(coords).is_ccw
    return coords if is_ccw == ccw else coords[::-1]


def _move(x, y, z, z_prev, cut: Cut) -> Feed:
    """Descending moves (plunge, ramp) at the plunge rate, everything else at the feed."""
    return Feed(x=x, y=y, z=z, f=cut.feeds.plunge if z < z_prev - _EPS else cut.feeds.feed)


def ring_pass(
    ring: Ring,
    s0: float,
    z_from: float,
    z_to: float,
    cut: Cut,
    tab_spans: list[tuple[float, float]] | None = None,
    tab_top: float | None = None,
) -> tuple[list[Move], float]:
    """Ramp from `z_from` to `z_to` along the ring starting at s0, then one full lap at
    `z_to`; lift to `tab_top` over the tab spans. Returns the moves and the end position."""
    drop = max(z_from - z_to, 0.0)
    ramp_len = drop / math.tan(math.radians(cut.ramp_angle_deg)) if drop > _EPS else 0.0
    if ring.length < 4 * cut.tool.diameter and drop > _EPS:
        ramp_len = 0.0  # too short to ramp: plunge at the start
    s_end = s0 + ramp_len + ring.length

    def z_ramp(s: float) -> float:
        if ramp_len <= 0:
            return z_to
        return max(z_to, z_from - (s - s0) / ramp_len * drop)

    spans: list[tuple[float, float]] = []
    if tab_spans and tab_top is not None:
        lap = math.floor(s0 / ring.length) - 1
        while lap * ring.length < s_end:
            spans += [(a + lap * ring.length, b + lap * ring.length) for a, b in tab_spans]
            lap += 1

    marks = {s0, s0 + ramp_len, s_end, *ring.breaks(s0, s_end)}
    marks |= {e for a, b in spans for e in (a, b) if s0 < e < s_end}
    if spans and ramp_len > 0 and z_to < tab_top < z_from:  # type: ignore[operator]
        marks.add(s0 + (z_from - tab_top) / drop * ramp_len)  # type: ignore[operator]
    order = sorted(marks)

    def z_piece(s: float, in_tab: bool) -> float:
        return max(z_ramp(s), tab_top) if in_tab else z_ramp(s)  # type: ignore[type-var]

    moves: list[Move] = []
    z_cur = z_from
    for m0, m1 in zip(order, order[1:], strict=False):
        if m1 - m0 < _EPS:
            continue
        in_tab = any(a < 0.5 * (m0 + m1) < b for a, b in spans)
        za, zb = z_piece(m0, in_tab), z_piece(m1, in_tab)
        if abs(za - z_cur) > _EPS:  # step down (plunge) or up (onto a tab)
            moves.append(_move(*ring.at(m0), za, z_cur, cut))
        moves.append(_move(*ring.at(m1), zb, za, cut))
        z_cur = zb
    return moves, s_end


def _retract(cut: Cut) -> list[Move]:
    return [Rapid(z=cut.safe_z)]


def _approach(x: float, y: float, z: float, cut: Cut) -> list[Move]:
    """From safe Z: rapid over (x, y), rapid down near `z`, feed down to `z`."""
    return [
        Rapid(z=cut.safe_z),
        Rapid(x=x, y=y),
        Rapid(z=z + cut.approach),
        Feed(z=z, f=cut.feeds.plunge),
    ]


# --- pocket -----------------------------------------------------------------------------


def _polys(geom) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def pocket_rings(region: Polygon, r: float, stepover: float) -> list[tuple[list, bool]]:
    """Offset rings, innermost first, as (coords, is_wall_of_hole) pairs."""
    stepover = min(stepover, 1.8 * r)
    if shapely.buffer(region, -r, quad_segs=QUAD_SEGS).is_empty:
        raise CamError(f"pocket is narrower than the {2 * r:g} mm tool")
    offsets: list[float] = []
    d = r
    while True:
        poly = shapely.buffer(region, -d, quad_segs=QUAD_SEGS)
        if poly.is_empty or poly.area < 1e-6:
            break
        offsets.append(d)
        nxt = d + stepover
        d = nxt if not shapely.buffer(region, -nxt).is_empty else d + 0.9 * r
    rings: list[tuple[list, bool]] = []
    for d in reversed(offsets):
        for p in _polys(shapely.buffer(region, -d, quad_segs=QUAD_SEGS)):
            rings.append((list(p.exterior.coords), False))
            rings += [(list(i.coords), True) for i in p.interiors]
    return rings


def pocket(region: Polygon, z_top: float, z_bottom: float, cut: Cut) -> list[Move]:
    """Clear `region` (its outline is the wall) down to a flat floor at `z_bottom`."""
    rings = pocket_rings(region, cut.r, cut.feeds.stepover)
    reach = shapely.buffer(region, -cut.r + 1e-3, quad_segs=QUAD_SEGS)
    moves: list[Move] = []
    z_prev = z_top
    for z in levels(z_top, z_bottom, cut.feeds.doc):
        pos = None
        for i, (coords, hole) in enumerate(rings):
            ring = Ring(oriented(LinearRing(coords), ccw=cut.climb != hole))
            if ring.length < _EPS:
                continue
            # start where the ring is nearest to the current position
            k = 0 if pos is None else int(np.argmin(np.linalg.norm(ring.pts - pos, axis=1)))
            start = float(ring.cum[k])
            sx, sy = ring.at(start)
            if i == 0:
                moves += _approach(sx, sy, z_prev, cut)
                pass_moves, s_end = ring_pass(ring, start, z_prev, z, cut)
            else:
                link = LineString([tuple(pos), (sx, sy)])
                if reach.contains(link) or link.length < 1e-3:
                    moves.append(Feed(x=sx, y=sy, f=cut.feeds.feed))
                    pass_moves, s_end = ring_pass(ring, start, z, z, cut)
                else:  # leaving the cleared area: retract and ramp in again
                    moves += _approach(sx, sy, z_prev, cut)
                    pass_moves, s_end = ring_pass(ring, start, z_prev, z, cut)
            moves += pass_moves
            pos = np.array(ring.at(s_end))  # a ramped pass ends past its start
        z_prev = z
    return [*moves, *_retract(cut)]


# --- profile ----------------------------------------------------------------------------


def tab_spans(ring: Ring, tabs: Tabs, tool_d: float) -> list[tuple[float, float]]:
    """Evenly spaced tab spans along the ring, as arc-length intervals in [0, length)."""
    if tabs.count <= 0:
        return []
    span = tabs.width + tool_d
    if span * tabs.count > 0.5 * ring.length:
        raise CamError(
            f"{tabs.count} tabs of {tabs.width} mm do not fit a {ring.length:.0f} mm cut"
        )
    step = ring.length / tabs.count
    return [(step * (k + 0.5) - span / 2, step * (k + 0.5) + span / 2) for k in range(tabs.count)]


def profile(
    region: Polygon,
    z_top: float,
    z_bottom: float,
    cut: Cut,
    tabs: Tabs | None = None,
    outside: bool = True,
) -> list[Move]:
    """Cut around `region` (outside by default) to `z_bottom`, leaving tabs if given."""
    offset = shapely.buffer(region, cut.r if outside else -cut.r, quad_segs=QUAD_SEGS)
    polys = _polys(offset)
    if not polys:
        raise CamError("profile offset is empty")
    path = max(polys, key=lambda p: p.area).exterior.simplify(SIMPLIFY_TOL)
    ccw = (not outside) == cut.climb
    ring = Ring(oriented(path, ccw=ccw))
    spans = tab_spans(ring, tabs, cut.tool.diameter) if tabs else []
    moves: list[Move] = []
    sx, sy = ring.at(0.0)
    moves += _approach(sx, sy, z_top, cut)
    z_prev, s = z_top, 0.0
    for z in levels(z_top, z_bottom, cut.feeds.doc):
        use_tabs = tabs is not None and z < tabs.top_z - _EPS
        pass_moves, s = ring_pass(
            ring, s, z_prev, z, cut, spans if use_tabs else None, tabs.top_z if use_tabs else None
        )
        moves += pass_moves
        z_prev = z
    return [*moves, *_retract(cut)]


# --- groove, holes, facing --------------------------------------------------------------


def groove(path: LineString, z_top: float, z_bottom: float, cut: Cut) -> list[Move]:
    """Follow an open path at constant depth, back and forth by depth pass."""
    pts = list(path.coords)
    moves = _approach(*pts[0], z_top, cut)
    z_prev = z_top
    for i, z in enumerate(levels(z_top, z_bottom, cut.feeds.doc)):
        seq = pts if i % 2 == 0 else pts[::-1]
        moves.append(Feed(z=z, f=cut.feeds.plunge))
        moves += [Feed(x=x, y=y, f=cut.feeds.feed) for x, y in seq[1:]]
        z_prev = z
    del z_prev
    return [*moves, *_retract(cut)]


def hole(x: float, y: float, d: float, z_top: float, z_bottom: float, cut: Cut) -> list[Move]:
    """A hole of diameter `d`: peck drill if it is the tool's size, else a helical bore."""
    tool_d = cut.tool.diameter
    if d < tool_d - 0.01:
        raise CamError(f"hole {d:g} mm is smaller than the {tool_d:g} mm tool")
    rp = (d - tool_d) / 2
    if rp < 0.05:
        return [
            Rapid(z=cut.safe_z),
            Drill(
                x=x,
                y=y,
                r_z=z_top + cut.approach + 1.0,
                bottom_z=z_bottom,
                peck=tool_d,
                f=cut.feeds.plunge,
            ),
            *_retract(cut),
        ]
    # Helix: climb inside a hole runs counter-clockwise; half-turn arcs, pitch = DOC.
    cw = not cut.climb
    pitch = min(cut.feeds.doc, tool_d / 2)
    f = cut.feeds.feed
    moves = _approach(x + rp, y, z_top, cut)
    z, sign = z_top, 1.0
    while z > z_bottom + _EPS:
        z = max(z - pitch / 2, z_bottom)
        sign = -sign
        moves.append(Arc(x=x + sign * rp, y=y, z=z, cx=x, cy=y, cw=cw, f=f))
    for _ in range(2):  # a full lap at the bottom
        sign = -sign
        moves.append(Arc(x=x + sign * rp, y=y, cx=x, cy=y, cw=cw, f=f))
    moves.append(Feed(x=x, y=y, f=f))
    return [*moves, *_retract(cut)]


def face(rect: Polygon, z_top: float, z_bottom: float, cut: Cut) -> list[Move]:
    """Zigzag raster along X covering `rect` (tool centre runs to its edges)."""
    x0, y0, x1, y1 = rect.bounds
    so = cut.feeds.stepover
    n = max(1, math.ceil((y1 - y0) / so))
    ys = [y0 + (y1 - y0) * k / n for k in range(n + 1)]
    moves: list[Move] = []
    for z in levels(z_top, z_bottom, cut.feeds.doc):
        moves += _approach(x0, ys[0], z_top, cut)
        moves.append(Feed(z=z, f=cut.feeds.plunge))
        fwd = True
        for k, y in enumerate(ys):
            if k:
                moves.append(Feed(y=y, f=cut.feeds.feed))
            moves.append(Feed(x=x1 if fwd else x0, f=cut.feeds.feed))
            fwd = not fwd
        moves += _retract(cut)
    return moves
