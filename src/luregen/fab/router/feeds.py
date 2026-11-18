"""Feeds, speeds, depth of cut and stepover from `spec/routers/<machine>/feeds.toml` (#46).

A port of the owner's cnc-feeds-speeds-calculator (Shapeoko A to Z data): the tables stay in
inches in `feeds.toml`, this module applies the calculator's formulas and converts to mm and
mm/min:

    feed = chipload × flutes × rpm      doc = doc_fraction × D
    stepover = stepover_fraction × D    plunge = plunge_fraction × feed

`conservative` picks the low end of each chipload range, `aggressive` the high end. Named
presets (owner-proven settings) override individual fields; the rest comes from the
calculator in the preset's `base_mode`. `chipload_status` reports below / within / above the
material's range, as the calculator's banner does.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

IN = 25.4  # mm per inch
Mode = Literal["conservative", "aggressive"]
Category = Literal["soft-plastic", "soft-wood", "hard-wood"]
_IDX = {"conservative": 0, "aggressive": 1}
_DIA_TOL_IN = 0.002  # a tool matches a calculator row within this (3.175 mm ↔ 0.125 in)


class FeedsError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RpmCfg(_Strict):
    default: float = Field(18000, gt=0)
    range: tuple[float, float] = (10000, 30000)


class VbitRow(_Strict):
    feedrate: tuple[float, float]
    doc: tuple[float, float]
    plunge: tuple[float, float]


class SurfacingRow(_Strict):
    feedrate: tuple[float, float]
    doc: tuple[float, float]


class Preset(_Strict):
    description: str = ""
    tool_diameter_in: float = Field(gt=0)
    shape: Literal["flat", "ball"] = "flat"
    base_mode: Mode = "conservative"
    feed_mm_min: float | None = Field(None, gt=0)
    plunge_mm_min: float | None = Field(None, gt=0)
    doc_mm: float | None = Field(None, gt=0)
    stepover_mm: float | None = Field(None, gt=0)
    source: str = ""


class FeedsTable(_Strict):
    source: str
    units: Literal["inch"] = "inch"
    rpm: RpmCfg = RpmCfg()
    chipload: dict[str, dict[Category, tuple[float, float]]]
    doc_fraction: dict[str, dict[Mode, float]]
    stepover_fraction: dict[Mode, float]
    plunge_fraction: dict[Category, float]
    vbit: dict[Category, VbitRow] = {}
    surfacing: SurfacingRow | None = None
    materials: dict[str, Category] = {}
    presets: dict[str, Preset] = {}

    def category(self, material: str) -> Category:
        """Calculator category of a body material (or a category name itself)."""
        if material in self.plunge_fraction:
            return material  # type: ignore[return-value]
        if material not in self.materials:
            known = ", ".join(sorted(self.materials))
            raise FeedsError(f"no feeds material mapping for {material!r} (known: {known})")
        return self.materials[material]

    def chipload_row(self, diameter_mm: float) -> tuple[str, dict[Category, tuple[float, float]]]:
        d_in = diameter_mm / IN
        for key, row in self.chipload.items():
            if abs(float(key) - d_in) <= _DIA_TOL_IN:
                return key, row
        sizes = ", ".join(f'{float(k):g}"' for k in self.chipload)
        raise FeedsError(f"no calculator row for a {diameter_mm:g} mm endmill (rows: {sizes})")


@dataclass(frozen=True)
class Feeds:
    """Cutting parameters for one tool, material and mode; lengths in mm, rates in mm/min."""

    tool_diameter: float
    flutes: int
    rpm: float
    mode: Mode
    material: str
    feed: float
    plunge: float
    doc: float
    stepover: float
    chipload_in: float | None  # actual chipload at `feed` and `rpm`
    chipload_range_in: tuple[float, float] | None
    preset: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status(self) -> Literal["below", "within", "above", "n/a"]:
        return chipload_status(self.chipload_in, self.chipload_range_in)

    def as_dict(self) -> dict:
        return {
            "tool_diameter_mm": self.tool_diameter,
            "flutes": self.flutes,
            "rpm": self.rpm,
            "mode": self.mode,
            "material": self.material,
            "preset": self.preset,
            "feed_mm_min": round(self.feed, 1),
            "plunge_mm_min": round(self.plunge, 1),
            "doc_mm": round(self.doc, 4),
            "stepover_mm": round(self.stepover, 4),
            "feed_ipm": round(self.feed / IN, 2),
            "plunge_ipm": round(self.plunge / IN, 2),
            "chipload_in": None if self.chipload_in is None else round(self.chipload_in, 5),
            "chipload_range_in": self.chipload_range_in,
            "chipload_status": self.status,
            "notes": list(self.notes),
        }


def chipload_status(
    chipload: float | None, rng: tuple[float, float] | None
) -> Literal["below", "within", "above", "n/a"]:
    if chipload is None or rng is None:
        return "n/a"
    eps = 1e-9
    if chipload < rng[0] - eps:
        return "below"
    if chipload > rng[1] + eps:
        return "above"
    return "within"


def load_feeds(path: Path) -> FeedsTable:
    return FeedsTable.model_validate(tomllib.loads(path.read_text()))


def calculate(
    table: FeedsTable,
    diameter_mm: float,
    material: str,
    mode: Mode = "conservative",
    flutes: int = 2,
    rpm: float | None = None,
    shape: Literal["flat", "ball"] = "flat",
) -> Feeds:
    """The calculator's endmill result for a tool of `diameter_mm` (mm, mm/min)."""
    cat = table.category(material)
    rpm = float(rpm or table.rpm.default)
    _, row = table.chipload_row(diameter_mm)
    rng = row[cat]
    chipload = rng[_IDX[mode]]
    feed_ipm = chipload * flutes * rpm
    d_in = diameter_mm / IN
    doc_row = table.doc_fraction.get(cat, table.doc_fraction["default"])
    notes: list[str] = []
    lo, hi = table.rpm.range
    if not lo <= rpm <= hi:
        notes.append(f"rpm {rpm:g} outside the calculator range {lo:g}-{hi:g}")
    if shape == "ball":
        notes.append("ball nose: flat-endmill chipload for the same diameter (assumption)")
    return Feeds(
        tool_diameter=diameter_mm,
        flutes=flutes,
        rpm=rpm,
        mode=mode,
        material=cat,
        feed=feed_ipm * IN,
        plunge=table.plunge_fraction[cat] * feed_ipm * IN,
        doc=doc_row[mode] * d_in * IN,
        stepover=table.stepover_fraction[mode] * d_in * IN,
        chipload_in=chipload,
        chipload_range_in=rng,
        notes=tuple(notes),
    )


