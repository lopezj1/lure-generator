import json
import os
import subprocess
import sys
from pathlib import Path

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


def test_build_emits_result_via_python_m(sample_variant: Path, tmp_path: Path):
    env = {k: v for k, v in os.environ.items() if k != progress.ENV}
    cmd = [sys.executable, "-m", "luregen", "--progress", "json", "build", str(sample_variant)]
    proc = subprocess.run(
        [*cmd, "--out-root", str(tmp_path)], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
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
