"""`lure fab --format cnc` end to end on a CNC build of the sample variant."""

import json
import re
from pathlib import Path

import pytest
from router_jobs import shapeoko
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.config import load_config
from luregen.fab.router.verify import verify
from luregen.report.build_dir import create_build_dir

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "spec/designs/glide/variants/striper_175_petg.toml"
CNC = ["process.method=cnc", "process.material=basswood", "process.rigging=through_wire"]


def invoke(*args: str, cwd: Path = REPO):
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(make_app(), list(args))
    finally:
        os.chdir(old)


@pytest.fixture(scope="module")
def cnc_build(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cncbuild")
    sets = [a for s in CNC for a in ("--set", s)]
    res = invoke("build", str(SAMPLE), "--out-root", str(root), *sets)
    assert res.exit_code == 0, res.output
    return (root / "glide" / "striper_175_petg" / "latest").resolve()


@pytest.mark.slow
def test_cnc_build_has_glue_face_ballast_pocket(cnc_build):
    report = json.loads((cnc_build / "report.json").read_text())
    ballast = report["geometry"]["ballast"]
    pocket = ballast["glue_face_pocket"]
    assert pocket["volume_cm3"] == pytest.approx(ballast["volume_cm3"], rel=2e-3)
    assert pocket["corner_r"] >= 3.175 / 2
    assert ballast["mass_g"] == pytest.approx(8.0, rel=0.01)


@pytest.mark.slow
def test_fab_cnc_writes_verified_files_per_tool(cnc_build):
    res = invoke("fab", str(cnc_build), "--format", "cnc")
    assert res.exit_code == 0, res.output
    out = cnc_build / "cnc"
    names = sorted(p.name for p in out.glob("*.nc"))
    assert names == [
        "setup1_01_flat_3.175.nc",
        "setup1_02_ball_3.175.nc",
        "setup2_01_flat_3.175.nc",
    ]
    post, machine, _ = shapeoko()
    for name in names:
        text = (out / name).read_text()
        assert len(re.findall(r"^M6T\d+$", text, re.M)) == 1
        assert not re.search(r"G8[0-3]\D|G10|G92", text)
        assert verify(text, post, machine, below_stock_mm=3.1).ok
    summary = json.loads((out / "cnc.json").read_text())
    assert summary["ok"] and summary["split_by_tool"]
    assert {h["section"] + h["side"] for h in summary["halves"]} == {
        "headright",
        "headleft",
        "tailright",
        "tailleft",
    }
    assert summary["feeds"]["pocket"]["chipload_status"] == "within"
    assert sorted(p.name for p in (out / "halves").glob("*.stl")) == [
        "head_left.stl",
        "head_right.stl",
        "tail_left.stl",
        "tail_right.stl",
    ]
    sheet = (out / "setup.md").read_text()
    assert "setup1_01_flat_3.175.nc" in sheet and "Chipload" in sheet


def test_fab_cnc_rejects_a_print_build(tmp_path):
    cfg = load_config(SAMPLE)
    build = create_build_dir(cfg, tmp_path)
    res = invoke("fab", str(build), "--format", "cnc")
    assert res.exit_code == 1
    assert "process.method = 'print'" in res.output
