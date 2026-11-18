"""Adjudicate exact-section disagreements on the cavity body.

The inset loft lies fully inside the outer loft, so section(body) must equal
section(outer) minus section(inner). Each single-face loft is checked three ways: a fine
structured mesh (F2's loft_grid, converges to the surface), the solid boolean, and the
BRepAlgoAPI_Section edges.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import shapes  # noqa: E402
from section import section_exact, section_exact_fast, section_mesh  # noqa: E402

from luregen.geometry.export import tessellate  # noqa: E402

if __name__ == "__main__":
    body, inner = shapes.cavity_body()
    outer = shapes.loft()
    for axis, off in (("y", 4.0), ("z", 1.0), ("x", 87.5)):
        print(f"--- {axis}={off}", flush=True)
        for nm, s in (("outer", outer), ("inner", inner)):
            fine = section_mesh(tessellate(s, 0.002, 0.02, max_triangles=None).mesh, axis, off)
            e, f = section_exact(s, axis, off), section_exact_fast(s, axis, off)
            print(
                f"{nm}: fine-mesh {fine['area_mm2']:.2f}  boolean {e['area_mm2']:.2f}  "
                f"section-edges {f['area_mm2']:.2f}",
                flush=True,
            )
        e, f = section_exact(body, axis, off), section_exact_fast(body, axis, off)
        print(
            f"body: boolean {e['area_mm2']:.2f}/{e['void_area_mm2']:.2f}  "
            f"section-edges {f['area_mm2']:.2f}/{f['void_area_mm2']:.2f}",
            flush=True,
        )
