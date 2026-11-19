"""CNC DFM rules: two wood/board halves glued on y = 0 (#40), machined from the glue face
(setup 1) and from the outer face after the flip (setup 2, #41)."""

from __future__ import annotations

import math

import numpy as np
import trimesh

from luregen.config import LureConfig
from luregen.dfm.mesh import face_regions, min_distance
from luregen.dfm.model import DfmModel
from luregen.dfm.registry import Violation, rule
from luregen.geometry.cnc_halves import tool_diameter_from_id

CNC = ("cnc",)
GLUE_FACE_TOL_MM = 0.05  # a glue-face pocket's void crosses y = 0 by more than this
FOOTPRINT_TOL_MM = 0.5  # void bbox vs the declared pocket footprint


def glue_face_pockets(model: DfmModel, section: str) -> list[dict]:
    """Declared glue-face pockets of a section (report geometry): ballast + air pockets."""
    g = model.geometry
    pockets = []
    ballast = (g.get("ballast") or {}).get("glue_face_pocket")
    if ballast and (g.get("ballast") or {}).get("section", "head") == section:
        pockets.append(ballast)
    for p in g.get("cnc_air_pockets") or []:
        if p.get("kind", "").split(":")[-1] == section:
            pockets.append(p)
    return pockets


def _matches(inner, pocket: dict) -> bool:
    lo, hi = inner.bounds
    x0, x1 = pocket["x"] - pocket["length"] / 2, pocket["x"] + pocket["length"] / 2
    z0, z1 = pocket["z"] - pocket["height"] / 2, pocket["z"] + pocket["height"] / 2
    t = FOOTPRINT_TOL_MM
    return (
        abs(lo[0] - x0) < t and abs(hi[0] - x1) < t and abs(lo[2] - z0) < t and abs(hi[2] - z1) < t
    )


