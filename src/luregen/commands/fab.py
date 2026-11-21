from enum import StrEnum
from pathlib import Path

import typer


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
        parts: list[str] = typer.Option(
            [],
            "--parts",
            help="Print only these parts (head, tail, fin; repeatable or comma-separated). "
            "A CNC build prints its printed parts (the TPU fin).",
        ),
    ) -> None:
        """Generate manufacturing output (G-code) for a build."""
        if format == Format.cnc:
            if parts:
                typer.echo("--parts applies to --format print", err=True)
                raise typer.Exit(code=2)
            fab_cnc(build, machine)
            return
        names = [p.strip() for item in parts for p in item.split(",") if p.strip()]
        fab_print(build, machine, names or None)


def fab_cnc(build: Path, machine: str | None) -> None:
    """CNC halves: two setups, posted per tool, verified, into <build>/cnc/ (F7, #27)."""
    from luregen import progress
    from luregen.fab.router.cam.job import CncJobError, run
    from luregen.fab.router.cam.ops2d import CamError
    from luregen.report.build_dir import resolve_build_dir

    try:
        folder = resolve_build_dir(build)
        result = run(folder, Path("spec"), machine)
    except (CncJobError, CamError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"cnc error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    s = result.summary
    stock = s["stock_mm"]
    typer.echo(f"cnc: {result.out_dir}")
    typer.echo(f"stock {stock['length']} x {stock['width']} x {stock['thickness']} mm")
    for f in result.files:
        m, sec = divmod(round(f.runtime_s), 60)
        status = "ok" if f.ok else "VERIFY FAILED"
        typer.echo(f"  {f.path}  {', '.join(f.tools)}  {m}:{sec:02d}  {status}")
        for issue in f.issues:
            typer.echo(f"    {issue}")
    m, sec = divmod(round(s["runtime_s"]), 60)
    typer.echo(f"total est. {m}:{sec:02d}; setup sheet: {result.out_dir / 'setup.md'}")
    for w in result.warnings:
        typer.echo(f"warning: {w}")
    progress.result(out_dir=result.out_dir, files=[f.path for f in result.files], ok=result.ok)
    if not result.ok:
        raise typer.Exit(code=1)


def fab_print(build: Path, machine: str | None, parts: list[str] | None = None) -> None:
    """Print sections: oriented upright, sliced, paused, verified into <build>/fab/print/."""
    from luregen import progress
    from luregen.fab.printer.job import PrintJobError, fmt_time, run
    from luregen.fab.printer.machine import MachineConfigError
    from luregen.fab.printer.orca import SlicerError
    from luregen.fab.printer.orient import OrientError
    from luregen.fab.printer.plan import PlanError
    from luregen.report.build_dir import resolve_build_dir

    try:
        folder = resolve_build_dir(build)
        result = run(folder, Path("spec"), machine, parts=parts)
    except (
        PrintJobError,
        PlanError,
        SlicerError,
        OrientError,
        MachineConfigError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        typer.echo(f"print error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    plan = result.summary["plan"]
    iface = plan["interface"]
    typer.echo(f"print: {result.out_dir} ({result.summary['slicer']})")
    typer.echo(
        f"body {plan['body']['tool']} {plan['body']['material']}; supports "
        f"{'on' if plan['supports'] else 'off'}; interface "
        f"{iface['tool'] + ' ' + iface['material'] if iface else 'none'}"
    )
    for s in result.sections:
        st = s.stats
        status = "ok" if s.ok else "FAILED"
        pause = f"pause layer {s.pause['layer']} Z {s.pause['layer_z_mm']}" if s.pause else ""
        typer.echo(
            f"  {s.gcode or s.name}  {s.tool} {s.material}  {fmt_time(st.get('time_s'))}  "
            f"{st.get('filament_g')} g "
            f"(interface {st.get('interface_g')} g)  {st.get('tool_changes')} tool changes  "
            f"{st.get('layers')} layers  {pause}  {status}"
        )
        for issue in s.issues:
            typer.echo(f"    {issue}")
    t = result.summary["totals"]
    typer.echo(
        f"total {fmt_time(t['time_s'])}, {t['filament_g']} g; report: {result.out_dir / 'print.md'}"
    )
    for w in result.warnings:
        typer.echo(f"warning: {w}")
    progress.result(out_dir=result.out_dir, files=result.files, ok=result.ok)
    if not result.ok:
        raise typer.Exit(code=1)
