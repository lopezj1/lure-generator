from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("check")
    def check(
        build: Path = typer.Argument(
            ..., help="Build folder, its `latest` link, or a variant folder."
        ),
    ) -> None:
        """Reload a build's resolved config and summarize it.

        Mass-property and DFM re-checks are added by F3–F5.
        """
        from luregen.config import ConfigError, load_resolved
        from luregen.report.build_dir import resolve_build_dir
        from luregen.units import g_to_oz

        try:
            build_dir = resolve_build_dir(build)
            cfg = load_resolved(build_dir / "resolved.toml")
        except (FileNotFoundError, ConfigError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        lo = sum(h.mass_g[0] * h.qty for h in cfg.hardware)
        hi = sum(h.mass_g[1] * h.qty for h in cfg.hardware)
        typer.echo(f"{cfg.name} ({cfg.family}) @ {build_dir.name}")
        typer.echo(
            f"  process: {cfg.process.method}/{cfg.process.body_style}, "
            f"{cfg.process.material} ({cfg.process.material_density} g/cm³)"
        )
        typer.echo(
            f"  target: {cfg.buoyancy.target_sink} in {cfg.buoyancy.water} water "
            f"({cfg.buoyancy.water_density} g/cm³)"
        )
        typer.echo(
            f"  hardware: {len(cfg.hardware)} items, {lo:.2f}–{hi:.2f} g "
            f"({g_to_oz(lo):.3f}–{g_to_oz(hi):.3f} oz)"
        )
