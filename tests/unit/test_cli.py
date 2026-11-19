import importlib
import sys
import types
from pathlib import Path

import typer
from typer.testing import CliRunner

from luregen.cli import make_app, register_all

EXPECTED = {"profile", "build", "check", "fab", "sync-profiles", "analyze-gcode"}


def test_help_lists_all_subcommands():
    result = CliRunner().invoke(make_app(), ["--help"])
    assert result.exit_code == 0
    for name in EXPECTED:
        assert name in result.output


def test_new_command_module_is_discovered_without_editing_cli(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "fakecmds"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "_private.py").write_text("raise RuntimeError('private modules are skipped')\n")
    (pkg / "hello.py").write_text(
        "import typer\n\n"
        "def register(app):\n"
        "    @app.command('hello')\n"
        "    def hello():\n"
        "        typer.echo('hi')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    fakecmds = importlib.import_module("fakecmds")
    app = typer.Typer()

    @app.callback()
    def _root() -> None: ...

    assert register_all(app, fakecmds) == ["hello"]
    result = CliRunner().invoke(app, ["hello"])
    assert result.output.strip() == "hi"
    sys.modules.pop("fakecmds", None)
    assert isinstance(fakecmds, types.ModuleType)


def test_fab_print_without_a_build_fails_cleanly(tmp_path):
    result = CliRunner().invoke(make_app(), ["fab", str(tmp_path / "x"), "--format", "print"])
    assert result.exit_code == 1
    assert "print error" in result.output
