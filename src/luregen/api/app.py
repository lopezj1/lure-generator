"""FastAPI app factory: JSON API under /api, built SPA from web/dist at /."""

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from luregen import __version__
from luregen.report.build_dir import git_info


class Health(BaseModel):
    status: str
    version: str
    git_sha: str


def health_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["meta"])
    git = git_info(root)
    sha = git.sha + ("-dirty" if git.dirty else "")

    @router.get("/health")
    def health() -> Health:
        return Health(status="ok", version=__version__, git_sha=sha)

    return router


def create_app(root: Path) -> FastAPI:
    """Build the app for data root `root` (the repo checkout: spec/, builds/, web/dist)."""
    root = root.resolve()
    app = FastAPI(title="luregen", version=__version__)
    app.state.root = root
    app.include_router(health_router(root), prefix="/api")
    # Mounted last: the catch-all static mount must not shadow /api routes.
    dist = root / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")
    return app
