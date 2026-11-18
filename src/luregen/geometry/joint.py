r"""V-joint split of the body into head and tail, with the hinge pin bore and eye slots.

Top view (XY plane), x toward the tail:

          head          gap          tail
    ───────────────\    \  \ ─────────────────
                    \    \  \
                     >    >  >  ← apexes on y = 0
                    /    /  /
    ───────────────/    /  /─────────────────

- The head ends in a male V whose arms make `angle_deg` with the lateral (y) axis; the tail
  starts with the matching notch, offset by `gap_mm` measured normal to the arms. Both are
  prisms through all z, so the joint faces are vertical.
- The tail notch is the head V offset by the gap, so its (concave) apex radius is the head
  apex radius + gap. For CNC that radius must be at least the smallest tool radius; the head
  apex is rounded just enough to guarantee it.
- The vertical hinge pin bore runs through the tail, `pin_offset_mm` behind the tail apex.
  The head's hinge eyes (screwed into its V face at `hinge_eye_z`) reach across the gap into
  horizontal slots in the tail, and the pin passes through them.
- The line tie sits on the nose at `line_tie_z`.

Pin bore and eye slots are cut on the full body before the split, then the body is split by
two boolean intersections. Every result is checked with `assert_valid`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
from build123d import Box, Cylinder, Face, Location, Polyline, Pos, Solid, extrude, fillet, offset

from luregen.geometry.validate import GeometryError, assert_valid
from luregen.profile.table import StationTable

PRISM_MARGIN = 20.0  # mm beyond the body in y and z for the cutting prisms


class JointError(GeometryError):
    pass


@dataclass(frozen=True)
class JointSpec:
    """Resolved joint dimensions in body coordinates (mm, degrees)."""

    x_joint: float  # gap centre on y = 0
    gap: float
    angle_deg: float  # arm angle from the lateral (y) axis
    head_apex_r: float  # convex apex radius of the head V (0 = sharp)
    pin_offset: float  # pin axis behind the tail apex
    pin_bore_d: float
    hinge_eye_z: tuple[float, ...]
    eye_slot_w: float  # slot width in y
    eye_slot_h: float  # slot height in z
    line_tie_z: float

    @property
    def half_shift(self) -> float:
        """x distance from the gap centre to each apex (gap measured normal to the arms)."""
        return self.gap / (2 * math.cos(math.radians(self.angle_deg)))

    @property
    def head_apex_x(self) -> float:
        return self.x_joint - self.half_shift

    @property
    def tail_apex_x(self) -> float:
        return self.x_joint + self.half_shift

    @property
    def tail_notch_r(self) -> float:
        return self.head_apex_r + self.gap

    @property
    def pin_x(self) -> float:
        return self.tail_apex_x + self.pin_offset


def smallest_tool_radius(tool_ids: list[str]) -> float:
    """Smallest cutter radius from tool ids named `<shape>_<diameter mm>` (dfm.cnc.tools)."""
    radii = []
    for tool in tool_ids:
        m = re.search(r"_(\d+(?:\.\d+)?)$", tool)
        if m:
            radii.append(float(m.group(1)) / 2)
    if not radii:
        raise JointError(f"no tool diameter found in dfm.cnc.tools {tool_ids!r}")
    return min(radii)


def head_apex_radius(gap: float, method: str, tool_ids: list[str]) -> float:
    """Head apex radius so that the tail notch radius (apex r + gap) ≥ the CNC tool radius."""
    if method != "cnc":
        return 0.0
    return max(0.0, smallest_tool_radius(tool_ids) - gap)


def line_tie_location(table: StationTable, z: float) -> tuple[float, float]:
    """(x, z) of the line tie: the first point on the nose outline at height `z`."""
    xs = np.concatenate([[table.nose[0]], table.x])
    lo = np.concatenate([[table.nose[1]], table.center_z - table.v_radius])
    hi = np.concatenate([[table.nose[1]], table.center_z + table.v_radius])
    for i in range(1, len(xs)):
        if lo[i] <= z <= hi[i]:
            # interpolate the crossing of the outline edge that brackets z
            edge = lo if z < table.nose[1] else hi
            a, b = edge[i - 1], edge[i]
            t = 0.0 if abs(b - a) < 1e-12 else min(max((z - a) / (b - a), 0.0), 1.0)
            return float(xs[i - 1] + t * (xs[i] - xs[i - 1])), float(z)
    raise JointError(f"line tie z = {z} is outside the body outline")


def hinge_eye_locations(spec: JointSpec) -> list[tuple[float, float]]:
    """(x, z) where each head hinge eye enters the head's V face (its shank runs forward
    into a solid boss; its loop reaches across the gap to the pin)."""
    return [(spec.head_apex_x, z) for z in spec.hinge_eye_z]


# --- cutting prisms --------------------------------------------------------------------


def _head_face(spec: JointSpec, x_front: float, half_y: float) -> Face:
    """Planar XY face of everything on the head side of the head V (z = 0 plane)."""
    t = math.tan(math.radians(spec.angle_deg))
    xa = spec.head_apex_x
    pts = [
        (xa, 0.0),
        (xa - half_y * t, half_y),
        (x_front, half_y),
        (x_front, -half_y),
        (xa - half_y * t, -half_y),
    ]
    face = Face(Polyline(*pts, close=True))
    if spec.head_apex_r > 0:
        apex = [v for v in face.vertices() if abs(v.X - xa) < 1e-9 and abs(v.Y) < 1e-9]
        face = fillet(apex, spec.head_apex_r)
        if not isinstance(face, Face):
            face = face.faces()[0]
    return face


def _prism(face: Face, z0: float, z1: float) -> Solid:
    solid = extrude(face.moved(Location((0, 0, z0))), amount=z1 - z0)
    return solid.solids()[0]


def joint_prisms(spec: JointSpec, bbox) -> tuple[Solid, Solid]:
    """(head prism, head prism grown by the gap) covering the body's y/z extent."""
    half_y = max(abs(bbox.min.Y), abs(bbox.max.Y)) + PRISM_MARGIN
    x_front = bbox.min.X - PRISM_MARGIN
    z0, z1 = bbox.min.Z - PRISM_MARGIN, bbox.max.Z + PRISM_MARGIN
    head = _head_face(spec, x_front, half_y)
    grown = offset(head, spec.gap, kind=_arc_kind())
    grown_face = grown if isinstance(grown, Face) else grown.faces()[0]
    return _prism(head, z0, z1), _prism(grown_face, z0, z1)


