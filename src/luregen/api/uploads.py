"""POST /uploads: content-checked, size-capped files stored as uploads/<sha256>.<ext>."""

import hashlib
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from luregen.api.models import Upload
from luregen.api.safety import check_relpath, inside

MAX_UPLOAD = 25 * 1024 * 1024
_CHUNK = 1024 * 1024
_MULTIPART_SLACK = 64 * 1024
GCODE_SUFFIXES = {".gcode", ".gco", ".g", ".nc", ".ngc", ".tap", ".cnc"}

Kind = Literal["photo", "csv", "gcode"]


def _image_ext(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif"
    if head.startswith(b"BM"):
        return "bmp"
    return None


def check_photo(data: bytes) -> str:
    import cv2
    import numpy as np

    ext = _image_ext(data[:16])
    if ext is None:
        raise ValueError("not a PNG, JPEG, WebP, TIFF or BMP image")
    if cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) is None:
        raise ValueError(f"the {ext} image doesn't decode")
    return ext


def check_csv(data: bytes) -> str:
    from luregen.profile.table import StationTable

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.csv"
        path.write_bytes(data)
        try:
            StationTable.load_csv(path)
        except (ValueError, UnicodeDecodeError, IndexError) as exc:
            raise ValueError(f"not a station-table CSV: {exc}") from exc
    return "csv"


def check_gcode(data: bytes, filename: str | None) -> str:
    from luregen.fab.gcode import GcodeSyntaxError, tokenize_line

    if b"\0" in data:
        raise ValueError("binary data is not G-code")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("G-code must be UTF-8 text") from exc
    code_lines = good = moves = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith(";"):
            continue
        code_lines += 1
        try:
            line = tokenize_line(raw, number)
        except GcodeSyntaxError:
            continue  # e.g. Klipper macros; tolerated like the toolpath encoder does
        good += 1
        moves += any(w.letter in "GM" for w in line.words)
    if not moves or good < code_lines / 2:
        raise ValueError("no G-code found (most lines don't tokenize)")
    suffix = Path(filename or "").suffix.lower()
    return suffix[1:] if suffix in GCODE_SUFFIXES else "gcode"


def uploads_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["uploads"])
    folder = root / "uploads"

    @router.post(
        "/uploads",
        responses={
            413: {"description": "Larger than 25 MB"},
            422: {"description": "Content check failed"},
        },
    )
    async def upload(
        request: Request, kind: Annotated[Kind, Query()], file: Annotated[UploadFile, File()]
    ) -> Upload:
        """Store a photo, station CSV or G-code file at uploads/<sha256>.<ext>."""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_UPLOAD + _MULTIPART_SLACK:
            raise HTTPException(413, "upload larger than 25 MB")
        digest, chunks, size = hashlib.sha256(), [], 0
        while chunk := await file.read(_CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413, "upload larger than 25 MB")
            digest.update(chunk)
            chunks.append(chunk)
        data = b"".join(chunks)
        try:
            if kind == "photo":
                ext = await run_in_threadpool(check_photo, data)
            elif kind == "csv":
                ext = await run_in_threadpool(check_csv, data)
            else:
                ext = await run_in_threadpool(check_gcode, data, file.filename)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        sha = digest.hexdigest()
        target = folder / f"{sha}.{ext}"
        if not target.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".upload.", dir=folder)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)
        return Upload(id=sha, kind=kind, size=size, path=f"uploads/{target.name}")

    @router.get("/uploads/{name:path}", response_class=FileResponse)
    def get_upload(name: str) -> FileResponse:
        """A file under uploads/ (e.g. a profile job's `profile_debug.png` overlay)."""
        path = inside(folder, folder.joinpath(*check_relpath(name, "file name")))
        if not path.is_file():
            raise HTTPException(404, f"no upload {name!r}")
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media.startswith("text/"):
            media += "; charset=utf-8"
        return FileResponse(path, media_type=media)

    return router
