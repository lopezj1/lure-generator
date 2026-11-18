"""FastAPI app factory: JSON API under /api, built SPA from web/dist at /."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from luregen import __version__
from luregen.api.jobs import JobManager
from luregen.api.jobs_routes import jobs_router
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
    jobs = JobManager(root)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await jobs.start()
        try:
            yield
        finally:
            await jobs.stop()

    app = FastAPI(title="luregen", version=__version__, lifespan=lifespan)
    app.state.root = root
    app.state.jobs = jobs
    app.include_router(health_router(root), prefix="/api")
    app.include_router(jobs_router(), prefix="/api")
    # W1 (#62): spec, variants, schema, uploads, builds, toolpaths.
    from luregen.api import builds, spec, uploads

    for router in (spec.spec_router, uploads.uploads_router, builds.builds_router):
        app.include_router(router(root), prefix="/api")
    # Mounted last: the catch-all static mount must not shadow /api routes.
    dist = root / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")
    return app
