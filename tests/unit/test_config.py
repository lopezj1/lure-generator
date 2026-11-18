import copy
from pathlib import Path

import pytest

from luregen.config import (
    ConfigError,
    apply_overrides,
    deep_merge,
    dump_resolved,
    load_config,
    load_layered,
    load_resolved,
)


def test_deep_merge_tables_merge_and_lists_replace():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    top = {"a": {"y": [9]}, "c": 3}
    assert deep_merge(base, top) == {"a": {"x": 1, "y": [9]}, "b": 1, "c": 3}


def test_layer_precedence_variant_over_family_over_common(mini_spec: Path):
    data = load_layered(mini_spec / "designs" / "fam" / "variants" / "v.toml")
    assert data["profile"]["n_stations"] == 12  # variant wins over family
    assert data["buoyancy"]["head_tail_mass_ratio"] == 2.0  # family
    assert data["buoyancy"]["water"] == "salt"  # common
    assert "extends" not in data
    assert Path(data["profile"]["preset"]).is_absolute()  # resolved relative to family.toml


def test_overrides_coerce_types_and_index_lists():
    data = {"a": {"n": 1}, "items": [{"m": 1}]}
    out = apply_overrides(
        data, ["a.n=2.5", "a.flag=true", "a.pair=[1, 2]", "a.word=suspend", "items.0.m=7"]
    )
    assert out["a"] == {"n": 2.5, "flag": True, "pair": [1, 2], "word": "suspend"}
    assert out["items"][0]["m"] == 7
    assert data["a"]["n"] == 1  # input untouched


def test_bad_override_rejected():
    with pytest.raises(ConfigError):
        apply_overrides({}, ["no_equals_sign"])
    with pytest.raises(ConfigError):
        apply_overrides({"items": []}, ["items.3.m=1"])


def test_unknown_key_rejected(mini_spec: Path):
    variant = mini_spec / "designs" / "fam" / "variants" / "v.toml"
    with pytest.raises(ConfigError, match="typo_key"):
        load_config(variant, ["process.typo_key=1"])


def test_exactly_one_of_length_and_weight(mini_spec: Path):
    variant = mini_spec / "designs" / "fam" / "variants" / "v.toml"
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(variant, ["size.target_weight_oz=2.0"])


def test_resolve_fills_densities_and_hardware(mini_spec: Path):
    cfg = load_config(mini_spec / "designs" / "fam" / "variants" / "v.toml")
    assert cfg.is_resolved
    assert cfg.buoyancy.water_density == 1.025
    assert cfg.process.material_density == 1.27
    assert cfg.ballast.density == 18.0
    hook = cfg.hardware[0]
    assert hook.mass_g == (1.0, 1.5) and hook.density == 7.85 and hook.measured


def test_material_density_override_wins(mini_spec: Path):
    variant = mini_spec / "designs" / "fam" / "variants" / "v.toml"
    cfg = load_config(variant, ["process.material=basswood", "process.material_density=0.45"])
    assert cfg.process.material_density == 0.45


def test_unknown_hardware_id_rejected(mini_spec: Path):
    variant = mini_spec / "designs" / "fam" / "variants" / "v.toml"
    with pytest.raises(ConfigError, match="nope"):
        load_config(variant, ["hardware.0.id=nope"])


def test_resolved_round_trip(mini_spec: Path, tmp_path: Path):
    cfg = load_config(mini_spec / "designs" / "fam" / "variants" / "v.toml")
    path = tmp_path / "resolved.toml"
    path.write_text(dump_resolved(cfg))
    assert load_resolved(path) == cfg


def test_sample_variant_validates(sample_variant: Path):
    cfg = load_config(sample_variant)
    assert cfg.family == "glide"
    assert cfg.joint is not None
    assert cfg.profile.preset is not None and cfg.profile.preset.is_file()
    assert len(cfg.hardware) >= 5


def test_path_helpers_round_trip(tmp_path: Path):
    from luregen.config import PATH_KEYS, absolutize_paths, relativize_paths

    assert ("profile", "image") in PATH_KEYS
    data = {"profile": {"image": "img/side.png", "preset": "../p.csv", "n": 3}, "name": "x"}
    absolute = absolutize_paths(copy.deepcopy(data), tmp_path / "spec")
    assert absolute["profile"]["image"] == str((tmp_path / "spec" / "img" / "side.png").resolve())
    assert absolute["profile"]["preset"] == str((tmp_path / "p.csv").resolve())
    assert relativize_paths(absolute, (tmp_path / "spec").resolve()) == data
