"""Export solids to STEP, STL and 3MF, and check the meshes are watertight.

One triangle mesh per shape feeds both STL and 3MF, so the two always match:

- A tipped loft (one closed B-spline face that collapses to a vertex at each end) gets a
  structured, curvature-adaptive UV mesh. OCCT's BRepMesh ignores the deflection on that
  face: it leaves long slivers along the flanks (about -0.7 % volume at 0.02 mm) and at some
  angular settings returns a broken mesh.
- Any other shape is meshed by OCCT with an *absolute* deflection. build123d's
  `tessellate`, `export_stl` and `Mesher` use a relative deflection and reuse a cached
  triangulation, so their "0.02" is neither millimetres nor repeatable.

STL is written by trimesh; 3MF by a small core-spec writer below (no lxml dependency).
"""

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

STL_TOLERANCE = 0.02  # mm chordal deviation
STL_ANGULAR_TOLERANCE = 0.1  # rad
MAX_TRIANGLES = 90_000  # ≈ 4.5 MB binary STL; tolerances are relaxed ×2 until it fits
MAX_RELAXATIONS = 4
MAX_EDGE_MM = 5.0  # structured mesh: longest step along a grid line
MAX_VOLUME_ERROR = 0.02  # mesh vs B-rep volume; more means a broken mesh
_3MF_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_3MF_NS = {"c": _3MF_CORE}


class ExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class MeshCheck:
    path: str
    triangles: int
    watertight: bool
    winding_consistent: bool
    manifold_ok: bool
    volume_mm3: float
    size_bytes: int
    volume_error: float | None = None  # relative to the B-rep volume, when known
    tolerance: float | None = None
    angular_tolerance: float | None = None
    mesher: str | None = None

    @property
    def ok(self) -> bool:
        closed = self.watertight and self.winding_consistent and self.manifold_ok
        return closed and (self.volume_error is None or abs(self.volume_error) <= MAX_VOLUME_ERROR)

    def as_dict(self) -> dict:
        d = {**asdict(self), "volume_mm3": round(self.volume_mm3, 3), "ok": self.ok}
        if self.volume_error is not None:
            d["volume_error"] = round(self.volume_error, 6)
        return d


@dataclass(frozen=True)
class Tessellation:
    mesh: object  # trimesh.Trimesh
    tolerance: float
    angular_tolerance: float
    mesher: str  # "loft_grid" or "occt"


# --- meshing --------------------------------------------------------------------------


