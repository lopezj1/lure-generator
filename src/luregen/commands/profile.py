from pathlib import Path

import typer

from luregen.commands._stub import not_implemented


def register(app: typer.Typer) -> None:
    @app.command("profile")
    def profile(
        image: Path = typer.Argument(..., help="Side-profile image."),
        out: Path = typer.Option(..., "-o", "--out", help="Station table CSV to write."),
    ) -> None:
        """Extract a station table from a side-profile image."""
        not_implemented("F1 (#8)")
