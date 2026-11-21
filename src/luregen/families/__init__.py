"""Bait families: one module per family behind the `LureFamily` protocol.

A family turns the outer envelope into its fabricated sections (joints, internals,
pockets) and describes each section's parts for the mass properties. `lure build` drives
the shared stages (config, stations, envelope, mass properties, export, report) and calls
the family for everything in between.

Import heavy modules (build123d) inside the family functions; `get_family` imports the
family module lazily.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.geometry.envelope import Envelope
    from luregen.physics.massprops import SectionInput
    from luregen.profile.table import StationTable

# stage name, fraction within the family's share of the build (0–1), message
Emit = Callable[[str, float, str], None]

FAMILIES = {"glide": "luregen.families.glide"}


class FamilyError(RuntimeError):
    pass


@dataclass
class FamilyBuild:
    """What a family hands back to `lure build`."""

    sections: dict[str, Any]  # name → build123d Solid, in export order
    mass_inputs: list[SectionInput]
    report: dict[str, Any]  # family geometry block (joint, internals, pockets, warnings)
    diagram: dict[str, Any] = field(default_factory=dict)  # side-view data for the report
    warnings: list[str] = field(default_factory=list)
    # Separately made parts that are not balance sections (e.g. the TPU tail fin, #123):
    # name → build123d Solid, exported as model/<name>.*; their mass is in a section's parts.
    parts: dict[str, Any] = field(default_factory=dict)


class LureFamily(Protocol):
    name: str

    def build(
        self, cfg: LureConfig, table: StationTable, envelope: Envelope, emit: Emit
    ) -> FamilyBuild: ...


def get_family(name: str) -> LureFamily:
    if name not in FAMILIES:
        raise FamilyError(f"unknown lure family {name!r} (known: {', '.join(FAMILIES)})")
    module = importlib.import_module(FAMILIES[name])
    return module.FAMILY
