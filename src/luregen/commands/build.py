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


# Overall progress fraction at the start of each stage (monotonic; #74).
STAGES = {
    "config": 0.0,
    "stations": 0.05,
    "envelope": 0.10,
    "joint": 0.20,
    "internals": 0.35,
    "massprops": 0.65,
    "export": 0.70,
    "report": 0.95,
}


def stage_emit(stage: str, frac: float = 0.0, msg: str = "") -> None:
    """Emit `stage` progress with `frac` (0–1 within the stage) mapped to the whole build."""
    from luregen import progress

    names = list(STAGES)
    start = STAGES[stage]
    i = names.index(stage)
    end = STAGES[names[i + 1]] if i + 1 < len(names) else 1.0
    progress.emit(stage, round(start + (end - start) * min(max(frac, 0.0), 1.0), 4), msg)


def build_envelope(cfg: "LureConfig", table: "StationTable"):
    from luregen.geometry.envelope import build_envelope as _build

    return _build(
        table,
        style=cfg.process.body_style,
        section_shape=cfg.profile.section_shape,
        roundover_r=cfg.dfm.cnc.roundover_r_mm,
    )


def envelope_report(cfg: "LureConfig", table: "StationTable", env, files: dict) -> dict:
    """The `envelope` report block (F2) for the outer body."""
    from luregen.geometry.envelope import mass_properties

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
        "files": files,
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


def export_model(shape, model_dir: Path, stem: str) -> dict:
    """Write model/<stem>.step|stl|3mf; return file info with build-relative paths."""
    from luregen.geometry.export import export_all

    files = export_all(shape, model_dir, stem=stem)
    return {k: {**v, "path": f"model/{v['path']}"} for k, v in files.items()}


def build_lure(cfg: "LureConfig", table: "StationTable", build_dir: Path) -> dict:
    """Envelope → family sections → mass properties → exports → report blocks."""
    from luregen.families import get_family
    from luregen.physics.massprops import band_props
    from luregen.report.balance import DIAGRAM_PATH, balance_blocks, render_diagram

    stage_emit("envelope", 0.0, f"{cfg.process.body_style} envelope")
    env = build_envelope(cfg, table)
    family = get_family(cfg.family)
    built = family.build(cfg, table, env, stage_emit)

    stage_emit("massprops", 0.0, "mass properties at the hardware band ends")
    props = band_props(built.mass_inputs)
    blocks = balance_blocks(cfg, table, props)

    model_dir = build_dir / "model"
    files = {}
    parts = [*built.sections.items(), ("body", env.solid)]
    for i, (name, solid) in enumerate(parts):
        stage_emit("export", i / len(parts), f"model/{name}.*")
        files[name] = export_model(solid, model_dir, name)

    stage_emit("report", 0.0, "report and CG/CB diagram")
    render_diagram(build_dir / DIAGRAM_PATH, built.diagram, props, f"{cfg.name}: side view")
    sections = blocks["sections"]
    for name in built.sections:
        sections[name]["files"] = files[name]
    return {
        "envelope": envelope_report(cfg, table, env, files["body"]),
        "geometry": built.report,
        **blocks,
        "diagram": DIAGRAM_PATH,
        "family_warnings": built.warnings,
    }


def register(app: typer.Typer) -> None:
    @app.command("build")
    def build(
        variant: Path = typer.Argument(..., exists=True, dir_okay=False, help="Variant TOML."),
        set_: list[str] = typer.Option(
            [], "--set", help="Override, e.g. buoyancy.target_sink=suspend"
        ),
        out_root: Path = typer.Option(Path("builds"), help="Root folder for build outputs."),
    ) -> None:
        """Resolve a variant, build the sections and their internals, measure and report.

        Writes resolved.toml, report.json/md, cg_cb.png and model/<part>.step|stl|3mf for
        each section (head, tail) plus the outer envelope (body). Solving the internals to
        meet the balance targets is F4; the report's verdict only compares.
        """
        from luregen import progress
        from luregen.config import ConfigError, load_config
        from luregen.families import FamilyError
        from luregen.geometry.validate import GeometryError
        from luregen.report.build_dir import create_build_dir, update_report

        stage_emit("config", 0.0, str(variant))
        try:
            cfg = load_config(variant, set_)
            stage_emit("stations", 0.0, "station table")
            table = station_table_for(cfg)
        except (ConfigError, BuildError) as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        build_dir = create_build_dir(cfg, out_root)
        typer.echo(f"build: {build_dir}")
        try:
            result = build_lure(cfg, table, build_dir)
        except (GeometryError, FamilyError, RuntimeError) as exc:
            update_report(build_dir, cfg, status="error", error=str(exc))
            typer.echo(f"geometry error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        warnings = json_warnings(build_dir) + result.pop("family_warnings")
        report = update_report(build_dir, cfg, status="built", warnings=warnings, **result)
        stage_emit("report", 1.0, "done")
        echo_summary(report)
        progress.result(build_dir=build_dir)


def json_warnings(build_dir: Path) -> list[str]:
    import json

    return list(json.loads((build_dir / "report.json").read_text()).get("warnings", []))


def echo_summary(report: dict) -> None:
    env = report["envelope"]
    typer.echo(f"envelope: {env['style']}, {env['volume_cm3']:.2f} cm³")
    if "roundover" in env:
        typer.echo(f"  {env['roundover']['note']}")
    for name, s in report["sections"].items():
        typer.echo(
            f"{name}: {s['mass_g']:.2f} g, {s['volume_cm3']:.2f} cm³, {s['density']:.3f} g/cm³"
        )
    lure = report["lure"]
    typer.echo(
        f"lure: {lure['mass_g']:.2f} g ({lure['mass_oz']:.2f} oz), {lure['density']:.3f} g/cm³, "
        f"CG−CB {lure['cg_minus_cb_x_mm']:+.2f} mm, CB−CG {lure['cb_minus_cg_z_mm']:+.2f} mm, "
        f"head:tail {lure.get('head_tail_mass_ratio', float('nan')):.2f}"
    )
    typer.echo(f"verdict: {report['verdict']['status']} ({report['verdict']['label']})")
    parts = ", ".join([*report["sections"], "body"])
    typer.echo(f"  model: {parts}; diagram: {report['diagram']}")
