"""Filament slots, supports and slicer overrides for one print job (#51).

Sections print upright, so a fully rounded (loft3d) belly needs supports. With a
**dissimilar-material interface** (PLA↔PETG don't bond) the top Z gap can be 0 and the
underside still peels clean. That makes the job multi-filament: the body slot prints the
part and the support base, the interface slot prints the interface layers, and the prime
tower is switched on. Single-filament jobs keep the prime tower off.

Every slot's filament profile is loaded in slot order, so tool numbers in the G-code match
the machine (T0–T3), and `--load-filament-ids` puts the object on the body slot.

Override precedence (later wins): machine `[overrides]` ← `[support_overrides]` ←
`[interface_overrides]` ← computed filament/prime-tower keys ← the variant's
`print.overrides`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from luregen.config import LureConfig
from luregen.fab.printer.machine import FilamentSlot, PrinterMachine


class PlanError(ValueError):
    pass


@dataclass
class JobPlan:
    body: FilamentSlot
    interface: FilamentSlot | None  # set only for a multi-filament support interface
    supports: bool
    supports_reason: str
    overrides: dict[str, Any]
    filaments: list[Path]  # one profile per slot, in slot order
    filament_ids: str  # 1-based filament per object (one object per STL)
    warnings: list[str] = field(default_factory=list)

    @property
    def slots(self) -> list[int]:
        """Slots the job prints with (body first)."""
        return [self.body.slot] + ([self.interface.slot] if self.interface else [])

    @property
    def multi_filament(self) -> bool:
        return self.interface is not None and self.interface.slot != self.body.slot

    def as_dict(self) -> dict[str, Any]:
        return {
            "body": asdict(self.body) | {"tool": self.body.tool},
            "interface": (asdict(self.interface) | {"tool": self.interface.tool})
            if self.interface
            else None,
            "supports": self.supports,
            "supports_reason": self.supports_reason,
            "multi_filament": self.multi_filament,
            "prime_tower": bool(self.overrides.get("enable-prime-tower")),
            "overrides": self.overrides,
            "filament_profiles": [p.name for p in self.filaments],
            "filament_ids": self.filament_ids,
            "warnings": self.warnings,
        }


def supports_needed(cfg: LureConfig, roundover_mm: float | None) -> tuple[bool, str]:
    """`print.supports`: on/off, or auto (loft3d always; profile25d by belly roundover)."""
    mode = cfg.print.supports
    if mode != "auto":
        return mode == "on", f"print.supports = {mode}"
    if cfg.process.body_style == "loft3d":
        return True, "loft3d printed upright: the rounded belly overhangs"
    limit = cfg.print.support_min_roundover_mm
    r = roundover_mm or 0.0
    if r >= limit:
        return True, f"profile25d belly roundover {r:.1f} mm ≥ {limit} mm"
    return False, f"profile25d belly roundover {r:.1f} mm < {limit} mm: self-supporting"


def _default_slot(machine: PrinterMachine, material: str) -> FilamentSlot | None:
    return next(
        (s for s in sorted(machine.filaments, key=lambda s: s.slot) if s.material == material), None
    )


def plan_job(
    cfg: LureConfig,
    machine: PrinterMachine,
    roundover_mm: float | None = None,
    app_key: str | None = None,
) -> JobPlan:
    """Choose slots, supports and the slicer overrides for a print build."""
    if not machine.filaments:
        raise PlanError(f"{machine.name}: no [[filaments]] slots in machine.toml")
    slots = sorted(machine.filaments, key=lambda s: s.slot)
    if [s.slot for s in slots] != list(range(len(slots))):
        raise PlanError(f"{machine.name}: filament slots must be T0..T{len(slots) - 1}")
    warnings: list[str] = []
    material = cfg.process.material

    if cfg.print.body_slot is not None:
        body = machine.slot(cfg.print.body_slot)
        if body.material != material:
            warnings.append(
                f"body slot {body.tool} holds {body.material}, but process.material is "
                f"{material}: the mass model assumes {material}"
            )
    else:
        body = _default_slot(machine, material)
        if body is None:
            raise PlanError(
                f"no filament slot holds {material} (process.material); load it in "
                f"machine.toml [[filaments]] or set print.body_slot"
            )

    supports, reason = supports_needed(cfg, roundover_mm)
    interface: FilamentSlot | None = None
    if supports:
        if cfg.print.interface_slot is not None:
            interface = machine.slot(cfg.print.interface_slot)
        elif pair := machine.interface_pairs.get(body.material):
            interface = _default_slot(machine, pair)
        if interface is None:
            warnings.append(
                f"supports without a dissimilar interface filament (none loaded for "
                f"{body.material}): the belly will show support marks"
            )
        elif interface.slot == body.slot:
            interface = None
        elif interface.material == body.material:
            warnings.append(
                f"interface slot {interface.tool} is {interface.material}, the same material "
                f"as the body: it will bond; load a dissimilar one (e.g. "
                f"{machine.interface_pairs.get(body.material, 'PLA/PETG')})"
            )

    overrides: dict[str, Any] = dict(machine.overrides)
    body_id = body.slot + 1
    if supports:
        overrides.update(machine.support_overrides)
        overrides["support-filament"] = body_id  # the base prints with the body
        overrides["support-interface-filament"] = body_id
        if interface is not None:
            overrides.update(machine.interface_overrides)
            overrides["support-interface-filament"] = interface.slot + 1
    else:
        overrides["enable-support"] = False
    overrides["enable-prime-tower"] = bool(interface is not None)
    overrides.update(cfg.print.overrides)

    filaments = [machine.filament_profile(s.material, app_key) for s in slots]
    return JobPlan(
        body=body,
        interface=interface,
        supports=supports,
        supports_reason=reason,
        overrides=overrides,
        filaments=filaments,
        filament_ids=str(body_id),
        warnings=warnings,
    )
