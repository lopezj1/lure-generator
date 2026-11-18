"""Spike #58: tessellate the test shapes into three-cad-viewer JSON (protocol v3).

Writes spike/web/public/models/<name>.json (one shape) and scene.json (all three, named
parts), and prints mesh quality of ocp-tessellate's BRepMesh output vs F2's own mesher.

    uv run --group viewer python spike/py/export_tcv.py [--deviation 0.1] [--hybrid]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).parent))
import shapes  # noqa: E402
from ocp_tessellate.convert import export_three_cad_viewer_js  # noqa: E402

from luregen.geometry.export import tessellate as f2_tessellate  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "web/public/models"


def mesh_quality(shape_json: dict, brep_volume: float) -> dict:
    v = np.asarray(shape_json["vertices"], dtype=float).reshape(-1, 3)
    f = np.asarray(shape_json["triangles"], dtype=np.int64).reshape(-1, 3)
    m = trimesh.Trimesh(v, f, process=True)  # merge per-face duplicate vertices
    return {
        "triangles": len(f),
        "watertight": bool(m.is_watertight),
        "volume_err": round(abs(m.volume) / brep_volume - 1.0, 5),
    }


def hybrid_override(part: dict, shape) -> None:
    """Replace a one-face tipped loft's BRepMesh triangles with F2's structured grid.

    Keeps ocp-tessellate's edges/face_types so the viewer's tree, edges and picking work.
    Only valid when the shape has one face (triangles_per_face == [n]).
    """
    s = part["shape"]
    if len(s["triangles_per_face"]) != 1:
        return
    tess = f2_tessellate(shape, max_triangles=None)
    if tess.mesher != "loft_grid":
        return
    m = tess.mesh
    s["vertices"] = np.asarray(m.vertices, dtype=np.float32).ravel().tolist()
    s["triangles"] = np.asarray(m.faces, dtype=np.uint32).ravel().tolist()
    s["normals"] = np.asarray(m.vertex_normals, dtype=np.float32).ravel().tolist()
    s["triangles_per_face"] = [len(m.faces)]


def export(name, objs, names, colors, deviation, angular, hybrid):
    t = time.perf_counter()
    js = export_three_cad_viewer_js(None, *objs, names=names, colors=colors)
    dt = time.perf_counter() - t
    data = json.loads(js)
    # ocp-tessellate reads deviation/angular_tolerance from presets; re-run with kwargs
    return data, dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deviation", type=float, default=0.1)
    ap.add_argument("--angular", type=float, default=0.2)
    ap.add_argument("--hybrid", action="store_true", help="F2 grid mesh for the loft faces")
    a = ap.parse_args()

    from ocp_tessellate.convert import tessellate_group, to_ocpgroup

    OUT.mkdir(parents=True, exist_ok=True)
    built = {k: f() for k, f in shapes.SHAPES.items()}
    colors = {"loft3d": "#e8b024", "profile25d": "#4a90d9", "cavity": "#5cb85c"}
    kw = {"deviation": a.deviation, "angular_tolerance": a.angular, "render_edges": True}

    def to_json(objs, names):
        t = time.perf_counter()
        group, inst = to_ocpgroup(*objs, names=names, colors=[colors[n] for n in names])
        inst, tree, _ = tessellate_group(group, inst, kw)
        dt = time.perf_counter() - t

        def walk(o):
            for p in o.get("parts", []):
                walk(p)
            if o.get("type") == "shapes" and "ref" in o.get("shape", {}):
                o["shape"] = inst[o["shape"]["ref"]]

        walk(tree)
        return json.loads(json.dumps(tree, default=_np)), dt

    report = {}
    for n, s in built.items():
        data, dt = to_json([s], [n])
        part = data["parts"][0]
        q = mesh_quality(part["shape"], s.volume)
        if a.hybrid and n != "profile25d":  # also keep the plain BRepMesh version to compare
            (OUT / f"{n}_brepmesh.json").write_text(json.dumps(data, separators=(",", ":")))
        if a.hybrid:
            hybrid_override(part, s)
            q["hybrid"] = mesh_quality(part["shape"], s.volume)
        q["tessellate_s"] = round(dt, 3)
        report[n] = q
        txt = json.dumps(data, separators=(",", ":"))
        (OUT / f"{n}.json").write_text(txt)
        q["json_kb"] = round(len(txt) / 1024)

    # one scene, three named parts side by side (z offset) for the tree demo
    from build123d import Pos

    placed = [
        built["loft3d"],
        Pos(0, 0, -70) * built["profile25d"],
        Pos(0, 0, -140) * built["cavity"],
    ]
    data, dt = to_json(placed, list(built))
    if a.hybrid:
        hybrid_override(data["parts"][0], placed[0])
    (OUT / "scene.json").write_text(json.dumps(data, separators=(",", ":")))
    report["scene_tessellate_s"] = round(dt, 3)
    print(json.dumps({"args": vars(a), **report}, indent=1))


def _np(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


if __name__ == "__main__":
    main()
