from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("toolpath")
    def toolpath(
        gcode: Path = typer.Argument(..., help="Print or CNC G-code file.", exists=True),
        kind: str = typer.Option("auto", help="print, cnc or auto (detect from the content)."),
        out: Path | None = typer.Option(
            None, "-o", "--out", help="Output .bin (default: <file>.<kind>.bin, cached)."
        ),
        stats: bool = typer.Option(False, "--stats", help="Print the header stats as JSON."),
        tolerance: float = typer.Option(0.02, help="Arc chord tolerance in mm."),
        force: bool = typer.Option(False, "--force", help="Re-encode even if the cache is fresh."),
    ) -> None:
        """Encode G-code into the compact binary toolpath format used by the web viewers."""
        import json
        import time

        from luregen.viewer.binfmt import cache_path, encode_file, read_file_header, resolve_kind

        if kind not in ("auto", "print", "cnc"):
            raise typer.BadParameter("kind must be print, cnc or auto", param_hint="--kind")
        resolved = resolve_kind(gcode, kind)
        target = out or cache_path(gcode, resolved)
        before = target.stat().st_mtime_ns if target.exists() else None
        t0 = time.perf_counter()
        path = encode_file(gcode, resolved, out, tolerance=tolerance, force=force)
        elapsed = time.perf_counter() - t0
        header = read_file_header(path)
        reused = before is not None and path.stat().st_mtime_ns == before
        parts = [f"{header['segments']} segments"]
        if resolved == "print":
            parts.append(f"{header['layer_count']} layers")
        parts.append(f"{len(header['tools'])} tools")
        parts.append(f"{len(header['markers'])} markers")
        note = "cached" if reused else f"{elapsed:.2f} s"
        typer.echo(
            f"{path} ({resolved}: {', '.join(parts)}; {path.stat().st_size / 1e6:.2f} MB, {note})"
        )
        if stats:
            typer.echo(json.dumps(header["stats"], indent=2))
