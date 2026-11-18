"""Mass properties per section and for the whole lure, from volumes, centroids and densities.

Pure numbers, no CAD: geometry code measures each part (volume, centroid) and passes it in.

- **Mass** of a section: Σ ρᵢVᵢ over its solid parts (body plastic, ballast, trim fill) plus
  hardware point masses.
- **Displacement** of a section: its outer envelope volume, i.e. the body solid + sealed
  cavity + filled pockets, plus each hardware item's own volume (m / ρ); submerged hooks
  displace too. Empty open pockets, the pin bore and eye slots are flooded and don't count.
- **CG** is the mass-weighted centroid; **CB** the centroid of the displaced volume.

Units: mm, mm³ and g in; reports add cm³, oz and g/cm³. Coordinates follow the package
convention (x from the nose, z up), so "CG − CB" fore/aft is `cg.x − cb.x` (positive: CG
behind CB) and "CB − CG" vertical is `cb.z − cg.z` (positive: self-righting).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from luregen.units import g_to_oz

Band = Literal["min", "mid", "max"]
BANDS: tuple[Band, ...] = ("min", "mid", "max")


@dataclass(frozen=True)
class Part:
    """A solid part: volume (mm³), centroid (mm), density (g/cm³; 0 for air/void).

    `displaces` says whether it counts toward the section's displaced volume. Every body
    part does (the cavity is a part with density 0); a flooded feature would not.
    """

    name: str
    volume_mm3: float
    centroid: tuple[float, float, float]
    density: float
    displaces: bool = True

    @property
    def mass_g(self) -> float:
        return self.density * self.volume_mm3 / 1000.0


@dataclass(frozen=True)
class PointMass:
    """Hardware: mass range [min, max] g of the whole item (qty included) at a position."""

    name: str
    mass_g: tuple[float, float]
    position: tuple[float, float, float]
    density: float  # g/cm³, for its displaced volume

    def mass(self, band: Band) -> float:
        lo, hi = self.mass_g
        return {"min": lo, "mid": 0.5 * (lo + hi), "max": hi}[band]


@dataclass(frozen=True)
class SectionInput:
    name: str
    parts: list[Part]
    hardware: list[PointMass] = field(default_factory=list)


@dataclass(frozen=True)
class MassProps:
    mass_g: float
    volume_mm3: float  # displaced
    cg: tuple[float, float, float]
    cb: tuple[float, float, float]

    @property
    def density(self) -> float:
        """Overall density in g/cm³ (mass over displaced volume)."""
        return 1000.0 * self.mass_g / self.volume_mm3

    @property
    def cg_minus_cb_x(self) -> float:
        return self.cg[0] - self.cb[0]

    @property
    def cb_minus_cg_z(self) -> float:
        return self.cb[2] - self.cg[2]

    def as_dict(self) -> dict:
        return {
            "mass_g": round(self.mass_g, 3),
            "mass_oz": round(g_to_oz(self.mass_g), 4),
            "volume_cm3": round(self.volume_mm3 / 1000.0, 4),
            "density": round(self.density, 4),
            "cg_mm": [round(v, 3) for v in self.cg],
            "cb_mm": [round(v, 3) for v in self.cb],
            "cg_minus_cb_x_mm": round(self.cg_minus_cb_x, 3),
            "cb_minus_cg_z_mm": round(self.cb_minus_cg_z, 3),
        }


def _weighted(items: Iterable[tuple[float, tuple[float, float, float]]]) -> tuple[float, tuple]:
    items = list(items)
    w = np.array([a for a, _ in items], dtype=float)
    p = np.array([c for _, c in items], dtype=float).reshape(-1, 3)
    total = float(w.sum())
    if total <= 0:
        raise ValueError("mass properties need a positive total weight")
    return total, tuple(float(v) for v in (w[:, None] * p).sum(axis=0) / total)


def section_props(section: SectionInput, band: Band = "mid") -> MassProps:
    """Mass, displaced volume, CG and CB of one section with hardware at `band`."""
    masses = [(p.mass_g, p.centroid) for p in section.parts if p.mass_g > 0]
    volumes = [(p.volume_mm3, p.centroid) for p in section.parts if p.displaces]
    for hw in section.hardware:
        m = hw.mass(band)
        masses.append((m, hw.position))
        volumes.append((1000.0 * m / hw.density, hw.position))
    mass, cg = _weighted(masses)
    volume, cb = _weighted(volumes)
    return MassProps(mass, volume, cg, cb)


def combine(props: Iterable[MassProps]) -> MassProps:
    """Whole-lure properties from per-section properties."""
    props = list(props)
    mass, cg = _weighted((p.mass_g, p.cg) for p in props)
    volume, cb = _weighted((p.volume_mm3, p.cb) for p in props)
    return MassProps(mass, volume, cg, cb)


@dataclass(frozen=True)
class LureProps:
    band: Band
    sections: dict[str, MassProps]
    lure: MassProps

    def ratio(self, num: str = "head", den: str = "tail") -> float:
        return self.sections[num].mass_g / self.sections[den].mass_g

    def as_dict(self) -> dict:
        out = {
            "band": self.band,
            "sections": {k: v.as_dict() for k, v in self.sections.items()},
            "lure": self.lure.as_dict(),
        }
        if {"head", "tail"} <= self.sections.keys():
            out["lure"]["head_tail_mass_ratio"] = round(self.ratio(), 4)
        return out


def lure_props(sections: list[SectionInput], band: Band = "mid") -> LureProps:
    per = {s.name: section_props(s, band) for s in sections}
    return LureProps(band, per, combine(per.values()))


def band_props(sections: list[SectionInput]) -> dict[Band, LureProps]:
    """Lure properties at the min, mid and max end of the hardware mass band."""
    return {b: lure_props(sections, b) for b in BANDS}
