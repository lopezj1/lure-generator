"""`lure` entry point.

Subcommands are discovered from `luregen.commands`: every non-private module there must
define `register(app: typer.Typer) -> None`. Features add a module instead of editing this
file. Keep heavy imports (build123d, OpenCV, …) inside command functions so `--help` is fast.
"""

import importlib
import os
import pkgutil
from enum import StrEnum
from types import ModuleType

import typer

from luregen import commands


def register_all(app: typer.Typer, package: ModuleType = commands) -> list[str]:
    """Import every public module in `package` and call its `register(app)`."""
    registered = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        module.register(app)
        registered.append(info.name)
    return registered


class ProgressFormat(StrEnum):
    json = "json"


def _root(
    progress: ProgressFormat | None = typer.Option(
        None, help="Emit @@progress/@@result lines on stderr (see luregen.progress)."
    ),
) -> None:
    if progress is ProgressFormat.json:
        os.environ["LUREGEN_PROGRESS"] = "1"


def make_app() -> typer.Typer:
    app = typer.Typer(
        no_args_is_help=True,
        add_completion=False,
        help="Lure generator: profile image + TOML spec → balanced CAD → print/CNC G-code.",
    )
    app.callback()(_root)
    register_all(app)
    return app


def main() -> None:
    make_app()()
