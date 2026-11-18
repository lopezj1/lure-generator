"""builds/ endpoints: list builds with report summaries, compare two builds, serve their
files (single downloads, GLB conversions, zip archives) and G-code as viewer toolpaths."""

import contextlib
import gzip
import json
import math
import mimetypes
import os
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from luregen.api.models import (
    BuildComparison,
    BuildFile,
    BuildSummary,
    ConfigChange,
    Delta,
    Download,
    ReportSummary,
    SectionSummary,
    Verdict,
)
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

# Zip archive groups: folder prefix inside the build ("" = every visible file).
ARCHIVE_GROUPS = {"all": "", "model": "model/", "print": "fab/print/", "cnc": "fab/cnc/"}
ArchiveGroup = Literal["all", "model", "print", "cnc"]
_ZIP = {200: {"content": {"application/zip": {}}, "description": "Zip archive"}}
_GLB = {200: {"content": {"model/gltf-binary": {}}, "description": "glTF binary"}}
# Download formats by suffix (other files are still listed under `files`).
_FORMATS = {
    ".step": "STEP",
    ".stp": "STEP",
    ".stl": "STL",
    ".3mf": "3MF",
    ".glb": "GLB",
    ".gcode": "G-code",
    ".gco": "G-code",
    ".nc": "NC",
    ".ngc": "NC",
    ".tap": "NC",
    ".cnc": "NC",
}
_MAX_CONFIG_CHANGES = 200


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

    def parse_ref(ref: str, what: str) -> Path:
        parts = check_relpath(ref, what)
        if len(parts) != 3:
            raise HTTPException(400, f"{what}: expected family/variant/stamp, got {ref!r}")
        return build_dir(*parts)

    @router.get("/builds/compare")
    def compare_builds(
        a: Annotated[str, Query(description="family/variant/stamp (stamp may be latest)")],
        b: Annotated[str, Query(description="family/variant/stamp; deltas are b − a")],
    ) -> BuildComparison:
        """Two builds side by side: mass, density, CG/CB deltas and changed config keys."""
        da, db = parse_ref(a, "a"), parse_ref(b, "b")
        sa, sb = _summary_of(root, da), _summary_of(root, db)
        changes = _config_changes(da / "resolved.toml", db / "resolved.toml")
        return BuildComparison(
            a=sa,
            b=sb,
            deltas=_deltas(sa.report or ReportSummary(), sb.report or ReportSummary()),
            config_changes=changes[:_MAX_CONFIG_CHANGES],
            config_changes_truncated=len(changes) > _MAX_CONFIG_CHANGES,
        )

    @router.get(
        "/builds/{family}/{variant}/{stamp}/archive",
        response_class=FileResponse,
        responses=_ZIP,
    )
    def get_archive(
        family: Name, variant: Name, stamp: Name, group: ArchiveGroup = "all"
    ) -> FileResponse:
        """A zip of the build's files (`group`: all, model/, fab/print/ or fab/cnc/)."""
        build = build_dir(family, variant, stamp)
        prefix = ARCHIVE_GROUPS[group]
        files = [f for f in _files(build) if f.name.startswith(prefix)]
        if not files:
            raise HTTPException(404, f"no {group} files in this build")
        fd, tmp = tempfile.mkstemp(prefix=".luregen-", suffix=".zip")
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf,
            ):
                top = f"{variant}-{build.name}"
                for f in files:
                    zf.write(build / f.name, f"{top}/{f.name}")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        suffix = "" if group == "all" else f"-{group}"
        return FileResponse(
            tmp,
            media_type="application/zip",
            filename=f"{variant}-{build.name}{suffix}.zip",
            background=BackgroundTask(Path(tmp).unlink, missing_ok=True),
        )

    @router.get(
        "/builds/{family}/{variant}/{stamp}/glb/{part}",
        response_class=FileResponse,
        responses=_GLB,
    )
    def get_glb(family: Name, variant: Name, stamp: Name, part: Name) -> FileResponse:
        """`model/<part>.stl` as GLB (glTF: metres, y up), cached as `model/.<part>.glb`."""
        build = build_dir(family, variant, stamp)
        stl = inside(build, build / "model" / f"{part}.stl")
        if not stl.is_file():
            raise HTTPException(404, f"no model/{part}.stl in this build")
        return FileResponse(
            _glb_of(stl), media_type="model/gltf-binary", filename=f"{variant}-{part}.glb"
        )

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


