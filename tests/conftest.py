from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "spec"
SAMPLE_VARIANT = SPEC / "designs" / "glide" / "variants" / "striper_175_petg.toml"


def pytest_configure(config):
    # Full `lure build` runs (10–40 s each); still run by default, skip with -m "not slow".
    config.addinivalue_line("markers", "slow: runs a full lure build (deselect: -m 'not slow')")


@pytest.fixture
def sample_variant() -> Path:
    return SAMPLE_VARIANT


@pytest.fixture
def mini_spec(tmp_path: Path) -> Path:
    """A minimal spec/ tree: materials, one hardware item, _common + family + variant."""
    spec = tmp_path / "spec"
    (spec / "hardware").mkdir(parents=True)
    (spec / "designs" / "fam" / "variants").mkdir(parents=True)
    (spec / "materials.toml").write_text(
        "[materials.water_salt]\ndensity = 1.025\n"
        "[materials.water_fresh]\ndensity = 1.0\n"
        "[materials.PETG]\ndensity = 1.27\n"
        "[materials.basswood]\ndensity = 0.42\n"
        "[materials.tungsten]\ndensity = 18.0\n"
        "[materials.steel]\ndensity = 7.85\n"
    )
    (spec / "hardware" / "hooks.toml").write_text(
        '[hook]\nname = "Hook"\nmass_g = [1.0, 1.5]\nmaterial = "steel"\nmeasured = true\n'
    )
    (spec / "designs" / "_common.toml").write_text(
        '[buoyancy]\nwater = "salt"\ntarget_sink = "slow_sink"\n'
        "sink_bands = { float = [0.9, 0.98], suspend = [1.02, 1.03], slow_sink = [1.03, 1.06] }\n"
        '[dfm.cnc]\ntools = ["a", "b"]\n'
    )
    (spec / "designs" / "fam" / "family.toml").write_text(
        'extends = "../_common.toml"\nfamily = "fam"\n'
        '[profile]\nsource = "preset"\npreset = "p.csv"\nn_stations = 20\n'
        "[buoyancy]\nhead_tail_mass_ratio = 2.0\n"
    )
    (spec / "designs" / "fam" / "variants" / "v.toml").write_text(
        'extends = "../family.toml"\nname = "v"\n'
        "[size]\nlength_mm = 150\n"
        '[process]\nmethod = "print"\nmaterial = "PETG"\n'
        "[profile]\nn_stations = 12\n"
        '[[hardware]]\nid = "hook"\nsection = "head"\nx_mm = 10\nz_mm = -5\n'
    )
    return spec
