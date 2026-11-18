import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.commands.build import BuildError, station_table_for
from luregen.config import load_config

SAMPLE = Path(__file__).resolve().parents[2] / "spec/designs/glide/variants/striper_175_petg.toml"


def run_build(variant: Path, out: Path, *sets: str):
    args = ["build", str(variant), "--out-root", str(out)]
    for s in sets:
        args += ["--set", s]
    return CliRunner().invoke(make_app(), args)


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> dict[str, Path]:
    """Two full builds: the print sample (loft3d) and a CNC wood profile25d body."""
    out = {}
    cases = {
        "print": [],
        "cnc": ["process.method=cnc", "process.material=basswood", "process.body_style=profile25d"],
    }
    for name, sets in cases.items():
        root = tmp_path_factory.mktemp(name)
        res = run_build(SAMPLE, root, *sets)
        assert res.exit_code == 0, res.output
        out[name] = (root / "glide" / "striper_175_petg" / "latest").resolve()
    return out


@pytest.mark.slow
@pytest.mark.parametrize("case", ["print", "cnc"])
def test_build_writes_sections_and_report(builds, case):
    build = builds[case]
    model = build / "model"
    assert sorted(p.stem for p in model.glob("*.step")) == ["body", "head", "tail"]
    for stem in ("head", "tail", "body"):
        for ext in ("step", "stl", "3mf"):
            assert (model / f"{stem}.{ext}").stat().st_size > 0
    report = json.loads((build / "report.json").read_text())
    assert report["status"] == "built"
    env = report["envelope"]
    assert env["style"] == ("loft3d" if case == "print" else "profile25d")
    assert env["bbox_mm"]["size"][0] == pytest.approx(175.0, abs=0.01)
    assert ("roundover" in env) == (case == "cnc")
    for name in ("head", "tail"):
        sec = report["sections"][name]
        assert sec["mass_g"] > 0 and sec["density_g_cm3"] == sec["density"]
        assert sec["files"]["stl"]["ok"] and sec["files"]["3mf"]["ok"]
        assert set(sec["band"]) == {"min", "mid", "max"}
    lure = report["lure"]
    h, t = report["sections"]["head"], report["sections"]["tail"]
    assert lure["mass_g"] == pytest.approx(h["mass_g"] + t["mass_g"], abs=2e-3)
    assert lure["band"]["min"]["mass_g"] < lure["band"]["max"]["mass_g"]
    assert lure["head_tail_mass_ratio"] == pytest.approx(h["mass_g"] / t["mass_g"], rel=1e-3)
    assert report["verdict"]["status"] in {"pass", "fail"}
    assert len(report["verdict"]["checks"]) == 6
    assert report["summary"]["verdict"]["status"] == report["verdict"]["status"]
    assert report["summary"]["cg_mm"] == lure["cg_mm"]
    assert (build / report["diagram"]).stat().st_size > 0
    md = (build / "report.md").read_text()
    assert "## Envelope" in md and "## Verdict" in md and "cg_cb.png" in md


@pytest.mark.slow
def test_print_build_has_sealed_cavities(builds):
    report = json.loads((builds["print"] / "report.json").read_text())
    geo = report["geometry"]
    for name in ("head", "tail"):
        cav = geo["internals"][name]
        assert cav["cavity_volume_cm3"] > 5
        assert cav["min_skin_mm"] >= 1.2
    assert geo["ballast"]["mass_g"] == pytest.approx(8.0, rel=0.01)
    assert geo["trim_pocket"]["capacity_g"] > geo["hardware_band_g"]
    # head displacement = body + cavity + ballast + hardware volumes
    assert report["sections"]["head"]["volume_cm3"] > geo["internals"]["head"]["cavity_volume_cm3"]


@pytest.mark.slow
def test_cnc_build_has_no_cavity(builds):
    report = json.loads((builds["cnc"] / "report.json").read_text())
    assert report["geometry"]["internals"] is None
    assert report["sections"]["head"]["density"] < 1.0  # basswood + ballast


def test_station_table_scaled_to_length(sample_variant: Path):
    cfg = load_config(sample_variant, ["size.length_mm=140"])
    table = station_table_for(cfg)
    assert table.length == pytest.approx(140.0)
    assert table.meta["scaled_by"] == "0.8"


def test_station_table_errors(sample_variant: Path, tmp_path: Path):
    cfg = load_config(sample_variant)
    image = cfg.model_copy(deep=True)
    image.profile.source = "image"
    with pytest.raises(BuildError, match="lure profile"):
        station_table_for(image)
    weight = cfg.model_copy(deep=True)
    weight.size.length_mm, weight.size.target_weight_oz = None, 2.0
    with pytest.raises(BuildError, match="F4"):
        station_table_for(weight)
    missing = cfg.model_copy(deep=True)
    missing.profile.preset = tmp_path / "nope.csv"
    with pytest.raises(BuildError, match="not found"):
        station_table_for(missing)


def test_build_image_source_is_a_clean_error(sample_variant: Path, tmp_path: Path):
    res = run_build(sample_variant, tmp_path, "profile.source=image", "profile.image=side.png")
    assert res.exit_code == 1
    assert "lure profile" in res.output
    assert not (tmp_path / "glide").exists()  # fails before creating a build folder