def _summary_of(root: Path, build: Path) -> BuildSummary:
    latest = build.parent / "latest"
    latest_name = latest.resolve().name if latest.is_symlink() else None
    return _summary(root, build, build.name == latest_name)


def _summary(
    root: Path, build: Path, latest: bool, _latest_name: str | None = None
) -> BuildSummary:
    family, variant = build.parent.parent.name, build.parent.name
    report: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        report = json.loads((build / "report.json").read_text())
    if not isinstance(report, dict):
        report = {}
    git = report.get("git") if isinstance(report.get("git"), dict) else {}
    warnings = report.get("warnings")
    files = _files(build)
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
        report=_report_summary(report) if report else None,
        files=files,
        downloads=_downloads(family, variant, build.name, files),
    )


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _vec(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) == 3:
        nums = [_num(v) for v in value]
        if all(n is not None for n in nums):
            return nums  # type: ignore[return-value]
    return None


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _report_summary(report: dict[str, Any]) -> ReportSummary:
    """Key numbers from report.json: F3's `summary`/`lure`/`sections`, else the envelope."""
    summary, lure = _obj(report.get("summary")), _obj(report.get("lure"))
    env = _obj(report.get("envelope"))
    mass = _num(summary.get("mass_g", lure.get("mass_g")))
    oz = _num(summary.get("mass_oz", lure.get("mass_oz")))
    if oz is None and mass is not None:
        oz = round(mass / 28.349523125, 3)
    cg = _vec(summary.get("cg_mm", lure.get("cg_mm")))
    cb = _vec(summary.get("cb_mm", lure.get("cb_mm")))
    diff = [round(g - b, 3) for g, b in zip(cg, cb, strict=True)] if cg and cb else None
    verdict = _obj(report.get("verdict")) or _obj(summary.get("verdict"))
    sections = {
        str(name): SectionSummary(
            mass_g=_num(sec.get("mass_g")),
            density_g_cm3=_num(sec.get("density_g_cm3", sec.get("density"))),
        )
        for name, sec in _obj(report.get("sections")).items()
        if isinstance(sec, dict)
    }
    error = report.get("error")
    return ReportSummary(
        mass_g=mass,
        mass_oz=oz,
        density_g_cm3=_num(
            summary.get("density_g_cm3", lure.get("density_g_cm3", lure.get("density")))
        ),
        cg_mm=cg,
        cb_mm=cb,
        cg_minus_cb_mm=diff,
        cb_above_cg_mm=round(cb[2] - cg[2], 3) if cg and cb else None,
        cb_above_cg_target_mm=_num(lure.get("cb_above_cg_target_mm")),
        envelope_volume_cm3=_num(env.get("volume_cm3")),
        verdict=Verdict(
            status=str(verdict["status"]),
            label=str(verdict.get("label", "")),
            failed=[str(f) for f in verdict.get("failed", []) if isinstance(f, str)],
        )
        if "status" in verdict
        else None,
        sections=sections,
        error=str(error) if error else None,
    )


def _downloads(family: str, variant: str, stamp: str, files: list[BuildFile]) -> list[Download]:
    base = "/api/builds/" + "/".join(quote(p, safe="") for p in (family, variant, stamp))
    out: list[Download] = []
    names = {f.name for f in files}
    for f in files:
        fmt = _FORMATS.get(Path(f.name).suffix.lower())
        if fmt:
            href = f"{base}/files/{quote(f.name)}?download=true"
            out.append(Download(label=f.name, format=fmt, href=href, size=f.size))
    for f in files:
        rel = Path(f.name)
        if rel.parent.as_posix() == "model" and rel.suffix.lower() == ".stl":
            if f"model/{rel.stem}.glb" in names:
                continue
            href = f"{base}/glb/{quote(rel.stem, safe='')}"
            out.append(Download(label=f"model/{rel.stem}.glb", format="GLB", href=href))
    for group, prefix in ARCHIVE_GROUPS.items():
        members = [f for f in files if f.name.startswith(prefix)]
        if not members:
            continue
        label = "all files" if group == "all" else f"{group} files"
        href = f"{base}/archive?group={group}"
        out.append(Download(label=label, format="ZIP", href=href))
    return out


