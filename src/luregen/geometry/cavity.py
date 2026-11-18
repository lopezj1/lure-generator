"""Sealed print cavities: an explicit inset loft per section, never `offset_3d`/Shell.

Each cavity section is the envelope's own unit section scaled to
(half_width − wall, v_radius − wall) at that station, with its top pulled in to a roof
that needs no support when printed upright (z up):

- `gable45`: two 45° planes meeting at a ridge on y = 0 at the inset top.
- `teardrop`: an inverted teardrop (point up, 45° at the tip, curving to vertical).

The loft runs through the mid stations only (where the inset is at least `MIN_INSET_MM`)
and is closed by flat end caps. Every section spline uses the same sample count and uniform
parameters, the F2 rule that keeps lofts from failing.

Per print section the cavity is then clipped to: above the solid keel (`keel_top_z`),
`wall` behind the section's joint face, and outside every keep-out (pockets, pin bore, eye
slots dilated by the clearance; screw-eye bosses). If keep-outs split it, only the largest
piece is kept, so each section stays 1 solid with 2 shells.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from build123d import Box, Edge, Pos, Solid, Wire

from luregen.geometry.envelope import SectionShape, section_profile
from luregen.geometry.validate import GeometryError, assert_valid
from luregen.profile.table import StationTable

RoofType = Literal["gable45", "teardrop"]
CAVITY_SAMPLES = 64  # per section; even so the top (sample 0) and bottom are samples
MIN_INSET_MM = 1.5  # smallest inset half-width / v_radius kept in the cavity loft
MIN_STATIONS = 3


class CavityError(GeometryError):
    pass


def _teardrop_up(n: int = 4001) -> np.ndarray:
    """Unit inverted teardrop (point at z = +1, 45° at the tip), max |y| = 1."""
    theta = np.linspace(0.0, 2 * np.pi, n)
    y = np.sin(theta) * np.abs(np.sin(theta / 2))
    return np.column_stack([y / np.abs(y).max(), np.cos(theta)])


def _polar(yz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Angle from +z (toward +y) and radius of points about the origin."""
    return np.arctan2(yz[:, 0], yz[:, 1]), np.hypot(yz[:, 0], yz[:, 1])


def roof_section(yz: np.ndarray, hw: float, v: float, roof: RoofType) -> np.ndarray:
    """Scale unit section `yz` to (hw, v) and pull points above the roof radially inward.

    Points stay in order with the same count, so every section keeps its seam at the top.
    """
    pts = yz * np.array([hw, v])
    ang, r = _polar(pts)
    if roof == "gable45":
        upper = pts[:, 1] > 0
        limit = np.full(len(pts), np.inf)
        reach = pts[upper, 1] + np.abs(pts[upper, 0])  # z + |y| along the ray, scaled by r
        limit[upper] = r[upper] * v / np.maximum(reach, 1e-12)
    elif roof == "teardrop":
        td = _teardrop_up() * np.array([hw, v])
        td_ang, td_r = _polar(td)
        order = np.argsort(td_ang)
        limit = np.interp(ang, td_ang[order], td_r[order], period=2 * np.pi)
    else:
        raise CavityError(f"unknown cavity roof {roof!r}")
    scale = np.minimum(1.0, limit / np.maximum(r, 1e-12))
    return pts * scale[:, None]


def cavity_wire(
    x: float, cz: float, v: float, hw: float, shape: SectionShape, roof: RoofType
) -> Wire:
    yz = roof_section(section_profile(shape, CAVITY_SAMPLES), hw, v, roof)
    pts = [(x, float(y), cz + float(z)) for y, z in yz]
    params = [float(u) for u in np.linspace(0.0, 1.0, CAVITY_SAMPLES + 1)]
    return Wire([Edge.make_spline(pts, periodic=True, parameters=params)])


def inset_stations(table: StationTable, wall: float) -> np.ndarray:
    """Indices of the contiguous mid stations whose inset is at least MIN_INSET_MM."""
    ok = (table.half_width - wall >= MIN_INSET_MM) & (table.v_radius - wall >= MIN_INSET_MM)
    idx = np.flatnonzero(ok)
    if len(idx) < MIN_STATIONS:
        raise CavityError(f"wall {wall} mm leaves fewer than {MIN_STATIONS} cavity stations")
    # longest contiguous run
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
    return max(runs, key=len)


