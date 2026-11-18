"""Spike #58: server-side section(shape, axis, offset) -> {loops, area_mm2, void_area_mm2, perimeter_mm}.

Two backends:
- `section_mesh`: trimesh plane cut of the export mesh (fast).
- `section_exact`: build123d/OCCT intersection with a planar face (exact), meant to run under
  `luregen.geometry.validate.run_guarded`.

`area_mm2` is the material area (outer minus holes); `void_area_mm2` the holes (internal
voids cut by the plane); loops are closed 2D polylines in the plane's (u, v) coordinates.

    uv run python spike/py/section.py
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

AXES = {"x": 0, "y": 1, "z": 2}
UV = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}  # in-plane coords kept for each cut axis


def _ring_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _summarise(loops: list[np.ndarray]) -> dict:
    """Classify loops by nesting depth: even depth = material outline, odd = void."""
    from shapely.geometry import Point, Polygon

    polys = [Polygon(p) for p in loops]  # loops never cross, so one vertex tests nesting
    area = void = perim = 0.0
    out = []
    for i, (p, poly) in enumerate(zip(loops, polys, strict=True)):
        depth = sum(1 for j, q in enumerate(polys) if j != i and q.contains(Point(p[0])))
        a = abs(_ring_area(p))
        if depth % 2:
            void += a
            area -= a
        else:
            area += a
        perim += float(np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum())
        out.append({"kind": "void" if depth % 2 else "outer", "points": np.round(p, 3).tolist()})
    return {"loops": out, "area_mm2": area, "void_area_mm2": void, "perimeter_mm": perim}


def section_mesh(mesh, axis: str, offset: float) -> dict:
    """Plane cut of a trimesh mesh at `axis = offset` (world mm)."""
    from trimesh.intersections import mesh_plane

    n = np.zeros(3)
    n[AXES[axis]] = 1.0
    origin = n * offset
    segs = mesh_plane(mesh, n, origin)  # (k, 2, 3) segments
    if len(segs) == 0:
        return {"loops": [], "area_mm2": 0.0, "void_area_mm2": 0.0, "perimeter_mm": 0.0}
    import trimesh

    path = trimesh.load_path(segs)
    u, v = UV[axis]
    loops = [np.asarray(d)[:, [u, v]] for d in path.discrete]
    loops = [lp[:-1] if np.allclose(lp[0], lp[-1]) else lp for lp in loops]
    return _summarise(loops)


def section_exact(shape, axis: str, offset: float, deflection: float = 0.01) -> dict:
    """Exact OCCT section: intersect with a large planar face, read faces and wires."""
    from build123d import Axis, Face, Plane, Vector

    bb = shape.bounding_box()
    size = 4 * max(bb.size.X, bb.size.Y, bb.size.Z)
    n = Vector(*(1.0 if i == AXES[axis] else 0.0 for i in range(3)))
    c = bb.center()
    origin = Vector(*(offset if i == AXES[axis] else (c.X, c.Y, c.Z)[i] for i in range(3)))
    plane = Plane(origin=origin, z_dir=n)
    cutter = Face.make_rect(size, size, plane)
    res = shape & cutter
    faces = res.faces() if res is not None else []
    u, v = UV[axis]
    loops, area, void, perim = [], 0.0, 0.0, 0.0
    for f in faces:
        area += f.area
        outer = f.outer_wire()
        for w in f.wires():
            is_outer = w.is_same(outer)
            perim += w.length
            if not is_outer:
                void += Face(w).area
            pts = [w.position_at(t) for t in np.linspace(0, 1, max(16, int(w.length / 0.5)))]
            p = np.array([(q.X, q.Y, q.Z) for q in pts])[:-1][:, [u, v]]
            loops.append(
                {"kind": "outer" if is_outer else "void", "points": np.round(p, 3).tolist()}
            )
    del Axis
    return {"loops": loops, "area_mm2": area, "void_area_mm2": void, "perimeter_mm": perim}


def section_exact_fast(shape, axis: str, offset: float) -> dict:
    """Exact section via BRepAlgoAPI_Section (edges only) + wire building; no solid boolean."""
    from build123d import Edge, Face, Wire
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    d = [0.0, 0.0, 0.0]
    d[AXES[axis]] = 1.0
    o = [0.0, 0.0, 0.0]
    o[AXES[axis]] = offset
    sec = BRepAlgoAPI_Section(shape.wrapped, gp_Pln(gp_Pnt(*o), gp_Dir(*d)), False)
    sec.Approximation(True)
    sec.Build()
    from build123d import Compound

    edges = Compound(sec.Shape()).edges()
    wires = Wire.combine(edges, tol=1e-4) if edges else []
    u, v = UV[axis]
    faces = [(w, Face(w).area) for w in wires if w.is_closed]
    loops = []
    for w, _a in faces:
        pts = [w.position_at(t) for t in np.linspace(0, 1, max(16, int(w.length / 0.5)))]
        loops.append(np.array([(q.X, q.Y, q.Z) for q in pts])[:-1][:, [u, v]])
    r = _summarise(loops)
    # replace polygon areas/perimeters with exact ones, keeping the nesting classification
    area = void = 0.0
    for (w, a), lp in zip(faces, r["loops"], strict=True):
        if lp["kind"] == "void":
            void += a
            area -= a
        else:
            area += a
    r.update(area_mm2=area, void_area_mm2=void, perimeter_mm=sum(w.length for w, _ in faces))
    del Edge
    return r


def _exact_by_name(name: str, axis: str, offset: float) -> dict:
    """Module-level entry for run_guarded (rebuilds the shape in the child: shapes pickle,
    but rebuilding keeps the payload tiny and mirrors the API, which loads a build's STEP)."""
    import shapes

    t = time.perf_counter()
    r = section_exact(shapes.SHAPES[name](), axis, offset)
    r["child_s"] = time.perf_counter() - t
    return r


def _exact_from_file(path: str, axis: str, offset: float) -> dict:
    """Child entry: load the B-rep from disk (the API would read the build's model/body.step).
    Passing the shape itself fails: boolean results carry a BRepTools_History that won't pickle."""
    from build123d import import_brep

    t = time.perf_counter()
    r = section_exact(import_brep(path), axis, offset)
    r["child_s"] = time.perf_counter() - t
    return r


def main() -> None:
    import shapes

    from luregen.geometry.export import tessellate
    from luregen.geometry.validate import run_guarded

    cuts = [("x", 30.0), ("x", 87.5), ("x", 150.0), ("y", 0.0), ("y", 4.0), ("z", 1.0)]
    rows = []
    for name, build in shapes.SHAPES.items():
        shape = build()
        brep = Path(__file__).resolve().parents[1] / f"out/{name}.brep"
        brep.parent.mkdir(exist_ok=True)
        from build123d import export_brep

        export_brep(shape, brep)
        t = time.perf_counter()
        mesh = tessellate(shape).mesh  # F2 export mesh (what model/body.stl holds)
        t_mesh = time.perf_counter() - t
        for axis, off in cuts:
            t = time.perf_counter()
            m = section_mesh(mesh, axis, off)
            t_m = time.perf_counter() - t
            t = time.perf_counter()
            e_direct = section_exact(shape, axis, off)
            t_e = time.perf_counter() - t
            t = time.perf_counter()
            f = section_exact_fast(shape, axis, off)
            t_f = time.perf_counter() - t
            t = time.perf_counter()
            e = run_guarded(_exact_from_file, str(brep), axis, off, timeout=60)
            t_g = time.perf_counter() - t
            rel = (m["area_mm2"] / e["area_mm2"] - 1) if e["area_mm2"] else float("nan")
            rows.append(
                {
                    "shape": name,
                    "cut": f"{axis}={off:g}",
                    "exact_area": round(e["area_mm2"], 3),
                    "mesh_area": round(m["area_mm2"], 3),
                    "area_err": f"{rel:+.3%}",
                    "exact_void": round(e["void_area_mm2"], 3),
                    "mesh_void": round(m["void_area_mm2"], 3),
                    "exact_perim": round(e["perimeter_mm"], 3),
                    "mesh_perim": round(m["perimeter_mm"], 3),
                    "loops_exact": [lp["kind"] for lp in e["loops"]],
                    "loops_mesh": [lp["kind"] for lp in m["loops"]],
                    "mesh_ms": round(1000 * t_m, 1),
                    "exact_ms": round(1000 * t_e, 1),
                    "fast_ms": round(1000 * t_f, 1),
                    "fast_area": round(f["area_mm2"], 3),
                    "fast_void": round(f["void_area_mm2"], 3),
                    "exact_guarded_ms": round(1000 * t_g, 1),
                    "child_ms": round(1000 * e["child_s"], 1),
                    "mesh_build_ms": round(1000 * t_mesh, 1),
                }
            )
            assert abs(e_direct["area_mm2"] - e["area_mm2"]) < 1e-6
    for r in rows:
        print(json.dumps(r))
    cav = [r for r in rows if r["shape"] == "cavity" and r["cut"] in ("x=87.5", "y=0", "z=1")]
    assert all(r["exact_void"] > 0 and r["mesh_void"] > 0 for r in cav), "void not found"
    print("cavity void > 0 at mid cuts: OK")


if __name__ == "__main__":
    main()
