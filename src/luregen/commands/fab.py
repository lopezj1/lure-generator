from enum import StrEnum
from pathlib import Path

import typer

from luregen.commands._stub import not_implemented


class Format(StrEnum):
    print = "print"
    cnc = "cnc"


def register(app: typer.Typer) -> None:
    @app.command("fab")
    def fab(
        build: Path = typer.Argument(..., help="Build folder (or its `latest` link)."),
        format: Format = typer.Option(
            ..., "--format", help="print → spec/printers, cnc → spec/routers."
        ),
        machine: str | None = typer.Option(
            None, help="Machine folder name; defaults to the only one."
        ),
    ) -> None:
        """Generate manufacturing output (G-code) for a build."""
        not_implemented("F6 (#25) / F7 (#27)")
