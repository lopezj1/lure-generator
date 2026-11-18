from pathlib import Path

import typer

from luregen.commands._stub import not_implemented


def register(app: typer.Typer) -> None:
    @app.command("analyze-gcode")
    def analyze_gcode(
        gcode: Path = typer.Argument(..., help="Reference G-code file."),
    ) -> None:
        """Infer post-processor settings from a reference G-code file."""
        not_implemented("#38")
