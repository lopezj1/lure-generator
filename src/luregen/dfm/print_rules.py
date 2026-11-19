"""Print DFM rules. Sections are printed upright (z up, belly on supports; epic #1 decision 8)."""

from __future__ import annotations

import math

import numpy as np

from luregen.config import LureConfig
from luregen.dfm.mesh import closest_points, face_regions, first_hit, ray_hits
from luregen.dfm.model import DfmModel
from luregen.dfm.registry import Violation, rule

PRINT = ("print",)
MAX_REPORTED = 5  # violations per rule and section
APPROACH_MM = 50.0  # screw-eye entry search: ray start this far outside along -axis


@rule(
    "print.single_void",
    severity="error",
    summary="Exactly one sealed void per section",
    process=PRINT,
)
def single_void(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    out = []
    for s in model.sections:
        n = len(model.shells(s).inners)
        if n != 1:
            out.append(Violation(f"{s}: {n} sealed voids (expected exactly 1)", s, n, 1, "voids"))
    return out


@rule(
    "print.min_wall",
    severity="error",
    summary="Cavity wall ≥ min_wall_mm and skin-to-cavity ≥ min_skin_to_cavity_mm",
    process=PRINT,
)
def min_wall(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.print
    internals = model.geometry.get("internals") or {}
    out = []
    for s in model.sections:
        block = internals.get(s)
        if block is None:
            out.append(Violation(f"{s}: no internals in the report geometry", s))
            continue
        if block["wall_thk_mm"] < d.min_wall_mm:
            out.append(
                Violation(
                    f"{s}: cavity wall {block['wall_thk_mm']:.2f} mm < {d.min_wall_mm} mm",
                    s,
                    block["wall_thk_mm"],
                    d.min_wall_mm,
                    "mm",
                )
            )
        if block["min_skin_mm"] < d.min_skin_to_cavity_mm:
            out.append(
                Violation(
                    f"{s}: skin to cavity {block['min_skin_mm']:.2f} mm < "
                    f"{d.min_skin_to_cavity_mm} mm",
                    s,
                    block["min_skin_mm"],
                    d.min_skin_to_cavity_mm,
                    "mm",
                )
            )
    return out


def overhang_regions(inner, max_deg: float) -> list[dict]:
    """Ceiling patches of a void steeper than `max_deg` from vertical.

    An inner shell's normals point into the material, so a ceiling face has n_z > 0 and
    its overhang angle from vertical is asin(n_z) (vertical wall 0°, flat ceiling 90°).
    The bridge span of a patch is the smaller of its horizontal (x, y) extents.
    """
    nz = np.clip(inner.face_normals[:, 2], -1.0, 1.0)
    angle = np.degrees(np.arcsin(nz))
    bad = np.flatnonzero(angle > max_deg)
    regions = []
    for faces in face_regions(inner, bad):
        v = inner.triangles[faces].reshape(-1, 3)
        lo, hi = v.min(axis=0), v.max(axis=0)
        regions.append(
            {
                "span_mm": float(min(hi[0] - lo[0], hi[1] - lo[1])),
                "area_mm2": float(inner.area_faces[faces].sum()),
                "max_deg": float(angle[faces].max()),
                "lo": lo,
                "hi": hi,
            }
        )
    return sorted(regions, key=lambda r: r["span_mm"], reverse=True)


@rule(
    "print.cavity_overhang",
    severity="error",
    summary="Cavity ceiling ≤ max_overhang_deg from vertical, printed upright "
    "(flatter patches only as bridges ≤ max_bridge_mm)",
    process=PRINT,
)
def cavity_overhang(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.print
    out = []
    for s in model.sections:
        for inner in model.shells(s).inners:
            regions = overhang_regions(inner, d.max_overhang_deg + d.overhang_tol_deg)
            for r in [r for r in regions if r["span_mm"] > d.max_bridge_mm][:MAX_REPORTED]:
                lo, hi = r["lo"], r["hi"]
                out.append(
                    Violation(
                        f"{s}: cavity ceiling at x {lo[0]:.1f}–{hi[0]:.1f}, z {lo[2]:.1f}–"
                        f"{hi[2]:.1f} mm is {r['max_deg']:.0f}° from vertical over a "
                        f"{r['span_mm']:.1f} mm span (max {d.max_overhang_deg:g}°, bridges "
                        f"≤ {d.max_bridge_mm:g} mm)",
                        s,
                        r["span_mm"],
                        d.max_bridge_mm,
                        "mm",
                    )
                )
    return out


def screw_eyes(cfg: LureConfig, section: str):
    return [h for h in cfg.hardware if h.section == section and h.id.startswith("screw_eye")]


def _near(p, q, r: float) -> bool:
    return q is not None and math.dist(p, q) <= r


def eye_axis(model: DfmModel, cfg: LureConfig, item, outer) -> np.ndarray:
    """Thread direction into the body: +x for the line tie (into the nose), −x for the
    head's hinge eyes (into its V face), else the inward surface normal nearest the eye."""
    joint = model.geometry.get("joint") or {}
    p = (item.x_mm, item.z_mm)
    r = cfg.internals.boss_r_mm
    if _near(p, joint.get("line_tie_mm"), r):
        return np.array([1.0, 0.0, 0.0])
    if item.section == "head" and any(_near(p, e, r) for e in joint.get("hinge_eyes_mm", [])):
        return np.array([-1.0, 0.0, 0.0])
    _, _, tri = closest_points(outer, [[item.x_mm, 0.0, item.z_mm]])
    return -outer.face_normals[int(tri[0])]


def eye_insertion(model: DfmModel, cfg: LureConfig, section: str, item) -> dict | None:
    """Entry point on the outer surface, thread axis and solid length along the axis."""
    shells = model.shells(section)
    axis = eye_axis(model, cfg, item, shells.outer)
    p = np.array([item.x_mm, 0.0, item.z_mm])
    start = p - APPROACH_MM * axis
    hits = ray_hits(shells.outer, start, axis)
    if not len(hits):
        return None
    entry = start + hits[0] * axis
    solid = first_hit(model.meshes[section], entry + 1e-3 * axis, axis)
    return {"axis": axis, "entry": entry, "solid_mm": solid}


@rule(
    "print.screw_eye_solid",
    severity="error",
    summary="Screw eyes only into solid bosses (thread length in solid, clear of voids)",
    process=PRINT,
)
def screw_eye_solid(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.print
    need_clear = d.screw_eye_shank_d_mm / 2 + d.screw_eye_min_solid_mm
    out = []
    for s in model.sections:
        inners = model.shells(s).inners
        for item in screw_eyes(cfg, s):
            name = item.name or item.id
            ins = eye_insertion(model, cfg, s, item)
            if ins is None:
                out.append(Violation(f"{s}: {name} does not reach the section surface", s))
                continue
            if ins["solid_mm"] < d.screw_eye_shank_mm:
                out.append(
                    Violation(
                        f"{s}: {name} has {ins['solid_mm']:.1f} mm of solid along its thread "
                        f"(needs {d.screw_eye_shank_mm:g} mm)",
                        s,
                        ins["solid_mm"],
                        d.screw_eye_shank_mm,
                        "mm",
                    )
                )
                continue
            along = ins["entry"] + np.outer(np.linspace(0, d.screw_eye_shank_mm, 13), ins["axis"])
            for inner in inners:
                _, dist, _ = closest_points(inner, along)
                if dist.min() < need_clear:
                    out.append(
                        Violation(
                            f"{s}: {name} thread is {dist.min():.2f} mm from a void "
                            f"(needs {need_clear:.2f} mm: thread radius + "
                            f"{d.screw_eye_min_solid_mm:g} mm solid)",
                            s,
                            float(dist.min()),
                            need_clear,
                            "mm",
                        )
                    )
    return out


@rule(
    "print.boss_layers",
    severity="warn",
    summary="Hook pull across layers: solid along a vertical screw eye ≥ boss_min_layers",
    process=PRINT,
)
def boss_layers(model: DfmModel, cfg: LureConfig) -> list[Violation]:
    d = cfg.dfm.print
    cos_max = math.cos(math.radians(d.boss_pull_axis_deg))
    out = []
    for s in model.sections:
        for item in screw_eyes(cfg, s):
            ins = eye_insertion(model, cfg, s, item)
            if ins is None or abs(ins["axis"][2]) < cos_max:
                continue  # pull within the layers (or no entry: print.screw_eye_solid)
            height = min(ins["solid_mm"], 1e6) * abs(ins["axis"][2])
            layers = int(height / d.layer_height_mm + 1e-6)
            if layers < d.boss_min_layers:
                out.append(
                    Violation(
                        f"{s}: {item.name or item.id} pulls across layers with "
                        f"{height:.1f} mm ({layers} layers) of solid boss "
                        f"(want ≥ {d.boss_min_layers})",
                        s,
                        layers,
                        d.boss_min_layers,
                        "layers",
                    )
                )
    return out
