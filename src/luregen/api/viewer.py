"""Model viewer endpoints (#66): model parts, lazy three-cad-viewer tessellation, sections.

A build's model is every `model/<part>.step` (today `body`; with joints `head` and `tail`),
with `model/<part>.stl` as its export mesh. Nothing here is written by `lure build`:

- tessellation runs ocp-tessellate on the STEP files on first request (guarded subprocess)
  and caches ocp-tessellate's instanced buffer format, gzipped, under `model/.viewer/`;
- a section cuts the STL with trimesh (milliseconds), or with `exact=true` the STEP B-rep
  with build123d `section()` in a guarded child, flagged when it differs from the mesh.
"""

import contextlib
import gzip
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from luregen.api.safety import NAME_PATTERN, Name, check_name, inside

Axis = Literal["x", "y", "z"]
AXES = {"x": 0, "y": 1, "z": 2}
PLANE_AXES: dict[str, tuple[str, str]] = {"x": ("y", "z"), "y": ("x", "z"), "z": ("x", "y")}
PART_ORDER = ("head", "body", "tail")  # tree order; other parts follow alphabetically
PART_COLORS = {"head": "#e8b024", "body": "#e8b024", "tail": "#4a90d9"}
OTHER_COLORS = ("#5cb85c", "#d9534f", "#9b59b6", "#f0ad4e")
CACHE_DIR = ".viewer"
CACHE_VERSION = 1
CACHE_KEEP = 8  # cached tessellations kept per build
EXACT_DIFF_PCT = 1.0  # flag exact vs mesh area differences above this
TESSELLATE_TIMEOUT = 300.0
EXACT_TIMEOUT = 120.0

_TESS_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "content": {"application/json": {"schema": {"type": "object"}}},
        "description": "ocp-tessellate instanced format `{instances, shapes}` (three-cad-viewer"
        " protocol v3 with base64 buffers; decode with `decodeInstancedFormat`)",
    }
}


# --- response models ---------------------------------------------------------------------


class ModelPart(BaseModel):
    name: str = Field(description="Part name = file stem (body, head, tail, …)")
    step: str = Field(description="STEP file, relative to the build folder")
    stl: str | None = Field(description="Export mesh, relative to the build folder")
    size: int = Field(description="STEP size in bytes")
    bbox_min: list[float] | None = Field(None, description="From the STL, mm")
    bbox_max: list[float] | None = None


class ModelInfo(BaseModel):
    family: str
    variant: str
    stamp: str
    parts: list[ModelPart]
    bbox_min: list[float] | None = Field(None, description="All parts (from the STLs), mm")
    bbox_max: list[float] | None = None


class SectionLoop(BaseModel):
    part: str
    kind: Literal["outer", "void"] = Field(description="Material outline or internal void")
    points: list[tuple[float, float]] = Field(description="Closed polyline in (u, v), mm")


class Section(BaseModel):
    axis: Axis
    offset: float = Field(description="Plane position along `axis`, mm (world)")
    part: str
    plane_axes: tuple[str, str] = Field(description="World axes of the loop (u, v) coords")
    method: Literal["mesh", "exact"]
    loops: list[SectionLoop]
    area_mm2: float = Field(description="Material area (outlines minus voids)")
    void_area_mm2: float
    perimeter_mm: float = Field(description="All loops, outer and void")
    mesh_area_mm2: float | None = Field(None, description="exact only: the mesh result")
    delta_pct: float | None = Field(None, description="exact only: exact vs mesh area, %")
    differs: bool = Field(False, description=f"exact differs from mesh by > {EXACT_DIFF_PCT}%")
    elapsed_ms: float


# --- geometry helpers (also run in guarded children: keep them module-level) --------------


def _ring_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _ring_length(p: np.ndarray) -> float:
    return float(np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum())


def classify_loops(loops: list[np.ndarray]) -> list[tuple[str, np.ndarray]]:
    """Kind of each closed loop by nesting depth: even = material outline, odd = void."""
    from shapely.geometry import Point, Polygon

    polys = [Polygon(p) if len(p) >= 3 else None for p in loops]
    out = []
    for i, p in enumerate(loops):
        probe = Point(p[0])
        depth = sum(
            1 for j, q in enumerate(polys) if j != i and q is not None and q.contains(probe)
        )
        out.append(("void" if depth % 2 else "outer", p))
    return out


