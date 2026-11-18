import json
from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("serve")
    def serve(
        host: str = typer.Option("127.0.0.1", help="Bind address (LAN only, no auth)."),
        port: int = typer.Option(8000, help="Port."),
        root: Path = typer.Option(
            Path("."), file_okay=False, help="Data root: the repo checkout with spec/ and builds/."
        ),
        dump_openapi: Path | None = typer.Option(
            None, help="Write the OpenAPI schema to this file and exit."
        ),
    ) -> None:
        """Serve the web app: JSON API under /api and the built SPA from web/dist."""
        try:
            import uvicorn

            from luregen.api.app import create_app
        except ImportError as exc:
            typer.echo(
                f"error: the web dependencies are missing ({exc.name}); "
                "install them with `uv sync --group web`",
                err=True,
            )
            raise typer.Exit(code=1) from exc

        api = create_app(root)
        if dump_openapi is not None:
            dump_openapi.write_text(json.dumps(api.openapi(), indent=2) + "\n")
            typer.echo(f"openapi: {dump_openapi}")
            return
        uvicorn.run(api, host=host, port=port)
