from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.profile.table import StationTable


class BuildError(RuntimeError):
    pass


def station_table_for(cfg: "LureConfig") -> "StationTable":
    """Load the config's station table and scale it to `size.length_mm`."""
    from luregen.profile.table import StationTable, StationTableError

    if cfg.profile.source == "image":
        raise BuildError(
            "profile.source = 'image' is not supported by `lure build` yet: run `lure profile` "
            "to extract a station table, then point profile.preset at the CSV"
        )
    if cfg.size.target_weight_oz is not None:
        raise BuildError(
            "size.target_weight_oz is solved in F4 (weight → length); set size.length_mm"
        )
    if cfg.profile.preset is None or not Path(cfg.profile.preset).is_file():
        raise BuildError(f"profile.preset not found: {cfg.profile.preset}")
    try:
        table = StationTable.load_csv(Path(cfg.profile.preset))
    except StationTableError as exc:
        raise BuildError(str(exc)) from exc
    if cfg.size.length_mm is not None and abs(cfg.size.length_mm - table.length) > 1e-9:
        table = table.scaled(cfg.size.length_mm / table.length)
    return table


def build_envelope_files(cfg: "LureConfig", table: "StationTable", build_dir: Path) -> dict:
    """Build the envelope per process.body_style, export model/body.*, return report data."""
    from luregen.geometry.envelope import build_envelope, mass_properties
    from luregen.geometry.export import export_all

    env = build_envelope(
        table,
        style=cfg.process.body_style,
        section_shape=cfg.profile.section_shape,
        roundover_r=cfg.dfm.cnc.roundover_r_mm,
    )
    files = export_all(env.solid, build_dir / "model", stem="body")
    props = mass_properties(env.solid)
    density = float(cfg.process.material_density or 0.0)
    report = {
        "style": env.style,
        "section_shape": env.section_shape,
        "n_stations": len(table),
        "length_mm": round(table.length, 3),
        "scale": float(table.meta.get("scaled_by", 1.0)),
        "source": table.source,
        **props,
        "material_density": density,
        "solid_mass_g": round(props["volume_cm3"] * density, 3),
        "files": {k: {**v, "path": f"model/{v['path']}"} for k, v in files.items()},
    }
    if env.roundover is not None:
        ro = env.roundover
        report["roundover"] = {
            "requested_mm": ro.requested_mm,
            "applied_mm": round(ro.applied_mm, 4),
            "limited_by": ro.limited_by,
            "half_thickness_mm": round(ro.half_thickness_mm, 4),
            "min_curvature_radius_mm": round(ro.min_curvature_radius_mm, 4),
            "note": ro.describe(),
        }
    return report


def register(app: typer.Typer) -> None:
    @app.command("build")
    def build(
        variant: Path = typer.Argument(..., exists=True, dir_okay=False, help="Variant TOML."),
        set_: list[str] = typer.Option(
            [], "--set", help="Override, e.g. buoyancy.target_sink=suspend"
        ),
        out_root: Path = typer.Option(Path("builds"), help="Root folder for build outputs."),
    ) -> None:
        """Resolve a variant config, build the body envelope and export it.

        Writes resolved.toml, report.json/md and model/body.step|stl|3mf. Joint, internals,
        mass properties and checks are added by later features (F3–F5).
        """
        from luregen import progress
        from luregen.config import ConfigError, load_config
        from luregen.geometry.validate import GeometryError
        from luregen.report.build_dir import create_build_dir, update_report

        try:
            cfg = load_config(variant, set_)
            table = station_table_for(cfg)
        except (ConfigError, BuildError) as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        build_dir = create_build_dir(cfg, out_root)
        typer.echo(f"build: {build_dir}")
        try:
            envelope = build_envelope_files(cfg, table, build_dir)
        except (GeometryError, RuntimeError) as exc:
            update_report(build_dir, cfg, status="error", error=f"envelope: {exc}")
            typer.echo(f"geometry error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        update_report(build_dir, cfg, status="envelope", envelope=envelope)
        typer.echo(
            f"envelope: {envelope['style']}, {envelope['volume_cm3']:.2f} cm³, "
            f"solid {envelope['solid_mass_g']:.1f} g at {envelope['material_density']} g/cm³"
        )
        if "roundover" in envelope:
            typer.echo(f"  {envelope['roundover']['note']}")
        typer.echo(f"  model: {', '.join(f['path'] for f in envelope['files'].values())}")
        progress.result(build_dir=build_dir)
