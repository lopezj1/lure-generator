from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("analyze-gcode")
    def analyze_gcode(
        gcode: Path = typer.Argument(..., help="Reference G-code file.", exists=True),
        machine: str | None = typer.Option(
            None, help="Router folder under spec/routers to diff against; defaults to the only one."
        ),
        spec: Path = typer.Option(Path("spec"), help="Spec root."),
        out: Path | None = typer.Option(None, help="Also write the proposed post.toml here."),
    ) -> None:
        """Infer post-processor settings from a reference G-code file."""
        from luregen.fab.router.analyze import analyze, diff, propose, summary, to_toml
        from luregen.fab.router.post_config import load_post

        current = None
        current_path = _current_post(spec, machine)
        if current_path is not None:
            current = load_post(current_path)
        result = analyze(gcode.read_text(errors="replace"))
        proposed = propose(result, current)
        typer.echo(f"# Inferred from {gcode.name}\n{summary(result)}\n")
        typer.echo("# Proposed post.toml\n" + to_toml(proposed))
        if current_path is not None:
            d = diff(current, proposed)
            typer.echo(f"# Diff against {current_path}\n" + (d or "(no changes)\n"))
        if out is not None:
            out.write_text(to_toml(proposed))
            typer.echo(f"wrote {out}")


def _current_post(spec: Path, machine: str | None) -> Path | None:
    routers = spec / "routers"
    if machine is None:
        found = sorted(p.parent for p in routers.glob("*/post.toml"))
        if len(found) != 1:
            return None
        return found[0] / "post.toml"
    path = routers / machine / "post.toml"
    if not path.is_file():
        raise typer.BadParameter(f"no post.toml at {path}")
    return path
