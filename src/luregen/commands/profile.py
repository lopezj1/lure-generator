from pathlib import Path
from typing import Literal

import typer


def register(app: typer.Typer) -> None:
    @app.command("profile")
    def profile(
        image: Path = typer.Argument(..., exists=True, dir_okay=False, help="Side-profile image."),
        out: Path = typer.Option(..., "-o", "--out", help="Station table CSV to write."),
        scale: Literal["aruco", "length", "auto"] = typer.Option(
            "auto", help="aruco, length, or auto (marker if found, else --length)."
        ),
        length: float | None = typer.Option(
            None, "--length", help="Known nose-to-tail length in mm (length scale)."
        ),
        marker_mm: float = typer.Option(30.0, help="Printed ArUco marker side in mm."),
        top: Path | None = typer.Option(
            None, "--top", exists=True, dir_okay=False, help="Top-view image for widths."
        ),
        width_ratio: float = typer.Option(
            0.34, help="Width/depth ratio when no top view is given."
        ),
        n_stations: int = typer.Option(23, "--n-stations", min=2, help="Interior stations."),
        nose: Literal["left", "right"] = typer.Option(
            "left", help="Which side of the image the nose faces."
        ),
        smoothing: Literal["efd", "spline"] = typer.Option("efd", help="Contour smoothing."),
        harmonics: int = typer.Option(20, min=4, max=60, help="EFD harmonics (fewer = smoother)."),
        rembg: bool = typer.Option(False, help="Use rembg background removal (optional)."),
    ) -> None:
        """Extract a station table from a side-profile image.

        Also writes profile_debug.png and profile_mask.png next to the CSV.
        """
        from luregen.profile.debug import write_debug
        from luregen.profile.extract import ProfileError
        from luregen.profile.pipeline import ExtractOptions, extract_profile

        opts = ExtractOptions(
            scale=scale,
            length_mm=length,
            marker_mm=marker_mm,
            nose=nose,
            smoothing=smoothing,
            harmonics=harmonics,
            use_rembg=rembg,
        )
        try:
            result = extract_profile(
                image, opts, n_stations=n_stations, top_image=top, width_ratio=width_ratio
            )
        except ProfileError as exc:
            typer.echo(f"profile error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        out.parent.mkdir(parents=True, exist_ok=True)
        result.table.to_csv(out)
        debug = write_debug(result, out.parent)
        t = result.table
        typer.echo(f"stations: {out} ({len(t)} stations)")
        typer.echo(
            f"length {t.length:.1f} mm, depth {t.max_depth:.1f} mm, width {t.max_width:.1f} mm"
        )
        typer.echo(f"scale: {result.side.scale.describe()}")
        for note in result.notes:
            typer.echo(f"note: {note}", err=True)
        typer.echo(f"debug: {', '.join(str(p) for p in debug)}")
