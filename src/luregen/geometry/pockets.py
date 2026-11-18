"""Ballast pocket, tail trim pocket and screw-eye bosses.

- **Ballast pocket** (head): a U-slot open at the belly, `2r` wide, with a horizontal
  cylinder of radius r along x on top. Its axis sits `z_above_belly` above the belly at
  `x_from_joint` ahead of the joint. The slot length is solved so the pocket volume inside
  the body equals the ballast volume (mass / density); the ballast fills it.
- **Trim pocket** (tail, aft): a vertical cylinder open at the belly, sized to hold the
  trim capacity in putty. The F4 balance step fills it halfway (nominal) to absorb the
  hardware mass band.
- **Bosses**: a sphere of solid material around each screw-eye position. Bosses are not
  cut; they are keep-outs subtracted from the cavity, so eyes always go into solid plastic.

Every pocket shape has a `dilated(d)` twin used as a cavity keep-out (clearance), built from
the same parameters with bigger dimensions, never by offsetting a solid.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from build123d import Box, Cylinder, Pos, Rot, Solid, Sphere

from luregen.geometry.envelope import SectionShape, section_profile
from luregen.geometry.validate import GeometryError
from luregen.profile.table import StationTable

SIZE_ITERATIONS = 6
SIZE_RTOL = 0.005


class PocketError(GeometryError):
    pass


# --- station-table helpers ------------------------------------------------------------


def _interp(table: StationTable, x: float) -> tuple[float, float, float]:
    """(center_z, v_radius, half_width) at x, interpolated (0 radius at the tips)."""
    xs = np.concatenate([[table.nose[0]], table.x, [table.tail[0]]])
    cz = np.concatenate([[table.nose[1]], table.center_z, [table.tail[1]]])
    v = np.concatenate([[0.0], table.v_radius, [0.0]])
    hw = np.concatenate([[0.0], table.half_width, [0.0]])
    return float(np.interp(x, xs, cz)), float(np.interp(x, xs, v)), float(np.interp(x, xs, hw))


def belly_z(table: StationTable, x: float) -> float:
    cz, v, _ = _interp(table, x)
    return cz - v


def half_width_at(table: StationTable, shape: SectionShape, x: float, z: float) -> float:
    """Body half-width at (x, z) for the envelope's section shape (0 outside the body)."""
    cz, v, hw = _interp(table, x)
    if v <= 0:
        return 0.0
    u = (z - cz) / v
    if abs(u) >= 1:
        return 0.0
    yz = section_profile(shape, 2000)
    right = yz[yz[:, 0] >= 0]
    order = np.argsort(right[:, 1])
    return float(hw * np.interp(u, right[order, 1], right[order, 0]))


# --- pocket shapes ----------------------------------------------------------------------


@dataclass(frozen=True)
class BallastPocket:
    x: float  # centre
    z_axis: float  # cylinder axis height
    r: float
    length: float  # straight length along x (cylinder length)
    z_floor: float  # bottom of the slot (below the belly)

    def solid(self, grow: float = 0.0) -> Solid:
        r, length = self.r + grow, self.length + 2 * grow
        z0 = self.z_floor - grow
        cyl = Pos(self.x, 0, self.z_axis) * Rot(0, 90, 0) * Cylinder(r, length)
        slot = Pos(self.x, 0, 0.5 * (z0 + self.z_axis)) * Box(length, 2 * r, self.z_axis - z0)
        return (cyl + slot).solids()[0]


@dataclass(frozen=True)
class TrimPocket:
    x: float
    r: float
    z_top: float
    z_floor: float  # below the belly

    def solid(self, grow: float = 0.0) -> Solid:
        z0, z1 = self.z_floor - grow, self.z_top + grow
        return Pos(self.x, 0, 0.5 * (z0 + z1)) * Cylinder(self.r + grow, z1 - z0)


def _inside(body: Solid, pocket: Solid) -> Solid | None:
    common = body & pocket
    return common.solids()[0] if common.solids() else None


def _solve(f, target: float, x0: float, x1: float) -> float:
    """Secant on a monotone f(x) = target, from two guesses; clamped to x > 0."""
    f0, f1 = f(x0) - target, f(x1) - target
    for _ in range(SIZE_ITERATIONS):
        if abs(f1) <= SIZE_RTOL * target:
            return x1
        if f1 == f0:
            break
        x0, x1, f0 = x1, max(1e-3, x1 - f1 * (x1 - x0) / (f1 - f0)), f1
        f1 = f(x1) - target
    return x1


def ballast_pocket(
    body: Solid,
    table: StationTable,
    shape: SectionShape,
    x: float,
    z_above_belly: float,
    volume_mm3: float,
    wall: float,
) -> BallastPocket:
    """Size the ballast U-slot at (x, belly + z_above_belly) to hold `volume_mm3`."""
    z_axis = belly_z(table, x) + z_above_belly
    r = min(half_width_at(table, shape, x, z_axis) - wall, z_above_belly)
    if r <= 0.5:
        raise PocketError(
            f"no room for ballast at x = {x:.1f}, z = {z_axis:.1f} with a {wall} mm wall"
        )
    z_floor = body.bounding_box().min.Z - 2.0
    base = BallastPocket(x, z_axis, r, 1.0, z_floor)

    def vol(length: float) -> float:
        common = _inside(body, replace(base, length=length).solid())
        return common.volume if common is not None else 0.0

    area = np.pi * r * r / 2 + 2 * r * z_above_belly  # rough section, for the first guesses
    guess = volume_mm3 / area
    length = _solve(vol, volume_mm3, guess, 1.2 * guess)
    return replace(base, length=length)


def trim_pocket(
    body: Solid, table: StationTable, shape: SectionShape, x: float, volume_mm3: float, wall: float
) -> TrimPocket:
    """Size a vertical trim cylinder at x, open at the belly, to hold `volume_mm3`."""
    zb = belly_z(table, x)
    cz, v, _ = _interp(table, x)
    r = min(4.0, half_width_at(table, shape, x, cz) - wall)
    if r <= 0.5:
        raise PocketError(f"no room for a trim pocket at x = {x:.1f} with a {wall} mm wall")
    z_floor = body.bounding_box().min.Z - 2.0
    base = TrimPocket(x, r, zb + 1.0, z_floor)

    def vol(height: float) -> float:
        common = _inside(body, replace(base, z_top=zb + height).solid())
        return common.volume if common is not None else 0.0

    guess = volume_mm3 / (np.pi * r * r) + 2.0
    height = _solve(vol, volume_mm3, guess, 1.2 * guess)
    if zb + height > cz + v - wall:
        raise PocketError(f"trim pocket at x = {x:.1f} does not fit ({height:.1f} mm tall)")
    return replace(base, z_top=zb + height)


def boss(x: float, z: float, r: float) -> Solid:
    """Keep-out sphere of solid material around a screw-eye position (y = 0)."""
    return Pos(x, 0, z) * Sphere(r)
