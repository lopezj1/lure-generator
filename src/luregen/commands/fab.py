from enum import StrEnum
from pathlib import Path

import typer

from luregen.commands._stub import not_implemented


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
    ) -> None:
        """Generate manufacturing output (G-code) for a build."""
        if format == Format.cnc:
            fab_cnc(build, machine)
            return
        not_implemented("F6 (#25)")


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