def _arc_kind():
    from build123d import Kind

    return Kind.ARC


def shifted_head_prism(spec: JointSpec, bbox, shift: float) -> Solid:
    """The head prism moved forward so its V face lies `shift` mm (normal) inside the head."""
    c = math.cos(math.radians(spec.angle_deg))
    head, _ = joint_prisms(spec, bbox)
    return head.moved(Location((-shift / c, 0, 0)))


def shifted_tail_prism(spec: JointSpec, bbox, shift: float) -> Solid:
    """Everything `shift` mm (normal) or more behind the tail's V face."""
    c = math.cos(math.radians(spec.angle_deg))
    _, grown = joint_prisms(spec, bbox)
    grown = grown.moved(Location((shift / c, 0, 0)))
    big = _bbox_box(bbox, 2 * PRISM_MARGIN)  # past the prism, so no coplanar slivers
    return _single(big - grown, "tail prism")


def _bbox_box(bbox, margin: float) -> Solid:
    size = (bbox.size.X + 2 * margin, bbox.size.Y + 2 * margin, bbox.size.Z + 2 * margin)
    c = bbox.center()
    return Pos(c.X, c.Y, c.Z) * Box(*size)


def _single(shape, what: str) -> Solid:
    solids = shape.solids()
    if len(solids) != 1:
        raise JointError(f"{what}: expected 1 solid, got {len(solids)}")
    return solids[0]


def pin_bore(spec: JointSpec, bbox, grow: float = 0.0) -> Solid:
    """Vertical through-bore for the hinge pin; `grow` dilates it (cavity keep-out)."""
    z0, z1 = bbox.min.Z - 5, bbox.max.Z + 5
    r = spec.pin_bore_d / 2 + grow
    return Pos(spec.pin_x, 0, 0.5 * (z0 + z1)) * Cylinder(r, z1 - z0)


def eye_slots(spec: JointSpec, grow: float = 0.0) -> list[Solid]:
    """Horizontal slots in the tail front for the head hinge eye loops (one per z).

    `grow` dilates them (cavity keep-out); the front end stays in the gap.
    """
    x0 = spec.head_apex_x  # start inside the gap so the slot opens onto the tail face
    x1 = spec.pin_x + spec.eye_slot_h / 2 + grow  # loop wraps the pin
    w, h = spec.eye_slot_w + 2 * grow, spec.eye_slot_h + 2 * grow
    return [Pos(0.5 * (x0 + x1), 0, z) * Box(x1 - x0, w, h) for z in spec.hinge_eye_z]


# --- split -----------------------------------------------------------------------------


@dataclass
class SplitResult:
    head: Solid
    tail: Solid
    gap_volume: float  # body volume removed by the gap wedge (mm³)
    feature_volume: float  # volume removed by the pin bore and eye slots (mm³)


def split_body(body: Solid, spec: JointSpec) -> SplitResult:
    """Cut the pin bore on the full body, split it by the V gap, then cut the eye slots.

    The eye slots are cut from the tail after the split: cutting them on the full body
    first leaves an invalid tail when the notch apex is rounded (CNC), although each
    boolean on its own is valid.
    """
    v0 = body.volume
    bbox = body.bounding_box()
    cut = _single(body - pin_bore(spec, bbox), "pin bore cut")
    assert_valid(cut, solids=1, shells=1, what="body after pin bore cut")
    head_prism, grown = joint_prisms(spec, bbox)
    head = _single(cut & head_prism, "head split")
    assert_valid(head, solids=1, shells=1, what="head after split")
    tail = _single(cut - grown, "tail split")
    assert_valid(tail, solids=1, shells=1, what="tail after split")
    gap_volume = cut.volume - head.volume - tail.volume
    if gap_volume <= 0:
        raise JointError(f"split removed no gap volume ({gap_volume:.3f} mm³)")
    for slot in eye_slots(spec):
        tail = _single(tail - slot, "eye slot cut")
        assert_valid(tail, solids=1, shells=1, what="tail after eye slot cut")
    feature_volume = v0 - gap_volume - head.volume - tail.volume
    return SplitResult(head, tail, gap_volume, feature_volume)


def gap_wedge(body: Solid, spec: JointSpec) -> Solid:
    """The body material inside the gap (for analytic checks)."""
    head_prism, grown = joint_prisms(spec, body.bounding_box())
    return _single((grown - head_prism) & body, "gap wedge")
