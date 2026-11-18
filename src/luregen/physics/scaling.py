"""Weight → length: when `size.target_weight_oz` is set, the body length follows the mass.

    L = L₀ · (m / (ρ_t · V₀))^(1/3)

with L₀ the reference length, V₀ the envelope volume at L₀ and ρ_t the middle of the
target sink band. The body is rebuilt once at L, then the internals are solved.

Lengths that locate features on the body scale with it: hardware x/z, the joint's hinge eye
and line tie heights, the ballast position, the trim pocket position and the keel tops.
Material thicknesses (walls, gap, bores) and hardware sizes do not. In a weight-driven
variant, those positions are given for the preset's own length (or, in a resolved config,
for `size.solved_length_mm`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from luregen.units import oz_to_g

if TYPE_CHECKING:
    from luregen.config import LureConfig

L_MIN, L_MAX = 100.0, 250.0


class ScalingError(ValueError):
    pass


def length_for_weight(l0_mm: float, v0_mm3: float, mass_g: float, rho_target: float) -> float:
    """L = L₀·(m / (ρ_t·V₀))^(1/3); V₀ in mm³, ρ in g/cm³."""
    if l0_mm <= 0 or v0_mm3 <= 0 or mass_g <= 0 or rho_target <= 0:
        raise ScalingError("length_for_weight needs positive inputs")
    return l0_mm * (mass_g / (rho_target * v0_mm3 / 1000.0)) ** (1.0 / 3.0)


def target_mass_g(cfg: LureConfig) -> float:
    return oz_to_g(cfg.size.target_weight_oz)


def target_density(cfg: LureConfig) -> float:
    lo, hi = cfg.buoyancy.sink_bands[cfg.buoyancy.target_sink]
    return 0.5 * (lo + hi)


def scale_config(cfg: LureConfig, l_ref: float, l_new: float) -> LureConfig:
    """Copy of `cfg` at length `l_new` with body-relative positions scaled by l_new/l_ref."""
    if not L_MIN <= l_new <= L_MAX:
        raise ScalingError(
            f"target_weight_oz {cfg.size.target_weight_oz} needs a {l_new:.1f} mm body, "
            f"outside {L_MIN:.0f}–{L_MAX:.0f} mm"
        )
    s = l_new / l_ref
    out = cfg.model_copy(deep=True)
    out.size.solved_length_mm = round(l_new, 3)
    for h in out.hardware:
        h.x_mm = round(h.x_mm * s, 3)
        h.z_mm = round(h.z_mm * s, 3)
    if out.joint is not None:
        out.joint.hinge_eye_z = tuple(round(z * s, 3) for z in out.joint.hinge_eye_z)
        out.joint.line_tie_z = round(min(max(out.joint.line_tie_z * s, -6.0), 0.0), 3)
    out.ballast.x_from_joint_mm = round(out.ballast.x_from_joint_mm * s, 3)
    out.ballast.z_above_belly_mm = round(out.ballast.z_above_belly_mm * s, 3)
    out.internals.trim_x_from_tail_mm = round(out.internals.trim_x_from_tail_mm * s, 3)
    for sec in (out.internals.head, out.internals.tail):
        sec.keel_top_z_mm = round(sec.keel_top_z_mm * s, 3)
    return out


def scaling_report(l_ref: float, v0_mm3: float, mass_g: float, rho: float, l_new: float) -> dict:
    return {
        "reference_length_mm": round(l_ref, 3),
        "reference_volume_cm3": round(v0_mm3 / 1000.0, 4),
        "target_mass_g": round(mass_g, 3),
        "target_density": rho,
        "length_mm": round(l_new, 3),
        "scale": round(l_new / l_ref, 5),
    }