def _clean(vertices: np.ndarray, faces: np.ndarray):
    import trimesh

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def _tipped_loft_surface(shape):
    """The BRepAdaptor_Surface of a one-face, U-closed loft with point tips, else None."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_SurfaceType

    faces = shape.faces()
    if len(faces) != 1:
        return None
    surf = BRepAdaptor_Surface(faces[0].wrapped)
    if surf.GetType() != GeomAbs_SurfaceType.GeomAbs_BSplineSurface or not surf.IsUClosed():
        return None
    us = np.linspace(surf.FirstUParameter(), surf.LastUParameter(), 9)
    for v in (surf.FirstVParameter(), surf.LastVParameter()):
        pts = _eval(surf, us, np.array([v]))[:, 0]
        if np.ptp(pts, axis=0).max() > 1e-6:
            return None
    return surf


def _eval(surf, us: np.ndarray, vs: np.ndarray) -> np.ndarray:
    """Surface points, shape (len(us), len(vs), 3)."""
    out = np.empty((len(us), len(vs), 3))
    for i, u in enumerate(us):
        for j, v in enumerate(vs):
            p = surf.Value(float(u), float(v))
            out[i, j] = (p.X(), p.Y(), p.Z())
    return out


def _measure(lines: np.ndarray, tol: float, ang: float, max_step: float) -> np.ndarray:
    """Per-interval sample density for polylines `lines` (k, M, 3), max over the k lines.

    The step h along a line obeys sag κh²/8 ≤ tol, turning κh ≤ ang and h ≤ max_step;
    the returned value is ds / h for each of the M-1 intervals.
    """
    seg = np.diff(lines, axis=1)
    ds = np.linalg.norm(seg, axis=2)
    unit = seg / np.maximum(ds, 1e-12)[..., None]
    cos = np.clip(np.sum(unit[:, 1:] * unit[:, :-1], axis=2), -1.0, 1.0)
    turn = np.arccos(cos)  # at interior nodes, (k, M-2)
    node_k = turn / np.maximum(0.5 * (ds[:, 1:] + ds[:, :-1]), 1e-12)
    pad = np.zeros((lines.shape[0], 1))
    kappa = np.maximum(np.hstack([pad, node_k]), np.hstack([node_k, pad]))  # per interval
    density = np.maximum.reduce(
        [np.sqrt(kappa / (8 * tol)), kappa / ang, np.full_like(kappa, 1.0 / max_step)]
    )
    return (density * ds).max(axis=0)


def _adaptive_params(dense: np.ndarray, measure: np.ndarray, min_n: int) -> np.ndarray:
    m = np.concatenate([[0.0], np.cumsum(measure)])
    n = max(min_n, int(np.ceil(m[-1])))
    return np.interp(np.linspace(0.0, m[-1], n + 1), m, dense)


def _loft_grid_mesh(surf, tol: float, ang: float):
    """Structured mesh of a tipped loft face: adaptive u (around) × v (along) grid."""
    u0, u1 = surf.FirstUParameter(), surf.LastUParameter()
    v0, v1 = surf.FirstVParameter(), surf.LastVParameter()
    du = np.linspace(u0, u1, 721)
    dv = np.linspace(v0, v1, 1201)
    probe_v = np.linspace(v0, v1, 11)[1:-1]
    probe_u = np.linspace(u0, u1, 9)[:-1]
    u_lines = _eval(surf, du, probe_v).transpose(1, 0, 2)  # (k, M, 3) around the body
    v_lines = _eval(surf, probe_u, dv)  # (k, M, 3) along the body
    us = _adaptive_params(du, _measure(u_lines, tol, ang, MAX_EDGE_MM), 16)[:-1]
    vs = _adaptive_params(dv, _measure(v_lines, tol, ang, MAX_EDGE_MM), 8)
    grid = _eval(surf, us, vs[1:-1])  # (nu, nv, 3), tips excluded
    nu, nv = grid.shape[:2]
    tip0, tip1 = _eval(surf, us[:1], vs[:1])[0, 0], _eval(surf, us[:1], vs[-1:])[0, 0]
    verts = np.vstack([tip0, grid.transpose(1, 0, 2).reshape(-1, 3), tip1])

    def idx(i: int, j: int) -> int:  # i around (wraps), j along
        return 1 + j * nu + (i % nu)

    faces = []
    last = len(verts) - 1
    for i in range(nu):
        faces.append((0, idx(i, 0), idx(i + 1, 0)))
        faces.append((last, idx(i + 1, nv - 1), idx(i, nv - 1)))
        for j in range(nv - 1):
            a, b, c, d = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            faces += [(a, d, b), (b, d, c)]
    return _clean(verts, np.array(faces, dtype=np.int64))


def _occt_mesh(shape, tol: float, ang: float):
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools

    BRepTools.Clean_s(shape.wrapped)  # drop any cached (coarser or relative) triangulation
    BRepMesh_IncrementalMesh(shape.wrapped, tol, False, ang, True)
    verts, tris = shape.tessellate(tol, ang)  # reads the triangulation made above
    BRepTools.Clean_s(shape.wrapped)
    return _clean(np.array([(v.X, v.Y, v.Z) for v in verts]), np.array(tris, dtype=np.int64))


def tessellate(
    shape,
    tolerance: float = STL_TOLERANCE,
    angular_tolerance: float = STL_ANGULAR_TOLERANCE,
    max_triangles: int | None = MAX_TRIANGLES,
) -> Tessellation:
    """Watertight mesh of `shape`; tolerances double while over `max_triangles`."""
    surf = _tipped_loft_surface(shape)
    mesher = "loft_grid" if surf is not None else "occt"
    for _ in range(MAX_RELAXATIONS + 1):
        if surf is not None:
            mesh = _loft_grid_mesh(surf, tolerance, angular_tolerance)
        else:
            mesh = _occt_mesh(shape, tolerance, angular_tolerance)
        if max_triangles is None or len(mesh.faces) <= max_triangles:
            break
        tolerance, angular_tolerance = 2 * tolerance, 2 * angular_tolerance
    return Tessellation(mesh, tolerance, angular_tolerance, mesher)


# --- checks ---------------------------------------------------------------------------


def check_mesh(mesh, path: Path, expected_volume: float | None = None) -> MeshCheck:
    """Watertightness by trimesh (closed, consistent winding) and manifold3d (2-manifold)."""
    import manifold3d

    try:
        man = manifold3d.Manifold(
            manifold3d.Mesh(
                vert_properties=np.asarray(mesh.vertices, dtype=np.float32),
                tri_verts=np.asarray(mesh.faces, dtype=np.uint32),
            )
        )
        manifold_ok = man.status() == manifold3d.Error.NoError and not man.is_empty()
    except Exception:
        manifold_ok = False
    volume = float(mesh.volume)
    return MeshCheck(
        path=path.name,
        triangles=len(mesh.faces),
        watertight=bool(mesh.is_watertight),
        winding_consistent=bool(mesh.is_winding_consistent),
        manifold_ok=manifold_ok,
        volume_mm3=volume,
        size_bytes=path.stat().st_size,
        volume_error=None if not expected_volume else volume / expected_volume - 1.0,
    )


def check_stl(path: Path, expected_volume: float | None = None) -> MeshCheck:
    import trimesh

    mesh = trimesh.load_mesh(path, file_type="stl")
    return check_mesh(_clean(mesh.vertices, mesh.faces), path, expected_volume)


def check_3mf(path: Path, expected_volume: float | None = None) -> MeshCheck:
    return check_mesh(_clean(*read_3mf_mesh(path)), path, expected_volume)


# --- writers --------------------------------------------------------------------------


def export_step(shape, path: Path) -> Path:
    from build123d import export_step as b3d_export_step

    path.parent.mkdir(parents=True, exist_ok=True)
    if not b3d_export_step(shape, path):
        raise ExportError(f"STEP export failed: {path}")
    return path


def write_3mf(mesh, path: Path, name: str = "body") -> Path:
    """Minimal 3MF (core spec, millimetres, one object)."""
    v = "\n".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in mesh.vertices)
    t = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{_3MF_CORE}">\n'
        f'<resources><object id="1" type="model" name="{name}"><mesh>\n'
        f"<vertices>\n{v}\n</vertices>\n<triangles>\n{t}\n</triangles>\n"
        '</mesh></object></resources>\n<build><item objectid="1"/></build>\n</model>\n'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>\n'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model)
    return path


def read_3mf_mesh(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Vertices and triangles of every object in a 3MF (stdlib XML; trimesh needs lxml)."""
    verts: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    offset = 0
    with zipfile.ZipFile(path) as zf:
        for name in (n for n in zf.namelist() if n.endswith(".model")):
            root = ET.fromstring(zf.read(name))
            for mesh in root.iterfind(".//c:mesh", _3MF_NS):
                v = [
                    [float(e.get(k)) for k in "xyz"] for e in mesh.iterfind(".//c:vertex", _3MF_NS)
                ]
                f = [
                    [int(e.get(k)) for k in ("v1", "v2", "v3")]
                    for e in mesh.iterfind(".//c:triangle", _3MF_NS)
                ]
                verts.append(np.array(v, dtype=float).reshape(-1, 3))
                faces.append(np.array(f, dtype=np.int64).reshape(-1, 3) + offset)
                offset += len(v)
    if not verts:
        raise ExportError(f"{path}: no mesh in 3MF")
    return np.vstack(verts), np.vstack(faces)