def inset_loft(
    table: StationTable,
    wall: float,
    shape: SectionShape = "ellipse",
    roof: RoofType = "gable45",
    x_range: tuple[float, float] | None = None,
) -> Solid:
    """Explicit inset loft (flat end caps) through the mid stations, with a roof."""
    idx = inset_stations(table, wall)
    if x_range is not None:
        lo, hi = x_range
        inside = idx[(table.x[idx] >= lo) & (table.x[idx] <= hi)]
        # keep one station beyond each end so the section clip, not the cap, bounds it
        first = max(int(idx[0]), int(inside[0]) - 1) if len(inside) else int(idx[0])
        last = min(int(idx[-1]), int(inside[-1]) + 1) if len(inside) else int(idx[-1])
        idx = np.arange(first, last + 1)
    if len(idx) < 2:
        raise CavityError("cavity loft needs at least two stations in range")
    wires = [
        cavity_wire(
            table.x[i],
            table.center_z[i],
            table.v_radius[i] - wall,
            table.half_width[i] - wall,
            shape,
            roof,
        )
        for i in idx
    ]
    solid = Solid.make_loft(wires)
    assert_valid(solid, solids=1, shells=1, what="cavity inset loft")
    return solid


@dataclass
class CavityResult:
    section: Solid  # the section with its sealed cavity
    cavity: Solid  # the void as a solid (for volume / centroid)
    dropped_mm3: float  # cavity pieces discarded because keep-outs split the void


def keel_box(bbox, keel_top_z: float) -> Solid:
    """Everything above `keel_top_z` within the body's bbox (+ margin)."""
    m = 10.0
    z0, z1 = keel_top_z, bbox.max.Z + m
    return Pos(bbox.center().X, 0, 0.5 * (z0 + z1)) * Box(
        bbox.size.X + 2 * m, bbox.size.Y + 2 * m, z1 - z0
    )


def clip_cavity(
    loft: Solid, keep: list[Solid], keepouts: list[Solid], what: str
) -> tuple[Solid, float]:
    """loft ∩ every `keep` − every keep-out; return the largest piece and the dropped volume."""
    shape = loft
    for k in keep:
        shape = shape & k
        if not shape.solids():
            raise CavityError(f"{what}: cavity vanished after clipping (wall/keel too large?)")
    for k in keepouts:
        shape = shape - k
        if not shape.solids():
            raise CavityError(f"{what}: cavity vanished after keep-outs")
    solids = sorted(shape.solids(), key=lambda s: s.volume, reverse=True)
    dropped = sum(s.volume for s in solids[1:])
    cavity = solids[0]
    if len(cavity.shells()) != 1:
        raise CavityError(
            f"{what}: a keep-out lies entirely inside the cavity (it would float in the void)"
        )
    assert_valid(cavity, solids=1, shells=1, what=f"{what} cavity")
    return cavity, dropped


def hollow(section: Solid, cavity: Solid, what: str) -> Solid:
    """Section with a fully enclosed cavity: exactly 1 solid with 2 shells.

    The cavity must not touch the outer shell (check `min_skin` > 0 first). The result is
    assembled directly from the outer shell and the reversed cavity shell, which is exact
    and avoids a slow boolean on the lofted faces; a boolean cut is the fallback.
    """
    from build123d.topology.shape_core import downcast
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN

    probe = cavity.vertices()[0].center().to_pnt()
    inside = BRepClass3d_SolidClassifier(section.wrapped, probe, 1e-6)
    if inside.State() != TopAbs_IN:
        raise CavityError(f"{what}: cavity is not inside the section")
    maker = BRepBuilderAPI_MakeSolid(section.shells()[0].wrapped)
    maker.Add(downcast(cavity.shells()[0].wrapped.Reversed()))
    out = Solid(maker.Solid())
    expected = section.volume - cavity.volume
    if not out.is_valid or abs(out.volume - expected) > 1e-4 * section.volume:
        result = (section - cavity).solids()
        if len(result) != 1:
            raise CavityError(f"{what}: hollowing gave {len(result)} solids")
        out = result[0]
    assert_valid(out, solids=1, shells=2, what=f"{what} with sealed cavity")
    return out


def min_skin(section: Solid, cavity: Solid) -> float:
    """Smallest distance (mm) between the cavity surface and the section's outer surface.

    `section` is the section before hollowing (one shell); surfaces are compared, not
    solids, because a solid-to-solid distance is 0 when one contains the other.
    """
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    dist = BRepExtrema_DistShapeShape(section.shells()[0].wrapped, cavity.shells()[0].wrapped)
    if not dist.IsDone():
        raise CavityError("skin distance computation failed")
    return float(dist.Value())
