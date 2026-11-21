import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from _profile_synth import Scene, lure_outline
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.commands.build import BuildError, station_table_for, trace_photo
from luregen.config import load_config
from luregen.profile.table import StationTable


def write_lure_facing_right(path: Path) -> Path:
    """Asymmetric body (deepest toward the nose), photographed nose-right."""
    outline = lure_outline(175, 50)
    outline[:, 0] = 197.5 - outline[:, 0]
    s = Scene(220, 80)
    s.polygon(outline + [0, 45])
    cv2.imwrite(str(path), s.render())
    return path


SAMPLE = Path(__file__).resolve().parents[2] / "spec/designs/glide/variants/striper_175_petg.toml"


def run_build(variant: Path, out: Path, *sets: str):
    args = ["build", str(variant), "--out-root", str(out)]
    for s in sets:
        args += ["--set", s]
    return CliRunner().invoke(make_app(), args)


@pytest.fixture(scope="module")
def builds(tmp_path_factory) -> dict[str, Path]:
    """Two full builds: the print sample (loft3d) and a CNC wood profile25d body. The
    balance solver is off here (F3 geometry checks); test_balance_build covers it."""
    out = {}
    cases = {
        "print": ["solver.enabled=false"],
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
    assert len(report["verdict"]["checks"]) == 5  # head density is info only (#96)
    assert report["verdict"]["info"][0]["id"] == "head_density"
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
    image.profile.source, image.profile.image = "image", tmp_path / "missing.png"
    with pytest.raises(BuildError, match="profile.image not found"):
        station_table_for(image)
    weight = cfg.model_copy(deep=True)
    weight.size.length_mm, weight.size.target_weight_oz = None, 2.0
    assert station_table_for(weight).length == pytest.approx(175.0)  # scaled in build_lure
    weight.size.solved_length_mm = 150.0
    assert station_table_for(weight).length == pytest.approx(150.0)
    missing = cfg.model_copy(deep=True)
    missing.profile.preset = tmp_path / "nope.csv"
    with pytest.raises(BuildError, match="not found"):
        station_table_for(missing)


def test_build_bad_photo_is_a_clean_error(sample_variant: Path, tmp_path: Path):
    blank = tmp_path / "blank.png"
    cv2.imwrite(str(blank), np.full((200, 300, 3), 235, np.uint8))
    for photo, msg in ((tmp_path / "side.png", "not found"), (blank, "profile photo")):
        res = run_build(sample_variant, tmp_path, "profile.source=image", f"profile.image={photo}")
        assert res.exit_code == 1
        assert msg in res.output
    assert not (tmp_path / "glide").exists()  # fails before creating a build folder


def test_station_table_traced_from_photo(sample_variant: Path, tmp_path: Path):
    """profile.source = "image" traces the photo in the build (#134), nose right here."""
    photo = write_lure_facing_right(tmp_path / "side.png")
    sets = ["profile.source=image", f"profile.image={photo}", "profile.nose=right"]
    cfg = load_config(sample_variant, [*sets, "size.length_mm=160"])
    traced = trace_photo(cfg)
    table = station_table_for(cfg, traced)
    assert traced.table.length == pytest.approx(160)  # length scale = size.length_mm
    assert table.length == pytest.approx(160)
    assert table.v_radius[2] > table.v_radius[-3]  # deep end (nose) at x = 0
    weight = cfg.model_copy(deep=True)
    weight.size.length_mm, weight.size.target_weight_oz = None, 2.0
    assert station_table_for(weight).length == pytest.approx(150)  # nominal trace length


@pytest.mark.slow
def test_build_from_photo_writes_profile(sample_variant: Path, tmp_path: Path):
    photo = write_lure_facing_right(tmp_path / "side.png")
    sets = ["profile.source=image", f"profile.image={photo}", "profile.nose=right"]
    res = run_build(sample_variant, tmp_path / "out", *sets, "solver.enabled=false")
    assert res.exit_code == 0, res.output
    build = next((tmp_path / "out").glob("*/*/latest")).resolve()
    assert StationTable.load_csv(build / "profile/stations.csv").length == pytest.approx(175)
    assert (build / "profile/profile_debug.png").is_file()