def preset(
    table: FeedsTable,
    name: str,
    material: str,
    flutes: int = 2,
    rpm: float | None = None,
) -> Feeds:
    """A named preset; unset fields come from the calculator in its `base_mode`."""
    if name not in table.presets:
        raise FeedsError(f"unknown feeds preset {name!r} (known: {', '.join(table.presets)})")
    p = table.presets[name]
    cat = table.category(material)
    rpm = float(rpm or table.rpm.default)
    d_mm = p.tool_diameter_in * IN
    try:
        base: Feeds | None = calculate(table, d_mm, cat, p.base_mode, flutes, rpm, p.shape)
    except FeedsError:
        base = None
    values = {
        "feed": p.feed_mm_min,
        "plunge": p.plunge_mm_min,
        "doc": p.doc_mm,
        "stepover": p.stepover_mm,
    }
    missing = [k for k, v in values.items() if v is None]
    if missing and base is None:
        raise FeedsError(f"preset {name!r} needs {', '.join(missing)}: no calculator row")
    if base is not None:
        values = {k: (v if v is not None else getattr(base, k)) for k, v in values.items()}
    feed = float(values["feed"])  # type: ignore[arg-type]
    chipload = feed / IN / (flutes * rpm)
    notes = [f"preset {name}: {p.source}"] if p.source else []
    if base is None:
        notes.append("no calculator row for this diameter: chipload status n/a")
    return Feeds(
        tool_diameter=d_mm,
        flutes=flutes,
        rpm=rpm,
        mode=p.base_mode,
        material=cat,
        feed=feed,
        plunge=float(values["plunge"]),  # type: ignore[arg-type]
        doc=float(values["doc"]),  # type: ignore[arg-type]
        stepover=float(values["stepover"]),  # type: ignore[arg-type]
        chipload_in=chipload if base is not None else None,
        chipload_range_in=base.chipload_range_in if base is not None else None,
        preset=name,
        notes=tuple(notes),
    )


def special(
    table: FeedsTable, kind: Literal["vbit", "surfacing"], material: str, mode: Mode
) -> dict[str, float | None]:
    """V-bit or surfacing recommendation in mm and mm/min (no chipload)."""
    i = _IDX[mode]
    if kind == "vbit":
        row = table.vbit[table.category(material)]
        return {"feed": row.feedrate[i] * IN, "doc": row.doc[i] * IN, "plunge": row.plunge[i] * IN}
    if table.surfacing is None:
        raise FeedsError("feeds.toml has no surfacing row")
    s = table.surfacing
    return {"feed": s.feedrate[i] * IN, "doc": s.doc[i] * IN, "plunge": None}


def compare_presets(
    table: FeedsTable, material: str, flutes: int = 2, rpm: float | None = None
) -> list[dict]:
    """Each preset with the calculator's conservative/aggressive values beside it."""
    rows = []
    for name, p in table.presets.items():
        f = preset(table, name, material, flutes, rpm)
        row = {"preset": name, **f.as_dict()}
        for mode in ("conservative", "aggressive"):
            try:
                c = calculate(table, f.tool_diameter, material, mode, flutes, rpm, p.shape)
                row[f"calc_{mode}_feed_mm_min"] = round(c.feed, 1)
            except FeedsError:
                row[f"calc_{mode}_feed_mm_min"] = None
        rows.append(row)
    return rows
