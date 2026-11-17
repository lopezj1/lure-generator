from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("build")
    def build(
        variant: Path = typer.Argument(..., exists=True, dir_okay=False, help="Variant TOML."),
        set_: list[str] = typer.Option(
            [], "--set", help="Override, e.g. buoyancy.target_sink=suspend"
        ),
        out_root: Path = typer.Option(Path("builds"), help="Root folder for build outputs."),
    ) -> None:
        """Resolve a variant config and create a build folder.

        Geometry, mass properties and checks are added by later features (F2–F5).
        """
        from luregen.config import ConfigError, load_config
        from luregen.report.build_dir import create_build_dir

        try:
            cfg = load_config(variant, set_)
        except ConfigError as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        build_dir = create_build_dir(cfg, out_root)
        typer.echo(f"build: {build_dir}")