def export_meshes(
    shape,
    out_dir: Path,
    stem: str = "body",
    tolerance: float = STL_TOLERANCE,
    angular_tolerance: float = STL_ANGULAR_TOLERANCE,
    max_triangles: int | None = MAX_TRIANGLES,
) -> dict[str, MeshCheck]:
    """Tessellate once; write `<stem>.stl` and `<stem>.3mf` and check both files."""
    tess = tessellate(shape, tolerance, angular_tolerance, max_triangles)
    stl, mf = out_dir / f"{stem}.stl", out_dir / f"{stem}.3mf"
    stl.parent.mkdir(parents=True, exist_ok=True)
    tess.mesh.export(stl, file_type="stl")
    write_3mf(tess.mesh, mf, stem)
    extra = {
        "tolerance": tess.tolerance,
        "angular_tolerance": tess.angular_tolerance,
        "mesher": tess.mesher,
    }
    volume = float(shape.volume)
    return {
        "stl": MeshCheck(**{**asdict(check_stl(stl, volume)), **extra}),
        "3mf": MeshCheck(**{**asdict(check_3mf(mf, volume)), **extra}),
    }


def export_all(
    shape,
    out_dir: Path,
    stem: str = "body",
    tolerance: float = STL_TOLERANCE,
    angular_tolerance: float = STL_ANGULAR_TOLERANCE,
    require_watertight: bool = True,
) -> dict[str, dict]:
    """Write `<stem>.step|stl|3mf` into `out_dir`; return per-file info for the report."""
    step = export_step(shape, out_dir / f"{stem}.step")
    meshes = export_meshes(shape, out_dir, stem, tolerance, angular_tolerance)
    if require_watertight:
        bad = [m.path for m in meshes.values() if not m.ok]
        if bad:
            raise ExportError(
                f"mesh check failed (not watertight/manifold or volume off): {', '.join(bad)}"
            )
    return {
        "step": {"path": step.name, "size_bytes": step.stat().st_size},
        **{k: m.as_dict() for k, m in meshes.items()},
    }
