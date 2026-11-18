"""Buoyancy targets, residuals, hardware band evaluation and trim putty sizing.

Pure numbers, no CAD. Mass properties are carried as additive **moments** (mass and its
first moment, displaced volume and its first moment), so a change to one part (ballast
grown, putty added, a hardware item at the other end of its band) is a plain sum.

Targets (salt water 1.025 by default) come from `[buoyancy]`:

- **hard**: overall density inside the sink band, level hang (fore/aft CG − CB within the
  tolerance). A build fails if either hardware band end misses one of them.
- **soft**: head and tail density bands, head:tail mass ratio (±10 %), self-righting
  (CB above CG). They are solved for in a weighted least-squares sense and reported.

The **hardware band**: the solver works at the band midpoint. Each end (all items at their
min or max mass) is then evaluated as built; if the tail trim pocket exists, the putty
correction that restores the mid density is sized ("add/remove X g of tungsten putty")
and the end is evaluated again with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np

from luregen.physics.massprops import (
    Band,
    LureProps,
    MassProps,
    Part,
    PointMass,
    SectionInput,
    combine,
)

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.profile.table import StationTable

RATIO_TOL = 0.10  # same as the report verdict
# Free zone (fraction of the half band) around hard targets: the hardware band ends move
# density and level by about this much, so the solve keeps the mid well inside.
HARD_DEADBAND = 0.25
EDGE_BANDS: tuple[Band, ...] = ("min", "max")

# Default least-squares weights (scaled residuals: 1.0 = at the edge of the target band).
# With the section volumes fixed by the envelope, the head density follows from the sink
# band, the tail density and the head:tail ratio, so it is redundant; when the targets
# conflict (see `feasibility`) it gives way and the ratio and the buoyant tail hold.
# Override per variant with `solver.weights`.
DEFAULT_WEIGHTS: dict[str, float] = {
    "sink": 30.0,
    "level": 30.0,
    "ballast_clearance": 30.0,
    "head_density": 0.2,
    "tail_density": 1.0,
    "head_tail_ratio": 1.0,
    "self_righting": 1.0,
}


# --- moments ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Moments:
    """Additive mass properties: mass (g), Σm·r (g·mm), displaced volume (mm³), ΣV·r."""

    mass: float = 0.0
    m1: tuple[float, float, float] = (0.0, 0.0, 0.0)
    vol: float = 0.0
    v1: tuple[float, float, float] = (0.0, 0.0, 0.0)

    VECTOR_LEN = 8

    def __add__(self, other: Moments) -> Moments:
        return Moments.from_vector(self.vector() + other.vector())

    def __sub__(self, other: Moments) -> Moments:
        return Moments.from_vector(self.vector() - other.vector())

    def scaled(self, k: float) -> Moments:
        return Moments.from_vector(k * self.vector())

    def vector(self) -> np.ndarray:
        return np.array([self.mass, *self.m1, self.vol, *self.v1], dtype=float)

    @classmethod
    def from_vector(cls, v: Iterable[float]) -> Moments:
        a = [float(x) for x in v]
        return cls(a[0], (a[1], a[2], a[3]), a[4], (a[5], a[6], a[7]))

    @classmethod
    def point(
        cls, mass: float, pos: Iterable[float], volume: float = 0.0, vpos: Iterable[float] = None
    ) -> Moments:
        p = np.asarray(list(pos), dtype=float)
        q = p if vpos is None else np.asarray(list(vpos), dtype=float)
        return cls(mass, tuple(mass * p), volume, tuple(volume * q))

    @classmethod
    def from_parts(cls, parts: Iterable[Part]) -> Moments:
        out = cls()
        for p in parts:
            out = out + cls.point(p.mass_g, p.centroid, p.volume_mm3 if p.displaces else 0.0)
        return out

    @classmethod
    def from_hardware(cls, hardware: Iterable[PointMass], band: Band) -> Moments:
        out = cls()
        for hw in hardware:
            m = hw.mass(band)
            out = out + cls.point(m, hw.position, 1000.0 * m / hw.density)
        return out

    def props(self) -> MassProps:
        if self.mass <= 0 or self.vol <= 0:
            raise ValueError("mass properties need a positive mass and volume")
        cg = tuple(c / self.mass for c in self.m1)
        cb = tuple(c / self.vol for c in self.v1)
        return MassProps(self.mass, self.vol, cg, cb)


def section_moments(sections: list[SectionInput]) -> dict[str, Moments]:
    """Per-section moments of the solid parts only (hardware is added per band)."""
    return {s.name: Moments.from_parts(s.parts) for s in sections}


def hardware_moments(sections: list[SectionInput], band: Band) -> dict[str, Moments]:
    return {s.name: Moments.from_hardware(s.hardware, band) for s in sections}


def lure_props_from_moments(per_section: dict[str, Moments], band: Band) -> LureProps:
    props = {k: m.props() for k, m in per_section.items()}
    return LureProps(band, props, combine(props.values()))


# --- trim putty ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrimSpec:
    """The tail trim pocket: a vertical cylinder open at the belly, putty packed from the
    closed top down. `fill_g` is the nominal (band-mid) fill."""

    section: str
    x: float
    r: float
    z_top: float
    capacity_g: float
    density: float
    material: str
    fill_g: float = 0.0

    @classmethod
    def from_report(cls, block: dict | None, section: str = "tail") -> TrimSpec | None:
        if not block:
            return None
        return cls(
            section=block.get("section", section),
            x=block["x_mm"],
            r=block["r_mm"],
            z_top=block["z_top_mm"],
            capacity_g=block["capacity_g"],
            density=block["density"],
            material=block["material"],
            fill_g=block.get("fill_g", 0.0),
        )

    def column(self, fill_g: float) -> Moments:
        """Moments of `fill_g` putty in the pocket (it displaces its own volume)."""
        if fill_g <= 0:
            return Moments()
        vol = 1000.0 * fill_g / self.density
        h = vol / (np.pi * self.r**2)
        c = (self.x, 0.0, self.z_top - h / 2)
        return Moments.point(fill_g, c, vol)

    def part(self, fill_g: float) -> Part | None:
        if fill_g <= 0:
            return None
        m = self.column(fill_g)
        return Part("trim_fill", m.vol, m.props().cg, self.density)


def putty_to_density(m: Moments, rho_target: float, rho_putty: float) -> float:
    """Putty mass Δ (g; negative = remove) so that (m + Δ) / (V + Δ/ρp) = ρ_target."""
    return (rho_target * m.vol / 1000.0 - m.mass) / (1.0 - rho_target / rho_putty)


# --- targets and residuals ----------------------------------------------------------------


@dataclass(frozen=True)
class Targets:
    water: str
    water_density: float
    sink_mode: str
    sink: tuple[float, float]
    head_density: tuple[float, float]
    tail_density: tuple[float, float]
    ratio: float
    level_tol: float
    righting_mm: float
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @property
    def sink_mid(self) -> float:
        return 0.5 * (self.sink[0] + self.sink[1])

    @classmethod
    def from_config(cls, cfg: LureConfig, table: StationTable) -> Targets:
        b = cfg.buoyancy
        weights = dict(DEFAULT_WEIGHTS)
        solver = getattr(cfg, "solver", None)
        if solver is not None:
            weights.update(solver.weights)
        return cls(
            water=b.water,
            water_density=float(b.water_density or 1.025),
            sink_mode=b.target_sink,
            sink=tuple(b.sink_bands[b.target_sink]),
            head_density=tuple(b.head_density),
            tail_density=tuple(b.tail_density),
            ratio=b.head_tail_mass_ratio,
            level_tol=b.level_tolerance_mm,
            righting_mm=b.cb_above_cg_frac * 2 * float(table.v_radius.max()),
            weights=weights,
        )


Side = Literal["both", "min", "max"]


@dataclass(frozen=True)
class Residual:
    """value vs target; `scale` maps the allowed deviation to 1.0. One-sided residuals
    (`side` = "min": value ≥ target; "max": value ≤ target) are 0 when satisfied; the
    solver aims `margin` past the target so model error doesn't land it just outside.

    `penalty` is what the solver minimizes: `scaled` with a dead band of `deadband` around
    the target (hard targets: anywhere in the middle of the band is as good, which leaves
    the soft targets free) and the margin on one-sided targets."""

    id: str
    label: str
    value: float
    target: float
    scale: float
    weight: float
    hard: bool
    side: Side = "both"
    unit: str = ""
    margin: float = 0.0
    deadband: float = 0.0
    penalty_value: float | None = None  # a smooth penalty supplied by the caller

    @property
    def scaled(self) -> float:
        if self.side == "min":
            return min((self.value - self.target) / self.scale, 0.0)
        if self.side == "max":
            return max((self.value - self.target) / self.scale, 0.0)
        return (self.value - self.target) / self.scale

    @property
    def penalty(self) -> float:
        if self.penalty_value is not None:
            return self.penalty_value
        if self.side == "min":
            return min((self.value - self.target - self.margin) / self.scale, 0.0)
        if self.side == "max":
            return max((self.value - self.target + self.margin) / self.scale, 0.0)
        d = self.scaled
        return float(np.sign(d) * max(abs(d) - self.deadband, 0.0))

    @property
    def ok(self) -> bool:
        if self.side == "min":
            return self.value >= self.target - 1e-9
        if self.side == "max":
            return self.value <= self.target + 1e-9
        return abs(self.scaled) <= 1.0 + 1e-9

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "value": round(self.value, 4),
            "target": round(self.target, 4),
            "scaled": round(self.scaled, 4),
            "hard": self.hard,
            "ok": self.ok,
        }


def _band(lo_hi: tuple[float, float]) -> tuple[float, float]:
    lo, hi = lo_hi
    return 0.5 * (lo + hi), 0.5 * (hi - lo)


def residuals(props: LureProps, t: Targets, extra: Iterable[Residual] = ()) -> list[Residual]:
    """Scaled residuals of one band's lure properties against the targets."""
    w = t.weights
    lure = props.lure
    sink_mid, sink_half = _band(t.sink)
    hd_mid, hd_half = _band(t.head_density)
    td_mid, td_half = _band(t.tail_density)
    out = [
        Residual(
            "sink",
            "Overall density",
            lure.density,
            sink_mid,
            sink_half,
            w["sink"],
            True,
            unit="g/cm³",
            deadband=HARD_DEADBAND,
        ),
        Residual(
            "level",
            "CG − CB fore/aft",
            lure.cg_minus_cb_x,
            0.0,
            t.level_tol,
            w["level"],
            True,
            unit="mm",
            deadband=HARD_DEADBAND,
        ),
    ]
    if {"head", "tail"} <= props.sections.keys():
        head, tail = props.sections["head"], props.sections["tail"]
        out += [
            Residual(
                "head_density",
                "Head density",
                head.density,
                hd_mid,
                hd_half,
                w["head_density"],
                False,
                unit="g/cm³",
            ),
            Residual(
                "tail_density",
                "Tail density",
                tail.density,
                td_mid,
                td_half,
                w["tail_density"],
                False,
                unit="g/cm³",
            ),
            Residual(
                "head_tail_ratio",
                "Head:tail mass ratio",
                props.ratio(),
                t.ratio,
                t.ratio * RATIO_TOL,
                w["head_tail_ratio"],
                False,
            ),
        ]
    out.append(
        Residual(
            "self_righting",
            "CB above CG",
            lure.cb_minus_cg_z,
            t.righting_mm,
            max(t.righting_mm, 1.0),
            w["self_righting"],
            False,
            side="min",
            unit="mm",
        )
    )
    return out + list(extra)


