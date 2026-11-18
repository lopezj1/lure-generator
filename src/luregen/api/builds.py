"""builds/ endpoints: list builds, serve their files, and serve G-code as viewer toolpaths."""

import contextlib
import gzip
import json
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse

from luregen.api.models import BuildFile, BuildSummary
from luregen.api.safety import NAME_PATTERN, Name, check_relpath, inside
from luregen.api.uploads import GCODE_SUFFIXES

MEDIA_TYPES = {
    ".json": "application/json",
    ".toml": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".stl": "model/stl",
    ".step": "model/step",
    ".stp": "model/step",
    ".3mf": "model/3mf",
    ".glb": "model/gltf-binary",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".bin": "application/octet-stream",
    **dict.fromkeys(GCODE_SUFFIXES, "text/plain; charset=utf-8"),
}
_CACHE_SUFFIXES = (".print.bin", ".cnc.bin", ".print.bin.gz", ".cnc.bin.gz")
_BINARY = {200: {"content": {"application/octet-stream": {}}, "description": "Toolpath v1"}}
ToolpathKind = Literal["auto", "print", "cnc"]


def media_type(path: Path) -> str:
    known = MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
    return known or "application/octet-stream"


def _visible(path: Path) -> bool:
    return not path.name.startswith(".") and not path.name.endswith(_CACHE_SUFFIXES)


def _files(build: Path) -> list[BuildFile]:
    out = []
    for dirpath, dirnames, filenames in os.walk(build):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if _visible(path) and path.is_file():
                rel = path.relative_to(build).as_posix()
                out.append(BuildFile(name=rel, size=path.stat().st_size))
    return out


def _subdirs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and _visible(p))


def _gzipped(source: Path) -> Path:
    """`<source>.gz`, recompressed when older than `source` (atomic write)."""
    target = source.with_name(source.name + ".gz")
    try:
        if target.stat().st_mtime >= source.stat().st_mtime:
            return target
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with (
            open(source, "rb") as src,
            os.fdopen(fd, "wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz,
        ):
            shutil.copyfileobj(src, gz)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def builds_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["builds"])
    builds = root / "builds"

    def build_dir(family: str, variant: str, stamp: str) -> Path:
        """A build folder; `stamp` may be `latest`. Symlinks must stay inside builds/."""
        path = inside(builds, builds / family / variant / stamp)
        if not (path.is_dir() and _visible(path)):
            raise HTTPException(404, f"no build {family}/{variant}/{stamp}")
        return path

    def build_file(family: str, variant: str, stamp: str, name: str) -> Path:
        build = build_dir(family, variant, stamp)
        path = inside(build, build.joinpath(*check_relpath(name, "file name")))
        if not path.is_file():
            raise HTTPException(404, f"no file {name!r} in this build")
        return path

    def toolpath_file(family: str, variant: str, stamp: str, name: str, kind: str) -> Path:
        from luregen.viewer.binfmt import ToolpathFormatError, encode_file

        source = build_file(family, variant, stamp, name)
        if source.suffix.lower() not in GCODE_SUFFIXES:
            raise HTTPException(422, f"{name!r} is not a G-code file")
        try:
            return encode_file(source, kind)
        except (ToolpathFormatError, ValueError) as exc:
            raise HTTPException(422, f"can't encode {name}: {exc}") from exc

    @router.get("/builds")
    def list_builds(
        family: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
        variant: Annotated[str | None, Query(pattern=NAME_PATTERN)] = None,
    ) -> list[BuildSummary]:
        """Builds, newest first, with report.json summaries (dot-folders like .jobs skipped)."""
        out = []
        for fam in _subdirs(builds):
            if family and fam.name != family:
                continue
            for var in _subdirs(fam):
                if variant and var.name != variant:
                    continue
                latest = var / "latest"
                latest_name = latest.resolve().name if latest.is_symlink() else None
                for build in _subdirs(var):
                    out.append(_summary(root, build, build.name == latest_name))
        return sorted(out, key=lambda b: (b.stamp, b.family, b.variant), reverse=True)

    @router.get("/builds/{family}/{variant}/{stamp}/files/{name:path}")
    def get_build_file(
        family: Name, variant: Name, stamp: Name, name: str, download: bool = False
    ) -> FileResponse:
        """A file of a build (`name` may include subfolders, e.g. model/body.stl)."""
        path = build_file(family, variant, stamp, name)
        return FileResponse(
            path,
            media_type=media_type(path),
            filename=path.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @router.get("/builds/{family}/{variant}/{stamp}/toolpath/{name:path}/header")
    def get_toolpath_header(
        family: Name, variant: Name, stamp: Name, name: str, kind: ToolpathKind = "auto"
    ) -> dict[str, Any]:
        """Only the JSON header of the binary toolpath (bounds, layers, tools, markers, …)."""
        from luregen.viewer.binfmt import read_file_header

        return read_file_header(toolpath_file(family, variant, stamp, name, kind))

    @router.get(
        "/builds/{family}/{variant}/{stamp}/toolpath/{name:path}",
        response_class=FileResponse,
        responses=_BINARY,
    )
    def get_toolpath(
        family: Name,
        variant: Name,
        stamp: Name,
        name: str,
        kind: ToolpathKind = "auto",
        accept_encoding: Annotated[str, Header()] = "",
    ) -> FileResponse:
        """G-code → binary toolpath v1 (docs/viewer-binary-format.md), gzipped when accepted.

        Encoded files are cached next to the G-code as `<file>.<kind>.bin[.gz]`.
        """
        encoded = toolpath_file(family, variant, stamp, name, kind)
        headers = {"Vary": "Accept-Encoding"}
        if "gzip" in accept_encoding.lower():
            encoded = _gzipped(encoded)
            headers["Content-Encoding"] = "gzip"
        return FileResponse(encoded, media_type="application/octet-stream", headers=headers)

    return router


def _summary(root: Path, build: Path, latest: bool) -> BuildSummary:
    family, variant = build.parent.parent.name, build.parent.name
    report: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        report = json.loads((build / "report.json").read_text())
    git = report.get("git") if isinstance(report.get("git"), dict) else {}
    warnings = report.get("warnings")
    return BuildSummary(
        family=family,
        variant=variant,
        stamp=build.name,
        path=build.relative_to(root).as_posix(),
        latest=latest,
        created=report.get("created"),
        status=report.get("status"),
        git_sha=git.get("sha"),
        git_dirty=git.get("dirty"),
        warnings=[str(w) for w in warnings] if isinstance(warnings, list) else [],
        files=_files(build),
    )
