from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("sync-profiles")
    def sync_profiles(
        printer: str = typer.Argument(..., help="Printer folder under spec/printers/."),
        app_key: list[str] = typer.Option(
            [], "--app", help="Slicer app key from machine.toml (repeatable; default: all)."
        ),
        spec_root: Path = typer.Option(Path("spec"), help="Root of the spec/ tree."),
    ) -> None:
        """Fetch and flatten slicer profiles for a printer at its pinned slicer tag."""
        from luregen.fab.printer.machine import MachineConfigError, load_machine
        from luregen.fab.printer.profiles import ProfileError
        from luregen.fab.printer.profiles import sync_profiles as sync

        try:
            machine = load_machine(spec_root / "printers" / printer)
            for key in app_key or list(machine.apps):
                slicer = machine.slicer(key)
                typer.echo(f"{slicer.label} {slicer.tag} ({slicer.repo})")
                for path in sync(machine, key):
                    typer.echo(f"  wrote {path}")
        except (MachineConfigError, ProfileError) as exc:
            typer.echo(f"sync-profiles: {exc}", err=True)
            raise typer.Exit(code=1) from exc