def weighted_vector(res: Iterable[Residual]) -> np.ndarray:
    return np.array([r.weight * r.penalty for r in res], dtype=float)


def cost(res: Iterable[Residual]) -> float:
    return float(0.5 * np.sum(weighted_vector(res) ** 2))


def hard_ok(res: Iterable[Residual]) -> bool:
    return all(r.ok for r in res if r.hard)


# --- hardware band ------------------------------------------------------------------------


def band_evaluation(sections: list[SectionInput], t: Targets, trim: TrimSpec | None) -> dict:
    """Evaluate both hardware band ends as built and, if needed, with a trim correction.

    The correction is the putty change that brings the lure back to its band-mid density,
    clamped to what the pocket can take (fill ≥ 0, ≤ capacity).
    """
    parts = section_moments(sections)
    mid = _band_moments(parts, sections, "mid")
    rho_mid = 1000.0 * sum(m.mass for m in mid.values()) / sum(m.vol for m in mid.values())
    ends: dict[str, dict] = {}
    for end in EDGE_BANDS:
        per = _band_moments(parts, sections, end)
        built = _hard_checks(per, end, t)
        entry: dict = {"as_built": built, "status": "pass" if built["ok"] else "fail"}
        if trim is not None:
            total = sum(per.values(), Moments())
            want = putty_to_density(total, rho_mid, trim.density)
            delta = float(np.clip(want, -trim.fill_g, trim.capacity_g - trim.fill_g))
            trimmed = dict(per)
            trimmed[trim.section] = (
                per[trim.section] + trim.column(trim.fill_g + delta) - trim.column(trim.fill_g)
            )
            with_trim = _hard_checks(trimmed, end, t)
            entry["trim"] = {
                "delta_g": round(delta, 3),
                "wanted_g": round(want, 3),
                "fill_g": round(trim.fill_g + delta, 3),
                "clamped": abs(delta - want) > 1e-6,
                "action": _putty_action(delta, trim.material),
                "checks": with_trim,
            }
            if not built["ok"] and with_trim["ok"]:
                entry["status"] = "pass_with_trim"
        ends[end] = entry
    ok = all(e["status"] != "fail" for e in ends.values())
    return {
        "status": "pass" if ok else "fail",
        "mid_density": round(rho_mid, 4),
        "trim": None
        if trim is None
        else {
            "material": trim.material,
            "nominal_fill_g": round(trim.fill_g, 3),
            "capacity_g": round(trim.capacity_g, 3),
            "x_mm": trim.x,
        },
        "ends": ends,
    }