def summarise(part: str, classified: list[tuple[str, np.ndarray]]) -> dict[str, Any]:
    area = void = perim = 0.0
    loops = []
    for kind, p in classified:
        a = abs(_ring_area(p))
        if kind == "void":
            void += a
            area -= a
        else:
            area += a
        perim += _ring_length(p)
        loops.append({"part": part, "kind": kind, "points": np.round(p, 3).tolist()})
    return {"loops": loops, "area_mm2": area, "void_area_mm2": void, "perimeter_mm": perim}


def mesh_section(mesh: Any, part: str, axis: str, offset: float) -> dict[str, Any]:
    """trimesh plane cut at `axis = offset`; loops in the plane's (u, v) world coords."""
    import trimesh
    from trimesh.intersections import mesh_plane

    normal = np.zeros(3)
    normal[AXES[axis]] = 1.0
    segments = mesh_plane(mesh, normal, normal * offset)
    if len(segments) == 0:
        return summarise(part, [])
    path = trimesh.load_path(segments)
    u, v = (AXES[a] for a in PLANE_AXES[axis])
    loops = []
    for d in path.discrete:
        p = np.asarray(d)[:, [u, v]]
        if len(p) > 1 and np.allclose(p[0], p[-1]):
            p = p[:-1]
        if len(p) >= 3:
            loops.append(p)
    return summarise(part, classify_loops(loops))


def exact_section(parts: list[tuple[str, str]], axis: str, offset: float) -> list[dict]:
    """Guarded child: build123d `section()` of each STEP part (loaded from disk, since
    boolean results don't pickle). Areas come from the B-rep faces, loops are sampled."""
    from build123d import Face, Plane, Vector, import_step, section

    u, v = (AXES[a] for a in PLANE_AXES[axis])
    normal = Vector(*(1.0 if i == AXES[axis] else 0.0 for i in range(3)))
    out = []
    for name, path in parts:
        shape = import_step(path)
        bb = shape.bounding_box()
        lo, hi = (getattr(bb.min, axis.upper()), getattr(bb.max, axis.upper()))
        if not lo < offset < hi:
            out.append(summarise(name, []))
            continue
        plane = Plane(origin=normal * offset, z_dir=normal)
        sketch = section(shape, plane)
        faces = sketch.faces() if sketch is not None else []
        area = void = perim = 0.0
        loops = []
        for face in faces:
            area += face.area
            outer = face.outer_wire()
            for wire in face.wires():
                is_outer = wire.is_same(outer)
                perim += wire.length
                if not is_outer:
                    void += Face(wire).area
                n = max(24, int(wire.length / 0.5))
                pts = [wire.position_at(t) for t in np.linspace(0, 1, n + 1)[:-1]]
                p = np.array([(q.X, q.Y, q.Z) for q in pts])[:, [u, v]]
                kind = "outer" if is_outer else "void"
                loops.append({"part": name, "kind": kind, "points": np.round(p, 3).tolist()})
        out.append({"loops": loops, "area_mm2": area, "void_area_mm2": void, "perimeter_mm": perim})
    return out


def tessellate_parts(
    parts: list[tuple[str, str, str]], root_name: str, deviation: float, angular: float, out: str
) -> dict[str, Any]:
    """Guarded child: STEP parts → ocp-tessellate instanced format, gzipped JSON at `out`."""
    from build123d import import_step
    from ocp_tessellate.convert import tessellate_group, to_ocpgroup
    from ocp_tessellate.utils import numpy_to_buffer_json

    t0 = time.perf_counter()
    shapes = [import_step(path) for _, path, _ in parts]
    group, instances = to_ocpgroup(
        *shapes, names=[n for n, _, _ in parts], colors=[c for _, _, c in parts]
    )
    kwargs = {"deviation": deviation, "angular_tolerance": angular, "render_edges": True}
    instances, tree, _ = tessellate_group(group, instances, kwargs)
    _rename_root(tree, root_name)
    payload = json.dumps(
        numpy_to_buffer_json({"instances": instances, "shapes": tree}),
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    _write_atomic(Path(out), gzip.compress(payload, compresslevel=6, mtime=0))
    triangles = sum(int(np.asarray(i["triangles"]).size) // 3 for i in instances)
    return {"seconds": time.perf_counter() - t0, "triangles": triangles, "bytes": len(payload)}


def _rename_root(tree: dict, name: str) -> None:
    """ocp-tessellate names the root `Group`; show the variant in the tree instead."""
    old = tree.get("id", "/Group")
    new = f"/{name}"

    def walk(node: dict) -> None:
        if isinstance(node.get("id"), str) and node["id"].startswith(old):
            node["id"] = new + node["id"][len(old) :]
        for child in node.get("parts") or []:
            walk(child)

    tree["name"] = name
    walk(tree)


def _json_default(o: Any) -> Any:
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o).__name__)


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


