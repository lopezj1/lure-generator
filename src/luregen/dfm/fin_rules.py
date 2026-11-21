"""Tail fin rules (#123): the slot that takes the TPU fin's tab and the lateral screw.

They read the `geometry.fin` report block (slot walls from the station outline, the screw
position and the body width along it), so they apply to print and CNC builds alike and
return nothing for a lure without a fin.
"""

from __future__ import annotations

import math

from luregen.config import LureConfig
from luregen.dfm.model import DfmModel
from luregen.dfm.registry import Violation, rule

SCREW_MIN_CLEAR_MM = 0.8  # solid between the screw pilot and another feature
SCREW_TIP_TOL_MM = 0.3  # the screw may stand this far past the far side


def _fin(model: DfmModel) -> dict | None:
    return (model.geometry or {}).get("fin")


@rule(
    "fin.slot_walls",
    severity="error",
    summary="Wall beside and above/below the fin slot ≥ the process minimum",
)
def slot_walls(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    fin = _fin(model)
    if not fin:
        return []
    walls = fin["slot"]["walls_mm"]
    out = []
    if cfg.process.method == "print":
        limit, severity = cfg.dfm.print.min_wall_mm, "error"
        what = "dfm.print.min_wall_mm"
    else:  # wood: the end grain beside a thin slot is fragile, but it's glued and screwed
        limit, severity = cfg.dfm.cnc.min_wood_wall_mm, "warn"
        what = "dfm.cnc.min_wood_wall_mm"
    for key, label in (("side_mm", "beside"), ("vertical_mm", "above/below")):
        if walls[key] < limit:
            out.append(
                Violation(
                    f"tail: {walls[key]:.2f} mm {label} the fin slot < {limit} mm ({what}); "
                    "thin the fin or move the cut forward (fin.cut_from_tail_mm)",
                    "tail",
                    walls[key],
                    limit,
                    "mm",
                    severity=severity,
                )
            )
    return out


@rule(
    "fin.screw_clearance",
    severity="error",
    summary="Fin screw clear of the through-wire, ballast and trim pockets and screw eyes",
)
def screw_clearance(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    fin = _fin(model)
    if not fin:
        return []
    geo = model.geometry
    sc = fin["screw"]
    xs, zs, r = sc["x_mm"], sc["z_mm"], 0.5 * sc["pilot_d_mm"]
    out: list[Violation] = []

    def clear(gap: float, what: str) -> None:
        if gap < SCREW_MIN_CLEAR_MM:
            out.append(
                Violation(
                    f"tail: fin screw at x {xs:.1f}, z {zs:.1f} mm is {gap:.2f} mm from {what} "
                    f"(< {SCREW_MIN_CLEAR_MM} mm)",
                    "tail",
                    gap,
                    SCREW_MIN_CLEAR_MM,
                    "mm",
                )
            )

    if cfg.process.rigging == "through_wire":
        w = cfg.cnc.wire
        zw = w.z_mm if w.z_mm is not None else (cfg.joint.line_tie_z if cfg.joint else 0.0)
        clear(abs(zs - zw) - r - 0.5 * w.diameter_mm - w.clearance_mm, "the through-wire")
        z_lo, z_hi = fin["slot"]["z_mm"]
        if z_lo - w.diameter_mm < zw < z_hi + w.diameter_mm:
            out.append(
                Violation(
                    f"tail: the through-wire (z {zw:g} mm) runs through the fin slot "
                    f"(z {z_lo:g}–{z_hi:g} mm); end the wire at the tail hook ahead of "
                    f"x {fin['slot']['x_mm'][0]:g} mm or move the slot (fin.screw / slot height)",
                    "tail",
                )
            )

    def to_region(x0: float, x1: float, z_top: float) -> float:
        """Side-view gap from the pilot hole to the region x0..x1 below z_top."""
        dx = max(x0 - xs, 0.0, xs - x1)
        dz = max(zs - z_top, 0.0)
        return math.hypot(dx, dz) - r

    tp = geo.get("trim_pocket")
    if tp:  # vertical cylinder up from the belly to z_top
        clear(
            to_region(tp["x_mm"] - tp["r_mm"], tp["x_mm"] + tp["r_mm"], tp["z_top_mm"]),
            "the trim pocket",
        )
    b = geo.get("ballast") or {}
    if b.get("x_mm") is not None:  # the pocket runs from the belly up to its axis + r
        half = 0.5 * b.get("length_mm", 0.0) + b.get("r_mm", 0.0)
        top = b.get("z_axis_mm", 0.0) + b.get("r_mm", 0.0)
        clear(to_region(b["x_mm"] - half, b["x_mm"] + half, top), "the ballast pocket")
    boss = cfg.internals.boss_r_mm if cfg.process.method == "print" else 0.0
    for h in cfg.hardware:
        if h.section == "tail" and h.id.startswith("screw_eye"):
            d = math.hypot(xs - h.x_mm, zs - h.z_mm) - r - boss
            clear(d, f"the {h.name or h.id} (boss r {boss:g} mm)")
    return out


@rule(
    "fin.screw_length",
    severity="warn",
    summary="Fin screw length vs the tail width along it",
)
def screw_length(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    fin = _fin(model)
    if not fin:
        return []
    sc = fin["screw"]
    width, length = sc["body_width_mm"], sc["length_mm"]
    if length > width + SCREW_TIP_TOL_MM:
        return [
            Violation(
                f"tail: the {length:g} mm fin screw stands {length - width:.1f} mm past the far "
                f"side (tail {width:.1f} mm wide there); use a shorter screw",
                "tail",
                length,
                width,
                "mm",
            )
        ]
    if length < 0.5 * width + 0.5 * fin["slot"]["width_mm"]:
        return [
            Violation(
                f"tail: the {length:g} mm fin screw does not cross the slot (tail {width:.1f} mm "
                "wide there)",
                "tail",
                length,
                width,
                "mm",
                severity="error",
            )
        ]
    return []