def _band_moments(
    parts: dict[str, Moments], sections: list[SectionInput], band: Band
) -> dict[str, Moments]:
    hw = hardware_moments(sections, band)
    return {k: parts[k] + hw[k] for k in parts}


def _hard_checks(per: dict[str, Moments], band: Band, t: Targets) -> dict:
    props = lure_props_from_moments(per, band)
    res = [r for r in residuals(props, t) if r.hard]
    return {
        "ok": all(r.ok for r in res),
        "density": round(props.lure.density, 4),
        "cg_minus_cb_x_mm": round(props.lure.cg_minus_cb_x, 3),
        "mass_g": round(props.lure.mass_g, 3),
        "failed": [r.id for r in res if not r.ok],
    }


def _putty_action(delta: float, material: str) -> str:
    what = material.replace("_", " ")
    if abs(delta) < 0.005:
        return "no change"
    verb = "add" if delta > 0 else "remove"
    return f"{verb} {abs(delta):.2f} g of {what} {'to' if delta > 0 else 'from'} the trim pocket"


# --- feasibility --------------------------------------------------------------------------


def feasibility(props: LureProps, t: Targets) -> list[dict]:
    """Whether the targets can hold together for these section volumes (masses free).

    With V_h, V_t fixed by the envelope, section densities fix the section masses, and so
    the overall density and the head:tail ratio. Reports each conflict with numbers.
    """
    if not {"head", "tail"} <= props.sections.keys():
        return []
    vh = props.sections["head"].volume_mm3 / 1000.0
    vt = props.sections["tail"].volume_mm3 / 1000.0
    v = vh + vt
    out = []
    lo = (t.head_density[0] * vh + t.tail_density[0] * vt) / v
    hi = (t.head_density[1] * vh + t.tail_density[1] * vt) / v
    overlap = hi >= t.sink[0] and lo <= t.sink[1]
    out.append(
        {
            "id": "section_densities_vs_sink",
            "ok": overlap,
            "message": (
                f"head {t.head_density[0]}–{t.head_density[1]} and tail "
                f"{t.tail_density[0]}–{t.tail_density[1]} g/cm³ over V_head {vh:.2f} + "
                f"V_tail {vt:.2f} cm³ give an overall {lo:.3f}–{hi:.3f} g/cm³"
                + ("" if overlap else f", outside the {t.sink_mode} band {t.sink[0]}–{t.sink[1]}")
            ),
            "values": {"overall_range": [round(lo, 4), round(hi, 4)], "v_head_cm3": vh},
        }
    )
    m = t.sink_mid * v
    mh = m * t.ratio / (1 + t.ratio)
    mt = m - mh
    rh, rt = mh / vh, mt / vt
    ok_h = t.head_density[0] <= rh <= t.head_density[1]
    ok_t = t.tail_density[0] <= rt <= t.tail_density[1]
    out.append(
        {
            "id": "ratio_vs_sink",
            "ok": ok_h and ok_t,
            "message": (
                f"at {t.sink_mid:.3f} g/cm³ overall and head:tail {t.ratio}, the head is "
                f"{mh:.1f} g ({rh:.3f} g/cm³{'' if ok_h else ', outside its band'}) and the tail "
                f"{mt:.1f} g ({rt:.3f} g/cm³{'' if ok_t else ', outside its band'})"
            ),
            "values": {
                "head_g": round(mh, 2),
                "tail_g": round(mt, 2),
                "head_density": rh,
                "tail_density": rt,
            },
        }
    )
    return out
