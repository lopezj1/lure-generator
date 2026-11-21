r"""Separate tail fin (#123): a flat paddle printed on its own in TPU, held in a slot in the
tail section by a lateral screw.

Side view (x, z), x toward the tail::

          tail section              fin
    ───────────────────────┐┌───────────────╮
            ┌──────────────┼┼──┐             │  span at the trailing edge
            │ slot ⊙ screw │ tab             │  (optional V fork)
            └──────────────┼┼──┘             │
    ───────────────────────┘└───────────────╯
                         x_cut             x_cut + length

- The pointed tail tip is cut off at `x_cut = tail tip − cut_from_tail_mm`; the fin root
  sits flush against the flat cut face.
- The fin is a planar outline in the xz plane (root `root_height_mm` tall, trailing edge
  `span_mm` tall, rounded trailing corners, optional fork) extruded `thickness_mm` in y
  about y = 0, plus a root **tab** of the same thickness reaching forward into the tail.
  Flat sides let it print lying on its side with no supports.
- The tail gets a **slot** on y = 0 (the fin's plane): `depth` into the cut end, the tab's
  thickness and height plus the fit clearance, rounded side-view corners. On a CNC body the
  slot straddles the glue face, so each half gets a half-thickness glue-face pocket.
- A **screw** runs sideways (along y) through the tail and the tab at mid-slot: a pilot hole
  through the tail and a matching hole through the tab.

Shapes are built directly (polyline face, extrude, boxes, cylinders); every result is
checked with `assert_valid`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Axis, Box, Cylinder, Face, Polyline, Pos, Rot, Solid, extrude, fillet

from luregen.geometry.validate import GeometryError, assert_valid
from luregen.profile.table import StationTable

ROOT_HEIGHT_FRAC = 0.8  # default root height: this fraction of the tail depth at the cut
SLOT_HEIGHT_FRAC = 0.5  # default slot height: this fraction of the root height
SLOT_RUNOUT = 5.0  # mm past the cut face so the slot opens cleanly onto it
SKIN_SAMPLES = 11


class FinError(GeometryError):
    pass


@dataclass(frozen=True)
class FinSpec:
    """Resolved fin, slot and screw dimensions in body coordinates (mm)."""

    x_cut: float  # tail cut plane = fin root
    z_axis: float  # fin centre line (the station centre at the cut)
    tail_end_x: float  # original tail tip
    tail_depth_at_cut: float  # 2 × v_radius at the cut
    tail_width_at_cut: float  # 2 × half_width at the cut
    length: float
    root_height: float
    span: float
    thickness: float
    fork_depth: float
    corner_r: float
    slot_depth: float
    slot_height: float
    slot_clearance: float
    slot_end_gap: float
    slot_corner_r: float
    screw_x: float
    screw_z: float
    pilot_d: float
    tab_hole_d: float
    screw_length: float

    @property
    def x_tip(self) -> float:
        return self.x_cut + self.length

    @property
    def slot_width(self) -> float:
        return self.thickness + self.slot_clearance

    @property
    def slot_x0(self) -> float:
        return self.x_cut - self.slot_depth

    @property
    def tab_height(self) -> float:
        return self.slot_height - self.slot_clearance

    @property
    def tab_x0(self) -> float:
        return self.slot_x0 + self.slot_end_gap

    @property
    def tab_corner_r(self) -> float:
        return max(self.slot_corner_r - 0.5 * self.slot_clearance, 0.0)


def fin_spec(cfg, table: StationTable, slot_corner_r: float | None = None) -> FinSpec:
    """FinSpec from `cfg.fin` and the (scaled) station table. `slot_corner_r` overrides the
    configured slot corner radius (CNC: the pocket tool's)."""
    from luregen.geometry.pockets import _interp

    f = cfg.fin
    tail_x = float(table.tail[0])
    x_cut = tail_x - f.cut_from_tail_mm
    cz, v, hw = _interp(table, x_cut)
    if v <= 0 or hw <= 0:
        raise FinError(f"fin: tail cut at x = {x_cut:.1f} mm is outside the body")
    root = f.root_height_mm or ROOT_HEIGHT_FRAC * 2 * v
    slot_h = f.slot.height_mm or SLOT_HEIGHT_FRAC * root
    screw_x = x_cut - (f.screw.x_from_cut_mm or 0.5 * f.slot.depth_mm)
    return FinSpec(
        x_cut=x_cut,
        z_axis=cz,
        tail_end_x=tail_x,
        tail_depth_at_cut=2 * v,
        tail_width_at_cut=2 * hw,
        length=f.length_mm,
        root_height=root,
        span=f.span_mm,
        thickness=f.thickness_mm,
        fork_depth=f.fork_depth_mm,
        corner_r=f.corner_r_mm,
        slot_depth=f.slot.depth_mm,
        slot_height=slot_h,
        slot_clearance=f.slot.clearance_mm,
        slot_end_gap=f.slot.end_gap_mm,
        slot_corner_r=f.slot.corner_r_mm if slot_corner_r is None else slot_corner_r,
        screw_x=screw_x,
        screw_z=cz + f.screw.z_offset_mm,
        pilot_d=f.screw.pilot_d_mm,
        tab_hole_d=f.screw.tab_hole_d_mm,
        screw_length=f.screw.length_mm,
    )


def check_spec(spec: FinSpec, x_min: float) -> None:
    """Hard limits: the fin, tab, slot and screw must fit; `x_min` is where the tail starts."""
    if spec.slot_x0 <= x_min + 2.0:
        raise FinError(
            f"fin: the {spec.slot_depth} mm slot from the tail cut at x = {spec.x_cut:.1f} mm "
            f"reaches the joint (tail starts at {x_min:.1f} mm)"
        )
    if spec.root_height > spec.tail_depth_at_cut + 1e-6:
        raise FinError(
            f"fin: root height {spec.root_height:.1f} mm exceeds the tail depth at the cut "
            f"({spec.tail_depth_at_cut:.1f} mm)"
        )
    if spec.slot_height > spec.root_height + 1e-6:
        raise FinError(
            f"fin: slot height {spec.slot_height:.1f} mm exceeds the fin root height "
            f"{spec.root_height:.1f} mm"
        )
    if spec.fork_depth >= spec.length:
        raise FinError("fin: fork_depth_mm must be shorter than the fin")
    if not spec.tab_x0 < spec.screw_x < spec.x_cut:
        raise FinError(
            f"fin: screw at x = {spec.screw_x:.1f} mm is not on the tab "
            f"({spec.tab_x0:.1f}–{spec.x_cut:.1f} mm)"
        )
    margin = 0.5 * max(spec.pilot_d, spec.tab_hole_d)
    if abs(spec.screw_z - spec.z_axis) + margin >= 0.5 * spec.tab_height:
        raise FinError("fin: the screw hole leaves the tab (screw.z_offset_mm too large)")
    if 2 * spec.slot_corner_r > min(spec.slot_depth, spec.slot_height):
        raise FinError(
            f"fin: slot corner radius {spec.slot_corner_r:.2f} mm too big for a "
            f"{spec.slot_depth} × {spec.slot_height:.1f} mm slot"
        )


# --- shapes -----------------------------------------------------------------------------


def outline_face(spec: FinSpec) -> Face:
    """Side-view outline in the xz plane (y = 0), trailing corners rounded."""
    x0, x1, cz = spec.x_cut, spec.x_tip, spec.z_axis
    h, s = spec.root_height / 2, spec.span / 2
    pts = [(x0, 0, cz - h), (x1, 0, cz - s)]
    if spec.fork_depth > 0:
        pts.append((x1 - spec.fork_depth, 0, cz))
    pts += [(x1, 0, cz + s), (x0, 0, cz + h)]
    face = Face(Polyline(*pts, close=True))
    if spec.corner_r > 0:
        corners = [v for v in face.vertices() if abs(v.X - x1) < 1e-6]
        r = min(spec.corner_r, _max_corner_r(spec))
        if r > 0.05:
            rounded = fillet(corners, r)
            face = rounded if isinstance(rounded, Face) else rounded.faces()[0]
    return face


def _max_corner_r(spec: FinSpec) -> float:
    """A safe trailing-corner radius: well inside the shortest adjacent edge."""
    dz = (spec.span - spec.root_height) / 2
    edge = math.hypot(spec.length, dz)
    tip = spec.span / 2 if spec.fork_depth <= 0 else math.hypot(spec.fork_depth, spec.span / 2)
    return 0.4 * min(edge, tip)


def _rounded_box(x0: float, x1: float, width: float, z: float, height: float, r: float) -> Solid:
    """Box x0..x1 × width (y, centred) × height (z, centred at z), side-view corners r."""
    box = Pos(0.5 * (x0 + x1), 0, z) * Box(x1 - x0, width, height)
    r = min(r, 0.499 * min(x1 - x0, height))
    return box.fillet(r, box.edges().filter_by(Axis.Y)) if r > 1e-3 else box


def _y_cylinder(x: float, z: float, d: float, length: float) -> Solid:
    return Pos(x, 0, z) * Rot(90, 0, 0) * Cylinder(d / 2, length)


def slot_solid(spec: FinSpec, grow: float = 0.0) -> Solid:
    """The slot (open past the cut face); `grow` dilates it (cavity keep-out)."""
    return _rounded_box(
        spec.slot_x0 - grow,
        spec.x_cut + SLOT_RUNOUT,
        spec.slot_width + 2 * grow,
        spec.z_axis,
        spec.slot_height + 2 * grow,
        spec.slot_corner_r + grow,
    )


def screw_hole(spec: FinSpec, through: float, grow: float = 0.0) -> Solid:
    """Pilot hole along y through the tail (`through` = its full length in y)."""
    return _y_cylinder(spec.screw_x, spec.screw_z, spec.pilot_d + 2 * grow, through)


def cut_box(spec: FinSpec, bbox, grow: float = 0.0) -> Solid:
    """Everything at x ≥ x_cut − grow (the tail tip the fin replaces)."""
    x0 = spec.x_cut - grow
    x1 = max(bbox.max.X, spec.tail_end_x) + 10.0
    c = bbox.center()
    return Pos(0.5 * (x0 + x1), c.Y, c.Z) * Box(x1 - x0, bbox.size.Y + 40.0, bbox.size.Z + 40.0)


def keepouts(spec: FinSpec, bbox, grow: float) -> list[Solid]:
    """Cavity keep-outs, each dilated by `grow`: the cut end face, the slot and the screw
    hole (the cavity may wrap above and below the slot)."""
    through = bbox.size.Y + 10.0
    return [cut_box(spec, bbox, grow), slot_solid(spec, grow), screw_hole(spec, through, grow)]


def _single(shape, what: str) -> Solid:
    solids = shape.solids()
    if len(solids) != 1:
        raise FinError(f"{what}: expected 1 solid, got {len(solids)}")
    return solids[0]


def fin_body(spec: FinSpec) -> Solid:
    """Paddle plus root tab, without the screw hole."""
    paddle = _single(extrude(outline_face(spec), amount=spec.thickness / 2, both=True), "fin")
    r = spec.tab_corner_r
    # the tab reaches r into the paddle so its rear rounded corners are buried
    tab = _rounded_box(
        spec.tab_x0, spec.x_cut + r + 0.5, spec.thickness, spec.z_axis, spec.tab_height, r
    )
    body = _single((paddle + tab).clean(), "fin with tab")
    assert_valid(body, solids=1, shells=1, what="fin with tab")
    return body


def fin_solid(spec: FinSpec) -> Solid:
    hole = _y_cylinder(spec.screw_x, spec.screw_z, spec.tab_hole_d, spec.thickness + 4.0)
    fin = _single(fin_body(spec) - hole, "fin screw hole")
    assert_valid(fin, solids=1, shells=1, what="fin with screw hole")
    return fin


def cut_tail_end(body: Solid, spec: FinSpec) -> Solid:
    """Cut the tail tip off at x_cut, cut the fin slot and the lateral screw hole."""
    bbox = body.bounding_box()
    cut = _single(body - cut_box(spec, bbox), "tail cut")
    assert_valid(cut, solids=1, shells=1, what="body after the tail cut")
    cut = _single(cut - slot_solid(spec), "fin slot")
    assert_valid(cut, solids=1, shells=1, what="body after the fin slot")
    through = bbox.size.Y + 10.0
    cut = _single(cut - screw_hole(spec, through), "fin screw hole")
    assert_valid(cut, solids=1, shells=1, what="body after the fin screw hole")
    return cut


def volumes(spec: FinSpec, envelope: Solid) -> tuple[float, float]:
    """(tail tip volume removed by the cut, fin volume outside the tail) in mm³.

    The tab replaces tail material in the slot nearly one for one, so it is left out."""
    tip = sum(s.volume for s in (envelope & cut_box(spec, envelope.bounding_box())).solids())
    paddle = extrude(outline_face(spec), amount=spec.thickness / 2, both=True)
    return float(tip), float(paddle.volume)


# --- checks -----------------------------------------------------------------------------


def slot_walls(spec: FinSpec, table: StationTable, shape: str) -> dict[str, float]:
    """Smallest solid beside (y) and above/below (z) the slot over its depth, from the
    station outline."""
    import numpy as np

    from luregen.geometry.pockets import _interp, half_width_at

    side, vertical = math.inf, math.inf
    z_lo = spec.z_axis - spec.slot_height / 2
    z_hi = spec.z_axis + spec.slot_height / 2
    for x in np.linspace(spec.slot_x0, spec.x_cut, SKIN_SAMPLES):
        x = float(x)
        cz, v, _ = _interp(table, x)
        for z in (z_lo, spec.z_axis, z_hi):
            side = min(side, half_width_at(table, shape, x, z) - spec.slot_width / 2)
        vertical = min(vertical, (cz + v) - z_hi, z_lo - (cz - v))
    return {"side_mm": round(float(side), 3), "vertical_mm": round(float(vertical), 3)}


def local_width(spec: FinSpec, table: StationTable, shape: str) -> float:
    """Body width (y) along the screw axis."""
    from luregen.geometry.pockets import half_width_at

    return 2 * half_width_at(table, shape, spec.screw_x, spec.screw_z)
