from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from luregen.config import LureConfig
    from luregen.profile.pipeline import ProfileResult
    from luregen.profile.table import StationTable

# Trace length for a weight-driven photo build with `scale = "length"`: the build rescales
# the table to the solved length, so only the shape matters.
NOMINAL_TRACE_LENGTH_MM = 150.0


class BuildError(RuntimeError):
    pass


def trace_photo(cfg: "LureConfig") -> "ProfileResult | None":
    """Trace `profile.image` (`profile.source = "image"`) into a station table (#134)."""
    if cfg.profile.source != "image":
        return None
    from luregen.profile.extract import ProfileError
    from luregen.profile.pipeline import extract_profile, options_from_config

    p = cfg.profile
    for name, path in (("profile.image", p.image), ("profile.top_image", p.top_image)):
        if path is not None and not Path(path).is_file():
            raise BuildError(f"{name} not found: {path}")
    length = cfg.size.length_mm or cfg.size.solved_length_mm
    if length is None and p.scale == "length":
        length = NOMINAL_TRACE_LENGTH_MM
    try:
        return extract_profile(
            Path(p.image),
            options_from_config(p, length),
            n_stations=p.n_stations,
            top_image=Path(p.top_image) if p.top_image else None,
            width_ratio=p.width_rule.value,
        )
    except ProfileError as exc:
        raise BuildError(f"profile photo: {exc}") from exc


def write_traced_profile(traced: "ProfileResult", build_dir: Path) -> None:
    """The traced station table and its debug images, in `<build>/profile/`."""
    from luregen.profile.debug import write_debug

    out = build_dir / "profile"
    out.mkdir(parents=True, exist_ok=True)
    traced.table.to_csv(out / "stations.csv")
    write_debug(traced, out)


def station_table_for(cfg: "LureConfig", traced: "ProfileResult | None" = None) -> "StationTable":
    """The config's station table (the preset CSV, or `traced` from the photo), scaled to
    `size.length_mm`."""
    from luregen.profile.table import StationTable, StationTableError

    if cfg.profile.source == "image":
        table = (traced or trace_photo(cfg)).table
    else:
        preset = cfg.profile.preset
        if preset is None or not Path(preset).is_file():
            raise BuildError(f"profile.preset not found: {preset}")
        try:
            table = StationTable.load_csv(Path(preset))
        except StationTableError as exc:
            raise BuildError(str(exc)) from exc
    # weight-driven: the solved length, else the preset's own length (scaled in build_lure)
    length = cfg.size.length_mm or cfg.size.solved_length_mm
    if length is not None and abs(length - table.length) > 1e-9:
        table = table.scaled(length / table.length)
    return table


