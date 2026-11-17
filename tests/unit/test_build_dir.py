import json
from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.config import load_config, load_resolved
from luregen.report.build_dir import create_build_dir, resolve_build_dir


def test_two_builds_and_latest(sample_variant: Path, tmp_path: Path):
    cfg = load_config(sample_variant)
    out = tmp_path / "builds"
    first = create_build_dir(cfg, out, now=datetime(2026, 11, 17, 10, 0, 0))
    second = create_build_dir(cfg, out, now=datetime(2026, 11, 17, 10, 0, 1))
    assert first != second
    assert first.parent == out / "glide" / "striper_175_petg"
    assert resolve_build_dir(first.parent) == second
    assert load_resolved(second / "resolved.toml") == cfg
    report = json.loads((second / "report.json").read_text())
    assert report["status"] == "config_only"
    assert any("placeholder" in w for w in report["warnings"])
    assert (second / "report.md").read_text().startswith("# striper_175_petg")


def test_build_then_check_cli(sample_variant: Path, tmp_path: Path):
    app = make_app()
    runner = CliRunner()
    out = tmp_path / "builds"
    res = runner.invoke(
        app,
        [
            "build",
            str(sample_variant),
            "--out-root",
            str(out),
            "--set",
            "buoyancy.target_sink=suspend",
        ],
    )
    assert res.exit_code == 0, res.output
    res = runner.invoke(app, ["check", str(out / "glide" / "striper_175_petg")])
    assert res.exit_code == 0, res.output
    assert "suspend" in res.output and "oz" in res.output


def test_build_reports_config_errors(sample_variant: Path, tmp_path: Path):
    res = CliRunner().invoke(
        make_app(),
        [
            "build",
            str(sample_variant),
            "--out-root",
            str(tmp_path),
            "--set",
            "size.target_weight_oz=2",
        ],
    )
    assert res.exit_code == 1
    assert "exactly one" in res.output
