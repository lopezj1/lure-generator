"""Outer body envelope from a StationTable.

- `loft3d`: a section wire per station, lofted between nose and tail tip vertices.

Coordinates follow the package convention: x from the nose tip, z up, y lateral.
"""

from typing import Literal

import numpy as np
from build123d import Edge, Solid, Vertex, Wire

from luregen.geometry.validate import GeometryError, assert_valid
from luregen.profile.table import StationTable

SectionShape = Literal["ellipse", "egg", "teardrop"]
BodyStyle = Literal["loft3d", "profile25d"]

SECTION_SAMPLES = 32  # points per section spline; even, so top and bottom are samples
EGG_K = 0.25  # egg: half-width multiplier (1 + k·cosθ), wider at the back than the belly
TEARDROP_P = 1.0  # teardrop: exponent on cos(θ/2), > 0 gives a keel point at the belly
TWIST_TOL = 0.05  # relative loft-volume error that flags a twisted or collapsed loft


class EnvelopeError(GeometryError):
    pass


# --- sections -------------------------------------------------------------------------


def _unit_y(shape: SectionShape, theta: np.ndarray) -> np.ndarray:
    s, c = np.sin(theta), np.cos(theta)
    if shape == "ellipse":
        return s
    if shape == "egg":
        return s * (1 + EGG_K * c)
    if shape == "teardrop":
        return s * np.abs(np.cos(theta / 2)) ** TEARDROP_P
    raise EnvelopeError(f"unknown section shape {shape!r}")


def section_profile(shape: SectionShape, n: int = SECTION_SAMPLES) -> np.ndarray:
    """Unit section samples as (n, 2) [y, z] with max |y| = 1 and z = cos θ in [-1, 1].

    Sample 0 is the dorsal top (y = 0, z = 1) and the curve runs toward +y first, so every
    section of every shape shares its start point and direction.
    """
    theta = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    norm = np.abs(_unit_y(shape, np.linspace(0.0, 2 * np.pi, 2001))).max()
    return np.column_stack([_unit_y(shape, theta) / norm, np.cos(theta)])


def section_area_factor(shape: SectionShape) -> float:
    """Section area divided by (half_width · v_radius), from a dense polygon."""
    yz = section_profile(shape, 4000)
    y, z = yz[:, 0], yz[:, 1]
    return float(0.5 * abs(np.dot(y, np.roll(z, -1)) - np.dot(z, np.roll(y, -1))))


def section_wire(
    x: float,
    center_z: float,
    v_radius: float,
    half_width: float,
    shape: SectionShape = "ellipse",
    n: int = SECTION_SAMPLES,
) -> Wire:
    """One closed periodic-spline section in the plane at `x` (a single edge).

    Uniform parameters give every section the same knot vector. With the default
    chord-length parameters, egg and teardrop lofts between tip vertices fail
    (`StdFail_NotDone`) for some sample counts.
    """
    yz = section_profile(shape, n)
    pts = [(x, half_width * y, center_z + v_radius * z) for y, z in yz]
    params = [float(u) for u in np.linspace(0.0, 1.0, n + 1)]
    return Wire([Edge.make_spline(pts, periodic=True, parameters=params)])


def check_section_seams(wires: list[Wire], table: StationTable) -> None:
    """Raise unless all wires have the same edge count, a dorsal start and one direction."""
    counts = {len(w.edges()) for w in wires}
    if len(counts) != 1:
        raise EnvelopeError(f"loft sections have different edge counts: {sorted(counts)}")
    signs = set()
    for i, (w, cz, v) in enumerate(zip(wires, table.center_z, table.v_radius, strict=True)):
        start = w.edges()[0].position_at(0)
        if abs(start.Y) > 1e-6 * max(1.0, v) or start.Z - cz < 0.99 * v:
            raise EnvelopeError(f"section {i} does not start at its dorsal top: {start}")
        signs.add(np.sign(w.edges()[0].tangent_at(0).Y))
    if len(signs) != 1:
        raise EnvelopeError("loft sections run in different directions (the loft would twist)")


# --- loft3d ---------------------------------------------------------------------------


def expected_loft_volume(table: StationTable, shape: SectionShape) -> float:
    """Volume from integrating section areas along x (area 0 at the tips)."""
    k = section_area_factor(shape)
    xs = np.concatenate([[table.nose[0]], table.x, [table.tail[0]]])
    areas = np.concatenate([[0.0], k * table.v_radius * table.half_width, [0.0]])
    return float(np.trapezoid(areas, xs))


def loft3d(
    table: StationTable,
    shape: SectionShape = "ellipse",
    ruled: bool = False,
    twist_check: bool = True,
) -> Solid:
    """Loft section wires between nose and tail tip vertices into one valid solid.

    The twist check compares the loft volume with integrated section areas; a twisted
    loft pinches between stations and loses volume. Ruled lofts get a looser bound.
    """
    wires = [
        section_wire(x, cz, v, hw, shape)
        for x, cz, v, hw in zip(
            table.x, table.center_z, table.v_radius, table.half_width, strict=True
        )
    ]
    check_section_seams(wires, table)
    nose = Vertex(table.nose[0], 0.0, table.nose[1])
    tail = Vertex(table.tail[0], 0.0, table.tail[1])
    solid = Solid.make_loft([nose, *wires, tail], ruled=ruled)
    assert_valid(solid, solids=1, shells=1, what="loft3d envelope")
    if twist_check:
        expected = expected_loft_volume(table, shape)
        err = abs(solid.volume - expected) / expected
        tol = TWIST_TOL * (3 if ruled else 1)
        if err > tol:
            raise EnvelopeError(
                f"loft volume {solid.volume:.0f} mm³ differs from integrated sections "
                f"{expected:.0f} mm³ by {err:.1%} (> {tol:.0%}): twisted or collapsed loft"
            )
    return solid


# --- mass properties ------------------------------------------------------------------


def mass_properties(solid: Solid) -> dict:
    """Volume (mm³ and cm³), bounding box and centroid (mm) of a solid."""
    from build123d import CenterOf

    bb = solid.bounding_box()
    c = solid.center(CenterOf.MASS)
    return {
        "volume_mm3": round(solid.volume, 3),
        "volume_cm3": round(solid.volume / 1000.0, 5),
        "bbox_mm": {
            "min": [round(v, 3) for v in (bb.min.X, bb.min.Y, bb.min.Z)],
            "max": [round(v, 3) for v in (bb.max.X, bb.max.Y, bb.max.Z)],
            "size": [round(v, 3) for v in (bb.size.X, bb.size.Y, bb.size.Z)],
        },
        "centroid_mm": [round(v, 3) for v in (c.X, c.Y, c.Z)],
    }