# Overall progress fraction at the start of each stage (monotonic; #74). `solve` (F4)
# runs the family build several times; without it the bar jumps to `joint`.
STAGES = {
    "config": 0.0,
    "stations": 0.02,
    "envelope": 0.04,
    "solve": 0.08,
    "joint": 0.70,
    "internals": 0.74,
    "massprops": 0.84,
    "export": 0.86,
    "dfm": 0.93,
    "report": 0.97,
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
    from luregen.config import dump_resolved
    from luregen.families import get_family
    from luregen.physics.balance import balance_build
    from luregen.physics.massprops import band_props
    from luregen.report.balance import DIAGRAM_PATH, balance_blocks, render_diagram

    stage_emit("envelope", 0.0, f"{cfg.process.body_style} envelope")
    env = build_envelope(cfg, table)
    scaling = None
    if cfg.size.target_weight_oz is not None and not cfg.solver.solved:
        cfg, table, scaling = scale_to_weight(cfg, table, env)
        stage_emit("envelope", 0.5, f"envelope at {table.length:.1f} mm for the target weight")
        env = build_envelope(cfg, table)
    family = get_family(cfg.family)
    outcome = balance_build(cfg, table, env, family, stage_emit)
    cfg, built = outcome.cfg, outcome.built
    (build_dir / "resolved.toml").write_text(dump_resolved(cfg))

    stage_emit("massprops", 0.0, "mass properties at the hardware band ends")
    props = band_props(built.mass_inputs)
    blocks = balance_blocks(cfg, table, props)

    model_dir = build_dir / "model"
    files = {}
    parts = [*built.sections.items(), *built.parts.items(), ("body", env.solid)]
    for i, (name, solid) in enumerate(parts):
        stage_emit("export", i / len(parts), f"model/{name}.*")
        files[name] = export_model(solid, model_dir, name)

    env_block = envelope_report(cfg, table, env, files["body"])
    dfm = run_dfm(cfg, model_dir, built.report, env_block)

    stage_emit("report", 0.0, "report and CG/CB diagram")
    render_diagram(build_dir / DIAGRAM_PATH, built.diagram, props, f"{cfg.name}: side view")
    sections = blocks["sections"]
    for name in built.sections:
        sections[name]["files"] = files[name]
    for name in built.parts:
        if isinstance(built.report.get(name), dict):
            built.report[name]["files"] = files[name]
    balance = outcome.report
    if scaling is not None:
        balance["scaling"] = scaling
    return {
        "envelope": env_block,
        "geometry": built.report,
        **blocks,
        "balance": balance,
        "diagram": DIAGRAM_PATH,
        "dfm": dfm,
        "family_warnings": built.warnings + balance["solver"].get("warnings", []),
        "_config": cfg,
    }


def run_dfm(cfg: "LureConfig", model_dir: Path, geometry: dict, envelope: dict) -> dict:
    """F5: evaluate the DFM rules on the exported sections; the report's `dfm` block."""
    from luregen.dfm import evaluate
    from luregen.dfm.model import DfmModel

    stage_emit("dfm", 0.0, "design-for-manufacture rules")
    model = DfmModel.load(model_dir, geometry, envelope)
    result = evaluate(model, cfg, emit=lambda f, msg: stage_emit("dfm", f, msg))
    return result.as_dict()


def scale_to_weight(cfg: "LureConfig", table: "StationTable", env):
    """Weight → length (F4): rescale the config and station table once."""
    from luregen.families import get_family
    from luregen.geometry.envelope import mass_properties
    from luregen.physics.scaling import (
        ScalingError,
        length_for_weight,
        scale_config,
        scaling_report,
        target_density,
        target_mass_g,
    )

    l_ref = table.length
    v0 = 1000.0 * mass_properties(env.solid)["volume_cm3"]
    mass, rho = target_mass_g(cfg), target_density(cfg)
    # The family's displaced volume vs the envelope: terms that scale with L³, L² and not
    # at all (e.g. the joint gap, a constant-thickness fin, hardware); envelope only if absent.
    terms = {"cubic": v0, "area": 0.0, "const": 0.0}
    family = get_family(cfg.family)
    if hasattr(family, "scaling_volumes"):
        terms = family.scaling_volumes(cfg, table, env)
    length = length_for_weight(l_ref, terms["cubic"], mass, rho, terms["area"], terms["const"])
    try:
        cfg = scale_config(cfg, l_ref, length)
    except ScalingError as exc:
        raise BuildError(str(exc)) from exc
    report = scaling_report(l_ref, v0, mass, rho, length)
    report["volume_terms_cm3"] = {k: round(v / 1000.0, 4) for k, v in terms.items()}
    return cfg, station_table_for(cfg), report


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
        each section (head, tail) plus the outer envelope (body). The balance solver tunes
        the internals (print: walls, keels, ballast) to the targets and writes the solved
        values into resolved.toml with solver.solved = true, so rebuilding that file
        reproduces the lure without solving; --set solver.solved=false solves again.
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
            traced = trace_photo(cfg)
            table = station_table_for(cfg, traced)
        except (ConfigError, BuildError) as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        build_dir = create_build_dir(cfg, out_root)
        typer.echo(f"build: {build_dir}")
        if traced is not None:
            write_traced_profile(traced, build_dir)
        try:
            result = build_lure(cfg, table, build_dir)
        except (GeometryError, FamilyError, RuntimeError) as exc:
            update_report(build_dir, cfg, status="error", error=str(exc))
            typer.echo(f"geometry error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        cfg = result.pop("_config")
        warnings = json_warnings(build_dir) + result.pop("family_warnings")
        if traced is not None:
            warnings += [f"profile photo: {n}" for n in traced.notes]
        status = "dfm_failed" if result["dfm"]["n_errors"] else "built"
        report = update_report(build_dir, cfg, status=status, warnings=warnings, **result)
        stage_emit("report", 1.0, "done")
        echo_summary(report)
        progress.result(build_dir=build_dir, dfm=report["dfm"]["status"])
        if status == "dfm_failed":
            typer.echo(f"DFM: {report['dfm']['n_errors']} error(s); build failed", err=True)
            raise typer.Exit(code=1)


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
    if bal := report.get("balance"):
        sol = bal["solver"]
        if sol["status"] == "skipped":
            typer.echo(f"solver: skipped ({sol['reason']})")
        else:
            typer.echo(
                f"solver: {sol['status']} in {sol['n_passes']} passes, {sol['builds']} builds, "
                f"{sol['seconds']:.0f} s ({sol['reason']})"
            )
        hb = bal["hardware_band"]
        ends = ", ".join(f"{k} {v['status']}" for k, v in hb["ends"].items())
        typer.echo(f"hardware band: {hb['status']} ({ends})")
    typer.echo(f"verdict: {report['verdict']['status']} ({report['verdict']['label']})")
    if dfm := report.get("dfm"):
        from luregen.dfm.report import echo_dfm

        echo_dfm(dfm, typer.echo)
    extra = [k for k in ("fin",) if (report.get("geometry") or {}).get(k)]
    parts = ", ".join([*report["sections"], *extra, "body"])
    typer.echo(f"  model: {parts}; diagram: {report['diagram']}")
