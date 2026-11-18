import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.commands.build import BuildError, station_table_for
from luregen.config import load_config


def run_build(variant: Path, out: Path, *sets: str):
    args = ["build", str(variant), "--out-root", str(out)]
    for s in sets:
        args += ["--set", s]
    return CliRunner().invoke(make_app(), args)


@pytest.mark.parametrize("style", ["loft3d", "profile25d"])
def test_build_writes_envelope(sample_variant: Path, tmp_path: Path, style: str):
    res = run_build(sample_variant, tmp_path, f"process.body_style={style}")
    assert res.exit_code == 0, res.output
    build = (tmp_path / "glide" / "striper_175_petg" / "latest").resolve()
    for ext in ("step", "stl", "3mf"):
        assert (build / "model" / f"body.{ext}").stat().st_size > 0
    report = json.loads((build / "report.json").read_text())
    assert report["status"] == "envelope"
    env = report["envelope"]
    assert env["style"] == style
    assert env["bbox_mm"]["size"][0] == pytest.approx(175.0, abs=0.01)
    assert env["files"]["stl"]["ok"] and env["files"]["3mf"]["ok"]
    assert env["solid_mass_g"] == pytest.approx(env["volume_cm3"] * 1.27, rel=1e-3)
    assert ("roundover" in env) == (style == "profile25d")
    assert "## Envelope" in (build / "report.md").read_text()


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