# --- STL cache ------------------------------------------------------------------------------

_MESHES: "OrderedDict[tuple[str, int, int], Any]" = OrderedDict()
_MESHES_MAX = 16
_MESHES_LOCK = threading.Lock()


def load_mesh(path: Path) -> Any:
    """trimesh of an STL, cached by (path, mtime, size)."""
    import trimesh

    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _MESHES_LOCK:
        if key in _MESHES:
            _MESHES.move_to_end(key)
            return _MESHES[key]
    mesh = trimesh.load_mesh(path, file_type="stl")
    with _MESHES_LOCK:
        _MESHES[key] = mesh
        while len(_MESHES) > _MESHES_MAX:
            _MESHES.popitem(last=False)
    return mesh


_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        return _KEY_LOCKS.setdefault(key, threading.Lock())


# --- router ---------------------------------------------------------------------------------


def _sort_key(name: str) -> tuple[int, str]:
    return (PART_ORDER.index(name) if name in PART_ORDER else len(PART_ORDER), name)


def model_parts(build: Path) -> list[tuple[str, Path]]:
    """`(name, step path)` of every `model/<name>.step|stp`, in tree order."""
    model = build / "model"
    if not model.is_dir():
        return []
    found: dict[str, Path] = {}
    for path in model.iterdir():
        if path.suffix.lower() in (".step", ".stp") and not path.name.startswith("."):
            with contextlib.suppress(HTTPException):
                found.setdefault(check_name(path.stem, "part"), inside(build, path))
    return [(n, found[n]) for n in sorted(found, key=_sort_key)]


def _color(name: str, i: int) -> str:
    return PART_COLORS.get(name, OTHER_COLORS[i % len(OTHER_COLORS)])


