import typer

from luregen.commands._stub import not_implemented


def register(app: typer.Typer) -> None:
    @app.command("sync-profiles")
    def sync_profiles(
        printer: str = typer.Argument(..., help="Printer folder under spec/printers/."),
    ) -> None:
        """Fetch and flatten slicer profiles for a printer."""
        not_implemented("#31")