@rule(
    "cnc.no_internal_voids",
    severity="error",
    summary="No internal voids: every closed void is a declared glue-face pocket",
    process=CNC,
)
def no_internal_voids(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    out = []
    for s in model.sections:
        pockets = glue_face_pockets(model, s)
        unmatched = list(pockets)
        for inner in model.shells(s).inners:
            lo, hi = inner.bounds
            if not (lo[1] < -GLUE_FACE_TOL_MM and hi[1] > GLUE_FACE_TOL_MM):
                out.append(
                    Violation(
                        f"{s}: void at x {lo[0]:.1f}–{hi[0]:.1f}, z {lo[2]:.1f}–{hi[2]:.1f} mm "
                        "does not reach the glue face (cannot be machined in a half)",
                        s,
                    )
                )
                continue
            match = next((p for p in unmatched if _matches(inner, p)), None)
            if match is None:
                out.append(
                    Violation(
                        f"{s}: void at x {lo[0]:.1f}–{hi[0]:.1f}, z {lo[2]:.1f}–{hi[2]:.1f} mm "
                        "is not a declared glue-face pocket (wood bodies have no cavity)",
                        s,
                    )
                )
            else:
                unmatched.remove(match)
    return out


@rule(
    "cnc.min_wall",
    severity="error",
    summary="Wood wall around every pocket ≥ min_wood_wall_mm",
    process=CNC,
)
def min_wall(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.cnc
    out = []
    for s in model.sections:
        shells = model.shells(s)
        for inner in shells.inners:
            dist, at = min_distance(inner, shells.outer)
            if dist < d.min_wood_wall_mm - d.mesh_tol_mm:
                out.append(
                    Violation(
                        f"{s}: {dist:.2f} mm of wood around the pocket at x {at[0]:.1f}, "
                        f"z {at[2]:.1f} mm (< {d.min_wood_wall_mm} mm)",
                        s,
                        dist,
                        d.min_wood_wall_mm,
                        "mm",
                    )
                )
    return out


@rule(
    "cnc.corner_radius",
    severity="error",
    summary="Inside corner radius ≥ tool radius + corner_margin_mm (V-joint apex, pockets)",
    process=CNC,
)
def corner_radius(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.cnc
    out = []
    joint = model.geometry.get("joint")
    if joint is not None:
        need = tool_diameter_from_id(cfg.cnc.ops.profile.tool) / 2 + d.corner_margin_mm
        r = joint["tail_notch_r_mm"]
        if r < need - 1e-6:
            out.append(
                Violation(
                    f"tail: V-joint notch radius {r:.3f} mm < {cfg.cnc.ops.profile.tool} radius "
                    f"+ {d.corner_margin_mm} mm = {need:.3f} mm",
                    "tail",
                    r,
                    need,
                    "mm",
                )
            )
    need = tool_diameter_from_id(cfg.cnc.ops.pocket.tool) / 2 + d.corner_margin_mm
    for s in model.sections:
        for p in glue_face_pockets(model, s):
            if p["corner_r"] < need - 1e-6:
                out.append(
                    Violation(
                        f"{s}: {p.get('kind', 'pocket')} pocket corner radius "
                        f"{p['corner_r']:.3f} mm < {need:.3f} mm",
                        s,
                        p["corner_r"],
                        need,
                        "mm",
                    )
                )
    return out


@rule(
    "cnc.pocket_depth",
    severity="error",
    summary="Pocket depth ≤ max_pocket_depth_x_dia × tool diameter",
    process=CNC,
)
def pocket_depth(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.cnc
    limit = d.max_pocket_depth_x_dia * tool_diameter_from_id(cfg.cnc.ops.pocket.tool)
    out = []
    for s in model.sections:
        for p in glue_face_pockets(model, s):
            if p["depth"] > limit + 1e-6:
                out.append(
                    Violation(
                        f"{s}: {p.get('kind', 'pocket')} pocket {p['depth']:.2f} mm deep per "
                        f"half > {limit:.2f} mm ({d.max_pocket_depth_x_dia:g} × "
                        f"{cfg.cnc.ops.pocket.tool})",
                        s,
                        p["depth"],
                        limit,
                        "mm",
                    )
                )
    return out


def undercut_faces(mesh, tol_deg: float, glue_tol: float = GLUE_FACE_TOL_MM) -> np.ndarray:
    """Faces of a glued body that the tool cannot reach from their half's outer side: in
    the right half (y > 0) the tool comes from +y, so a face is reachable when n_y ≥ 0."""
    c = mesh.triangles_center
    side = np.sign(c[:, 1])
    ny = mesh.face_normals[:, 1] * side
    return np.flatnonzero((np.abs(c[:, 1]) > glue_tol) & (ny < -math.sin(math.radians(tol_deg))))


@rule(
    "cnc.no_undercuts",
    severity="error",
    summary="No undercuts from each machining side (outer surface of each half)",
    process=CNC,
)
def no_undercuts(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    mesh = model.meshes.get("body")
    if mesh is None:
        return [Violation("no body mesh (model/body.stl) to check")]
    mesh = trimesh.Trimesh(mesh.vertices, mesh.faces, process=False)  # normals from winding
    bad = undercut_faces(mesh, cfg.dfm.cnc.undercut_tol_deg)
    regions = [(float(mesh.area_faces[f].sum()), f) for f in face_regions(mesh, bad)]
    regions = sorted(
        [r for r in regions if r[0] >= cfg.dfm.cnc.undercut_min_area_mm2],
        key=lambda r: r[0],
        reverse=True,
    )
    out = []
    for area, faces in regions[:5]:
        v = mesh.triangles[faces].reshape(-1, 3)
        lo, hi = v.min(axis=0), v.max(axis=0)
        out.append(
            Violation(
                f"undercut on the outer surface at x {lo[0]:.1f}–{hi[0]:.1f}, "
                f"z {lo[2]:.1f}–{hi[2]:.1f} mm ({area:.1f} mm²), hidden from the "
                f"{'right' if hi[1] > 0 else 'left'} half's machining side",
                None,
                area,
                0.0,
                "mm²",
            )
        )
    return out


def holes(model: DfmModel) -> list[dict]:
    """Holes and slots as {name, axis ("x" | "y" | "z"), y_mm (centre)}: the family's
    `geometry.holes` when it lists them, else derived from the joint block (the vertical
    pin bore and the tail's eye slots, which run along x but are split by the glue face)."""
    g = model.geometry
    if "holes" in g:
        return list(g["holes"])
    joint = g.get("joint")
    if not joint:
        return []
    out = [{"name": "hinge pin bore", "axis": "z", "y_mm": 0.0}]
    for _x, z in joint.get("hinge_eyes_mm", []):
        out.append({"name": f"hinge eye slot z {z:g}", "axis": "x", "y_mm": 0.0})
    return out


@rule(
    "cnc.no_axial_holes",
    severity="error",
    summary="No axial holes: features along x must be half-grooves split by the glue face",
    process=CNC,
)
def no_axial_holes(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    out = []
    for h in holes(model):
        if h["axis"] == "x" and abs(h["y_mm"]) > GLUE_FACE_TOL_MM:
            out.append(
                Violation(
                    f"axial hole {h['name']!r} at y {h['y_mm']:.2f} mm is not on the glue face; "
                    "cut it as a half-groove on y = 0",
                    h.get("section"),
                    abs(h["y_mm"]),
                    0.0,
                    "mm",
                )
            )
    return out


@rule(
    "cnc.roundover",
    severity="error",
    summary="Roundover ≤ max_fillet() and ≤ the outline's min curvature radius",
    process=CNC,
    body_style=("profile25d",),
)
def roundover(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    ro = model.envelope.get("roundover")
    if ro is None:
        return [Violation("profile25d envelope has no roundover block")]
    tol = cfg.dfm.cnc.mesh_tol_mm
    out = []
    if ro["requested_mm"] > ro["applied_mm"] + tol:
        out.append(
            Violation(
                f"roundover {ro['requested_mm']} mm was clamped to {ro['applied_mm']:.2f} mm "
                f"({ro['limited_by']}); set dfm.cnc.roundover_r_mm ≤ {ro['applied_mm']:.2f}",
                None,
                ro["requested_mm"],
                ro["applied_mm"],
                "mm",
            )
        )
    if ro["applied_mm"] > ro["min_curvature_radius_mm"] + tol:
        out.append(
            Violation(
                f"roundover {ro['applied_mm']:.2f} mm > the outline's min curvature radius "
                f"{ro['min_curvature_radius_mm']:.2f} mm",
                None,
                ro["applied_mm"],
                ro["min_curvature_radius_mm"],
                "mm",
            )
        )
    return out


@rule(
    "cnc.through_wire",
    severity="warn",
    summary="Through-wire rigging recommended for wood",
    process=CNC,
)
def through_wire(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    if cfg.process.rigging == "through_wire":
        return []
    return [
        Violation(
            f"rigging is {cfg.process.rigging!r} in {cfg.process.material}; a through-wire "
            "(process.rigging = 'through_wire') is recommended for wood"
        )
    ]