def viewer_router(root: Path) -> APIRouter:
    router = APIRouter(tags=["viewer"])
    builds = root / "builds"
    base = "/builds/{family}/{variant}/{stamp}"

    def build_dir(family: str, variant: str, stamp: str) -> Path:
        path = inside(builds, builds / family / variant / stamp)
        if not path.is_dir() or path.name.startswith("."):
            raise HTTPException(404, f"no build {family}/{variant}/{stamp}")
        return path

    def parts_of(build: Path, part: str = "all") -> list[tuple[str, Path]]:
        parts = model_parts(build)
        if not parts:
            raise HTTPException(404, "this build has no model/*.step")
        if part == "all":
            return parts
        chosen = [p for p in parts if p[0] == part]
        if not chosen:
            raise HTTPException(404, f"no model part {part!r}")
        return chosen

    def stl_of(step: Path) -> Path | None:
        stl = step.with_suffix(".stl")
        return stl if stl.is_file() else None

    @router.get(f"{base}/model")
    def get_model(family: Name, variant: Name, stamp: Name) -> ModelInfo:
        """Model parts of a build (`model/*.step`), with STL bounds."""
        build = build_dir(family, variant, stamp)
        parts = []
        for name, step in parts_of(build):
            stl = stl_of(step)
            lo = hi = None
            if stl is not None:
                with contextlib.suppress(Exception):
                    bounds = np.asarray(load_mesh(stl).bounds, dtype=float)
                    lo, hi = (
                        [round(float(x), 4) for x in bounds[0]],
                        [round(float(x), 4) for x in bounds[1]],
                    )
            parts.append(
                ModelPart(
                    name=name,
                    step=step.relative_to(build).as_posix(),
                    stl=stl.relative_to(build).as_posix() if stl else None,
                    size=step.stat().st_size,
                    bbox_min=lo,
                    bbox_max=hi,
                )
            )
        mins = [p.bbox_min for p in parts if p.bbox_min]
        maxs = [p.bbox_max for p in parts if p.bbox_max]
        return ModelInfo(
            family=family,
            variant=variant,
            stamp=build.name,
            parts=parts,
            bbox_min=np.min(mins, axis=0).tolist() if mins else None,
            bbox_max=np.max(maxs, axis=0).tolist() if maxs else None,
        )

    @router.get(
        f"{base}/model/tessellation", response_class=FileResponse, responses=_TESS_RESPONSES
    )
    def get_tessellation(
        family: Name,
        variant: Name,
        stamp: Name,
        deviation: Annotated[float, Query(gt=0.001, le=5.0)] = 0.1,
        angular: Annotated[float, Query(gt=0.01, le=1.0)] = 0.2,
        accept_encoding: Annotated[str, Header()] = "",
    ) -> Response:
        """three-cad-viewer shapes for all parts, tessellated on first request and cached
        in `model/.viewer/` (keyed by tolerances and the STEP files' mtimes)."""
        from luregen.geometry.validate import GeometryError, GuardedCrash, run_guarded

        build = build_dir(family, variant, stamp)
        parts = parts_of(build)
        key_src = {
            "v": CACHE_VERSION,
            "deviation": deviation,
            "angular": angular,
            "parts": [(n, p.stat().st_mtime_ns, p.stat().st_size) for n, p in parts],
        }
        key = hashlib.sha256(json.dumps(key_src).encode()).hexdigest()[:16]
        cache = build / "model" / CACHE_DIR / f"{key}.json.gz"
        status = "hit"
        with _key_lock(str(cache)):
            if not cache.is_file():
                status = "miss"
                jobs = [(n, str(p), _color(n, i)) for i, (n, p) in enumerate(parts)]
                try:
                    run_guarded(
                        tessellate_parts,
                        jobs,
                        variant,
                        deviation,
                        angular,
                        str(cache),
                        timeout=TESSELLATE_TIMEOUT,
                    )
                except (GeometryError, GuardedCrash) as exc:
                    raise HTTPException(500, f"tessellation failed: {exc}") from exc
                _prune(cache.parent)
        headers = {"Vary": "Accept-Encoding", "X-Luregen-Cache": status}
        if "gzip" in accept_encoding.lower():
            headers["Content-Encoding"] = "gzip"
            return FileResponse(cache, media_type="application/json", headers=headers)
        return Response(
            gzip.decompress(cache.read_bytes()), media_type="application/json", headers=headers
        )

    @router.get(f"{base}/section")
    def get_section(
        family: Name,
        variant: Name,
        stamp: Name,
        axis: Axis,
        offset: Annotated[float, Query(ge=-10_000, le=10_000)],
        part: Annotated[str, Query(pattern=NAME_PATTERN, max_length=128)] = "all",
        exact: bool = False,
    ) -> Section:
        """Cross-section at `axis = offset` (world mm): loops, material and void areas.

        Default: trimesh on `model/<part>.stl` (ms). `exact=true`: build123d `section()` of
        the STEP in a guarded child (seconds), compared with the mesh result.
        """
        from luregen.geometry.validate import GeometryError, GuardedCrash, run_guarded

        t0 = time.perf_counter()
        build = build_dir(family, variant, stamp)
        parts = parts_of(build, part)
        mesh_results = []
        for name, step in parts:
            stl = stl_of(step)
            if stl is None:
                if not exact:
                    raise HTTPException(404, f"part {name!r} has no STL; use exact=true")
                continue
            try:
                mesh_results.append(mesh_section(load_mesh(stl), name, axis, offset))
            except ValueError as exc:
                raise HTTPException(422, f"can't section {stl.name}: {exc}") from exc
        mesh = _combine(mesh_results)
        common = {"axis": axis, "offset": offset, "part": part, "plane_axes": PLANE_AXES[axis]}
        if not exact:
            ms = 1000 * (time.perf_counter() - t0)
            return Section(**common, method="mesh", elapsed_ms=round(ms, 2), **mesh)
        jobs = [(n, str(p)) for n, p in parts]
        try:
            result = _combine(run_guarded(exact_section, jobs, axis, offset, timeout=EXACT_TIMEOUT))
        except (GeometryError, GuardedCrash) as exc:
            raise HTTPException(500, f"exact section failed: {exc}") from exc
        delta = None
        mesh_area = mesh["area_mm2"] if mesh_results else None
        if mesh_area:
            delta = 100.0 * (result["area_mm2"] / mesh_area - 1.0)
        elif mesh_area == 0.0 and result["area_mm2"] > 0:
            delta = 100.0
        ms = 1000 * (time.perf_counter() - t0)
        return Section(
            **common,
            method="exact",
            elapsed_ms=round(ms, 2),
            mesh_area_mm2=mesh_area,
            delta_pct=None if delta is None else round(delta, 4),
            differs=delta is not None and abs(delta) > EXACT_DIFF_PCT,
            **result,
        )

    return router


def _combine(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "loops": [lp for r in results for lp in r["loops"]],
        "area_mm2": sum(r["area_mm2"] for r in results),
        "void_area_mm2": sum(r["void_area_mm2"] for r in results),
        "perimeter_mm": sum(r["perimeter_mm"] for r in results),
    }


def _prune(folder: Path) -> None:
    """Keep the CACHE_KEEP newest cached tessellations of a build."""
    files = sorted(folder.glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[CACHE_KEEP:]:
        old.unlink(missing_ok=True)
