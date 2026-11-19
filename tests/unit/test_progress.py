import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen import progress
from luregen.cli import make_app
from luregen.report.build_dir import GitInfo, git_info


def parse(stderr: str, prefix: str) -> list[dict]:
    return [
        json.loads(line[len(prefix) :]) for line in stderr.splitlines() if line.startswith(prefix)
    ]


def test_progress_is_silent_by_default(monkeypatch, capsys):
    monkeypatch.delenv(progress.ENV, raising=False)
    progress.emit("stage", 0.5, "msg")
    progress.result(path=Path("x"))
    assert capsys.readouterr() == ("", "")


def test_progress_line_format(monkeypatch, capsys):
    monkeypatch.setenv(progress.ENV, "1")
    progress.emit("envelope", 0.25, "lofting")
    progress.emit("slice")
    progress.result(build_dir=Path("builds/a"), n=2)
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.splitlines()
    assert lines[0] == '@@progress {"stage": "envelope", "frac": 0.25, "msg": "lofting"}'
    assert parse(err, "@@progress ")[1] == {"stage": "slice", "frac": None, "msg": ""}
    assert parse(err, "@@result ") == [{"build_dir": "builds/a", "n": 2}]


def test_progress_json_flag_sets_env(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(progress.ENV, raising=False)
    CliRunner().invoke(make_app(), ["--progress", "json", "check", str(tmp_path)])
    assert os.environ.get(progress.ENV) == "1"
    bad = CliRunner().invoke(make_app(), ["--progress", "xml", "check", str(tmp_path)])
    assert bad.exit_code == 2


@pytest.mark.slow
def test_build_emits_stages_and_result_via_python_m(sample_variant: Path, tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != progress.ENV}
    cmd = [sys.executable, "-m", "luregen", "--progress", "json", "build", str(sample_variant)]
    cnc = ["--set", "process.method=cnc", "--set", "process.material=basswood"]  # faster
    proc = subprocess.run(
        [*cmd, *cnc, "--out-root", str(tmp_path)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    events = parse(proc.stderr, "@@progress ")
    stages = list(dict.fromkeys(e["stage"] for e in events))
    assert stages == [
        "config",
        "stations",
        "envelope",
        "joint",
        "internals",
        "massprops",
        "export",
        "dfm",
        "report",
    ]
    fracs = [e["frac"] for e in events]
    assert fracs == sorted(fracs) and fracs[0] == 0.0 and fracs[-1] == 1.0
    (res,) = parse(proc.stderr, "@@result ")
    latest = tmp_path / "glide" / "striper_175_petg" / "latest"
    assert Path(res["build_dir"]).resolve() == latest.resolve()


def test_git_info_env_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no git binary
    monkeypatch.delenv("LUREGEN_GIT_SHA", raising=False)
    monkeypatch.delenv("LUREGEN_GIT_DIRTY", raising=False)
    assert git_info(tmp_path) == GitInfo(sha="nogit", dirty=False)
    monkeypatch.setenv("LUREGEN_GIT_SHA", "0123456789abcdef")
    monkeypatch.setenv("LUREGEN_GIT_DIRTY", "1")
    info = git_info(tmp_path)
    assert (info.sha, info.dirty) == ("0123456", True)


def test_profile_emits_result_with_root_relative_paths(monkeypatch, tmp_path: Path, capsys):
    import cv2
    from _profile_synth import Scene, lure_outline

    outline = lure_outline(175, 50)
    scene = Scene(220, 80)
    scene.polygon(outline + [22.5, 45])
    (tmp_path / "uploads").mkdir()
    cv2.imwrite(str(tmp_path / "uploads" / "side.png"), scene.render())
    monkeypatch.chdir(tmp_path)  # API jobs run with cwd = data root
    monkeypatch.setenv(progress.ENV, "1")
    args = ["profile", "uploads/side.png", "-o", "uploads/p/stations.csv", "--length", "175"]
    res = CliRunner().invoke(make_app(), args)
    assert res.exit_code == 0, res.output
    (result,) = parse(capsys.readouterr().err + res.stderr, "@@result ")
    assert result["csv"] == "uploads/p/stations.csv"
    assert result["debug_png"] == "uploads/p/profile_debug.png"
    assert result["mask_png"] == "uploads/p/profile_mask.png"
    assert result["mask_top_png"] is None
    assert result["stations"] == 23 and result["length_mm"] == 175
    assert all((tmp_path / result[k]).is_file() for k in ("csv", "debug_png", "mask_png"))
