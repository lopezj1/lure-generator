"""End-to-end balance solver on the sample variant (slow: several family builds)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen.cli import make_app

SAMPLE = Path(__file__).resolve().parents[2] / "spec/designs/glide/variants/striper_175_petg.toml"


def run_build(variant: Path, out: Path, *sets: str):
    args = ["build", str(variant), "--out-root", str(out)]
    for s in sets:
        args += ["--set", s]
    return CliRunner().invoke(make_app(), args)


def latest(root: Path) -> Path:
    return (root / "glide" / "striper_175_petg" / "latest").resolve()


@pytest.fixture(scope="module")
def solved(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("solved")
    res = run_build(SAMPLE, root)
    assert res.exit_code == 0, res.output
    return latest(root)


@pytest.mark.slow
def test_sample_converges_with_level_hang_at_both_band_ends(solved):
    report = json.loads((solved / "report.json").read_text())
    sol = report["balance"]["solver"]
    trace = [
        (p["pass"], p["hard_ok"], {k: r["value"] for k, r in p["residuals"].items()})
        for p in sol["passes"]
    ]
    assert sol["status"] == "converged", (sol["reason"], trace)
    assert sol["n_passes"] <= 5
    assert all(r["ok"] for r in sol["final"].values() if r["hard"])
    band = report["balance"]["hardware_band"]
    assert band["status"] == "pass"
    for end in ("min", "max"):
        assert abs(band["ends"][end]["as_built"]["cg_minus_cb_x_mm"]) <= 1.0
        assert band["ends"][end]["trim"]["action"]
    # the verdict is the same data: sink and level hold at both ends
    checks = {c["id"]: c for c in report["verdict"]["checks"]}
    assert checks["level"]["ok"]
    # report.md renders the solver section
    md = (solved / "report.md").read_text()
    assert "## Balance solver" in md and "### Hardware band ends" in md


@pytest.mark.slow
def test_resolved_toml_records_the_solution_and_rebuilds_without_solving(solved, tmp_path):
    import tomllib

    resolved = tomllib.loads((solved / "resolved.toml").read_text())
    assert resolved["solver"]["solved"] is True
    assert "ballast.mass_g" in resolved["solver"]["solved_knobs"]
    report = json.loads((solved / "report.json").read_text())
    knobs = report["balance"]["solver"]["knobs"]
    assert resolved["ballast"]["mass_g"] == pytest.approx(knobs["ballast.mass_g"]["solved"])
    assert resolved["internals"]["trim_fill_g"] > 0
    assert not report["geometry"]["ballast"]["pinned"]

    res = run_build(solved / "resolved.toml", tmp_path)
    assert res.exit_code == 0, res.output
    again = json.loads((latest(tmp_path) / "report.json").read_text())
    assert again["balance"]["solver"]["status"] == "skipped"
    assert again["lure"]["mass_g"] == pytest.approx(report["lure"]["mass_g"], abs=1e-3)
    assert again["lure"]["cg_minus_cb_x_mm"] == pytest.approx(
        report["lure"]["cg_minus_cb_x_mm"], abs=1e-3
    )


@pytest.mark.slow
def test_weight_target_scales_the_body(tmp_path):
    from luregen.commands.build import build_lure, station_table_for
    from luregen.config import load_config
    from luregen.physics.scaling import length_for_weight
    from luregen.report.build_dir import create_build_dir

    cfg = load_config(SAMPLE, ["solver.enabled=false"])
    cfg.size.length_mm, cfg.size.target_weight_oz = None, 2.0
    table = station_table_for(cfg)
    assert table.length == pytest.approx(175.0)  # the preset's own length until solved
    out = build_lure(cfg, table, create_build_dir(cfg, tmp_path))
    sc = out["balance"]["scaling"]
    expect = length_for_weight(
        175.0, 1000 * sc["reference_volume_cm3"], sc["target_mass_g"], sc["target_density"]
    )
    assert sc["length_mm"] == pytest.approx(expect, rel=1e-4)
    assert out["envelope"]["length_mm"] == pytest.approx(sc["length_mm"], abs=1e-3)
    final = out["_config"]
    assert final.size.solved_length_mm == pytest.approx(sc["length_mm"], abs=1e-3)
    s = sc["scale"]
    assert final.hardware[-1].x_mm == pytest.approx(cfg.hardware[-1].x_mm * s, abs=1e-2)
