"""Uploaded G-code as viewer toolpaths (W6, #67): the same binary v1 as build toolpaths.

Builds hold no G-code until F6b, so the print and CNC previews also accept an upload
(`POST /api/uploads?kind=gcode`, stored as `uploads/<sha256>.<ext>`) and read it here.
"""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from luregen.api.builds import _BINARY, ToolpathKind, _gzipped
from luregen.api.safety import inside
from luregen.api.uploads import GCODE_SUFFIXES

UploadId = Annotated[str, PathParam(pattern=r"^[0-9a-f]{64}$")]


def upload_toolpaths_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["uploads"])
    folder = root / "uploads"

    def gcode_upload(upload_id: str) -> Path:
        for suffix in sorted(GCODE_SUFFIXES):
            path = inside(folder, folder / f"{upload_id}{suffix}")
            if path.is_file():
                return path
        raise HTTPException(404, f"no G-code upload {upload_id}")

    def toolpath_file(upload_id: str, kind: str) -> Path:
        from luregen.viewer.binfmt import ToolpathFormatError, encode_file

        source = gcode_upload(upload_id)
        try:
            return encode_file(source, kind)
        except (ToolpathFormatError, ValueError) as exc:
            raise HTTPException(422, f"can't encode upload {upload_id}: {exc}") from exc

    @router.get("/uploads/{upload_id}/toolpath/header")
    def get_upload_toolpath_header(
        upload_id: UploadId, kind: ToolpathKind = "auto"
    ) -> dict[str, Any]:
        """Only the JSON header of an uploaded G-code's binary toolpath."""
        from luregen.viewer.binfmt import read_file_header

        return read_file_header(toolpath_file(upload_id, kind))

    @router.get("/uploads/{upload_id}/toolpath", response_class=FileResponse, responses=_BINARY)
    def get_upload_toolpath(
        upload_id: UploadId,
        kind: ToolpathKind = "auto",
        accept_encoding: Annotated[str, Header()] = "",
    ) -> FileResponse:
        """Uploaded G-code → binary toolpath v1 (docs/viewer-binary-format.md), gzipped when
        accepted. Cached next to the upload as `<sha256>.<ext>.<kind>.bin[.gz]`."""
        encoded = toolpath_file(upload_id, kind)
        headers = {"Vary": "Accept-Encoding"}
        if "gzip" in accept_encoding.lower():
            encoded = _gzipped(encoded)
            headers["Content-Encoding"] = "gzip"
        return FileResponse(encoded, media_type="application/octet-stream", headers=headers)

    return router