def _glb_of(stl: Path) -> Path:
    """`<dir>/.<stem>.glb` converted from `stl` (mm, z up → glTF metres, y up); cached."""
    target = stl.with_name(f".{stl.stem}.glb")
    with contextlib.suppress(OSError):
        if target.stat().st_mtime >= stl.stat().st_mtime:
            return target
    import numpy as np
    import trimesh

    mesh = trimesh.load_mesh(stl, file_type="stl")
    z_up_to_y_up = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float)
    mesh.apply_transform(z_up_to_y_up)
    mesh.apply_scale(0.001)
    data = mesh.export(file_type="glb")
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


# --- compare -------------------------------------------------------------------------------


def _deltas(a: ReportSummary, b: ReportSummary) -> list[Delta]:
    rows: list[tuple[str, str, str, float | None, float | None]] = [
        ("mass_g", "Mass", "g", a.mass_g, b.mass_g),
        ("mass_oz", "Mass", "oz", a.mass_oz, b.mass_oz),
        ("density_g_cm3", "Density", "g/cm³", a.density_g_cm3, b.density_g_cm3),
        (
            "envelope_volume_cm3",
            "Envelope volume",
            "cm³",
            a.envelope_volume_cm3,
            b.envelope_volume_cm3,
        ),
    ]
    for axis, j in (("x", 0), ("z", 2)):
        rows.append((f"cg_{axis}", f"CG {axis}", "mm", _at(a.cg_mm, j), _at(b.cg_mm, j)))
        rows.append((f"cb_{axis}", f"CB {axis}", "mm", _at(a.cb_mm, j), _at(b.cb_mm, j)))
    rows.append(
        ("cg_minus_cb_x", "CG − CB x", "mm", _at(a.cg_minus_cb_mm, 0), _at(b.cg_minus_cb_mm, 0))
    )
    rows.append(("cb_above_cg", "CB above CG", "mm", a.cb_above_cg_mm, b.cb_above_cg_mm))
    for name in sorted({*a.sections, *b.sections}):
        sa, sb = a.sections.get(name, SectionSummary()), b.sections.get(name, SectionSummary())
        rows.append((f"{name}.mass_g", f"{name.title()} mass", "g", sa.mass_g, sb.mass_g))
        rows.append(
            (
                f"{name}.density_g_cm3",
                f"{name.title()} density",
                "g/cm³",
                sa.density_g_cm3,
                sb.density_g_cm3,
            )
        )
    return [
        Delta(
            key=key,
            label=label,
            unit=unit,
            a=va,
            b=vb,
            delta=round(vb - va, 4) if va is not None and vb is not None else None,
        )
        for key, label, unit, va, vb in rows
        if va is not None or vb is not None
    ]


def _at(vec: list[float] | None, i: int) -> float | None:
    return vec[i] if vec else None


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(node, list) and any(isinstance(v, dict) for v in node):
        out = {}
        for i, v in enumerate(node):
            out.update(_flatten(v, f"{prefix}.{i}"))
        return out
    return {prefix: node}


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _config_changes(a: Path, b: Path) -> list[ConfigChange]:
    fa, fb = _flatten(_load_toml(a)), _flatten(_load_toml(b))
    keys = sorted(k for k in {*fa, *fb} if k != "name" and fa.get(k) != fb.get(k))
    return [ConfigChange(key=k, a=_jsonable(fa.get(k)), b=_jsonable(fb.get(k))) for k in keys]


def _jsonable(value: Any) -> Any:
    """TOML dates/times → ISO strings; everything else is already JSON-friendly."""
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
