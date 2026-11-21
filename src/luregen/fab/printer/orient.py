"""Orient a print section on the bed and export its STL (#35).

Sections print **upright** (decided in #25): lure z up, belly on the plate (on supports for a
loft3d body), dorsal up, the joint face vertical. In lure coordinates that is no rotation at
all, only a translation: the lowest point goes to Z = 0 and the footprint is centered on the
bed. The slicer drops objects onto the plate the same way, so a lure Z maps to a print Z by
`z - z_min`, which is how the cavity-ceiling pause height is computed.

A separate tail fin (#123) prints **flat**: lying on its side (lure y up), so its flat face
is on the plate and it needs no supports.

The section mesh is the build's `model/<section>.stl`, written by
`luregen.geometry.export.tessellate`: one mesh with the outer skin and the sealed cavity as an
inner shell (normals pointing into the void), which the slicer prints as a closed hollow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

ORIENTATION = "upright"
ORIENTATIONS = ("upright", "flat")


class OrientError(ValueError):
    pass


@dataclass
class OrientedSection:
    name: str
    mesh: object  # trimesh.Trimesh, in print coordinates
    transform: np.ndarray  # 4x4, lure → print coordinates
    shells: int  # connected components: 1 outer skin + one per sealed void
    orientation: str = ORIENTATION

    @property
    def z_offset(self) -> float:
        """Print Z = lure Z + z_offset."""
        return float(self.transform[2, 3])

    def print_z(self, lure_z: float) -> float:
        return float(lure_z) + self.z_offset

    @property
    def height(self) -> float:
        return float(self.mesh.bounds[1][2] - self.mesh.bounds[0][2])

    def as_dict(self) -> dict:
        lo, hi = (np.round(b, 3).tolist() for b in self.mesh.bounds)
        return {
            "orientation": self.orientation,
            "transform": np.round(self.transform, 6).tolist(),
            "z_offset_mm": round(self.z_offset, 3),
            "bounds_mm": [lo, hi],
            "height_mm": round(self.height, 3),
            "shells": self.shells,
            "volume_cm3": round(float(self.mesh.volume) / 1000.0, 4),
            "triangles": len(self.mesh.faces),
        }


def bed_center(machine_profile: dict) -> tuple[float, float]:
    """Center of an Orca `printable_area` (["x0xy0", "x1xy1", …])."""
    pts = np.array([[float(v) for v in p.split("x")] for p in machine_profile["printable_area"]])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    return float((lo[0] + hi[0]) / 2), float((lo[1] + hi[1]) / 2)


def upright_transform(bounds: np.ndarray, center_xy: tuple[float, float]) -> np.ndarray:
    """Lure → print: z up as modeled, lowest point on Z = 0, footprint centered on the bed."""
    lo, hi = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    t = np.eye(4)
    t[0, 3] = center_xy[0] - (lo[0] + hi[0]) / 2
    t[1, 3] = center_xy[1] - (lo[1] + hi[1]) / 2
    t[2, 3] = -lo[2]
    return t


# Lure → print rotation for a flat part: +90° about x, so lure y becomes print Z.
FLAT_ROTATION = np.array(
    [[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
)


def orient_mesh(
    name: str, mesh, center_xy: tuple[float, float], orientation: str = ORIENTATION
) -> OrientedSection:
    """Orient a closed mesh upright (or flat, lying on its side); it must be watertight
    with consistent winding."""
    if orientation not in ORIENTATIONS:
        raise OrientError(f"{name}: unknown orientation {orientation!r}")
    if not (mesh.is_watertight and mesh.is_winding_consistent):
        raise OrientError(f"{name}: mesh is not closed (watertight/winding); rebuild the model")
    shells = len(mesh.split(only_watertight=False))
    rot = FLAT_ROTATION if orientation == "flat" else np.eye(4)
    out = mesh.copy()
    out.apply_transform(rot)
    t = upright_transform(out.bounds, center_xy)
    out.apply_transform(t)
    return OrientedSection(name, out, t @ rot, shells, orientation)


def orient_section(
    name: str,
    stl: Path,
    center_xy: tuple[float, float],
    out_stl: Path | None = None,
    orientation: str = ORIENTATION,
) -> OrientedSection:
    """Load `model/<part>.stl`, orient it and optionally write the print STL."""
    import trimesh

    if not Path(stl).is_file():
        raise OrientError(f"{name}: {stl} not found; is this a print build?")
    mesh = trimesh.load_mesh(stl, process=True)
    section = orient_mesh(name, mesh, center_xy, orientation)
    if out_stl is not None:
        Path(out_stl).parent.mkdir(parents=True, exist_ok=True)
        section.mesh.export(out_stl)
    return section
