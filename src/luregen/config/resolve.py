"""Fill catalog-derived fields (densities, hardware masses) into a LureConfig."""

import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from luregen.config.loader import ConfigError, apply_overrides, load_layered
from luregen.config.models import LureConfig


def find_spec_root(start: Path) -> Path:
    """Nearest ancestor directory named `spec` that holds materials.toml."""
    for parent in [start.resolve(), *start.resolve().parents]:
        if parent.name == "spec" and (parent / "materials.toml").is_file():
            return parent
    raise ConfigError(f"no spec/ root (with materials.toml) above {start}")


def load_materials(spec_root: Path) -> dict[str, dict[str, Any]]:
    data = tomllib.loads((spec_root / "materials.toml").read_text())
    return data["materials"]


def load_hardware_catalog(spec_root: Path) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for file in sorted((spec_root / "hardware").glob("*.toml")):
        for item_id, item in tomllib.loads(file.read_text()).items():
            if item_id in catalog:
                raise ConfigError(f"duplicate hardware id {item_id!r} in {file.name}")
            catalog[item_id] = item
    return catalog


def _density(materials: dict[str, dict[str, Any]], name: str, what: str) -> float:
    if name not in materials:
        raise ConfigError(f"unknown {what} material {name!r} (not in spec/materials.toml)")
    return float(materials[name]["density"])


def resolve(cfg: LureConfig, spec_root: Path) -> LureConfig:
    """Return a copy with every catalog-derived field filled; already-set fields win."""
    materials = load_materials(spec_root)
    catalog = load_hardware_catalog(spec_root)
    cfg = cfg.model_copy(deep=True)

    if cfg.buoyancy.water_density is None:
        cfg.buoyancy.water_density = _density(materials, f"water_{cfg.buoyancy.water}", "water")
    if cfg.ballast.density is None:
        cfg.ballast.density = _density(materials, cfg.ballast.material, "ballast")
    if cfg.internals.trim_density is None and cfg.internals.trim_material in materials:
        # optional: a catalog without the trim material still resolves (trim falls back)
        cfg.internals.trim_density = _density(materials, cfg.internals.trim_material, "trim")
    if cfg.process.material_density is None:
        cfg.process.material_density = _density(materials, cfg.process.material, "body")

    if cfg.fin is not None:
        if cfg.fin.density is None:
            cfg.fin.density = _density(materials, cfg.fin.material, "fin")
        _fill_hardware(cfg.fin.screw, catalog, materials)

    for item in cfg.hardware:
        _fill_hardware(item, catalog, materials)
    return cfg


def _fill_hardware(item, catalog: dict, materials: dict) -> None:
    """Fill a hardware item's unset name, mass band, material and density from the catalog."""
    entry = catalog.get(item.id)
    if entry is None and (item.mass_g is None or item.material is None):
        raise ConfigError(f"hardware id {item.id!r} not in spec/hardware and not fully given")
    entry = entry or {}
    item.name = item.name or entry.get("name", item.id)
    item.mass_g = item.mass_g or tuple(entry["mass_g"])
    item.material = item.material or entry["material"]
    item.density = item.density or _density(materials, item.material, "hardware")
    if item.measured is None:
        item.measured = bool(entry.get("measured", False))


def load_config(path: Path, overrides: list[str] | None = None) -> LureConfig:
    """Load a variant (following `extends`), apply overrides, validate and resolve."""
    data = apply_overrides(load_layered(path), overrides or [])
    try:
        cfg = LureConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        spec_root = find_spec_root(path.parent)
    except ConfigError:
        if cfg.is_resolved:  # a build's resolved.toml: self-contained, no catalogs needed
            return cfg
        raise
    return resolve(cfg, spec_root)


def dump_resolved(cfg: LureConfig) -> str:
    return tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True))


def load_resolved(path: Path) -> LureConfig:
    cfg = LureConfig.model_validate(tomllib.loads(path.read_text()))
    if not cfg.is_resolved:
        raise ConfigError(f"{path} is not a fully resolved config")
    return cfg
