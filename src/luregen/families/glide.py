"""Glide bait: two sections (head, tail) joined by a V-joint with a vertical hinge pin.

Order of operations on the envelope (booleans on the full body where possible, validity
asserted after each):

1. ballast pocket (head) and trim pocket (tail), cut on the full body;
2. pin bore on the full body, V-gap split into head and tail, then the tail's eye slots;
3. print only: one sealed cavity per section (explicit inset loft, roof, solid keel),
   kept clear of every other feature, and solid bosses around the screw eyes.

With `[fin]` (#123) the pointed tail tip is cut off before the split, the fin slot and the
lateral screw hole are cut into the new tail end (`geometry.fin`), and the fin is built as
a separate part (`FamilyBuild.parts`). Its mass and displacement count in the tail section
(it moves with the tail), and its screw is a tail point mass.

CNC (wood) sections get no cavity (F10: no void for wood). Their ballast sits in a closed
glue-face pocket (and optional air pockets) cut into the two halves (`geometry.cnc_halves`),
so a CNC section is 1 solid with 1 shell plus one per sealed pocket.
"""

from __future__ import annotations

from typing import Any

from luregen.config import LureConfig
from luregen.config.models import JointConfig
from luregen.families import Emit, FamilyBuild
from luregen.geometry.envelope import Envelope
from luregen.physics.massprops import Part, PointMass, SectionInput
from luregen.profile.table import StationTable

SECTIONS = ("head", "tail")
SLICE_SAMPLES = 24  # points per edge in the side-view slice


def joint_spec(cfg: LureConfig, table: StationTable):
    from luregen.geometry.joint import JointSpec, head_apex_radius

    j = cfg.joint or JointConfig()
    return JointSpec(
        x_joint=table.nose[0] + j.joint_frac * table.length,
        gap=j.gap_mm,
        angle_deg=j.angle_deg,
        head_apex_r=head_apex_radius(j.gap_mm, cfg.process.method, cfg.dfm.cnc.tools),
        pin_offset=j.pin_offset_mm,
        pin_bore_d=j.pin_bore_mm,
        hinge_eye_z=tuple(j.hinge_eye_z),
        eye_slot_w=j.eye_slot_w_mm,
        eye_slot_h=j.eye_slot_h_mm,
        line_tie_z=j.line_tie_z,
    )


def hardware_band_g(cfg: LureConfig) -> float:
    """Total hardware mass spread (max − min) over all items, qty included."""
    return sum(h.qty * (h.mass_g[1] - h.mass_g[0]) for h in cfg.hardware if h.mass_g)


def point_masses(cfg: LureConfig, section: str) -> list[PointMass]:
    return [
        PointMass(
            name=h.name or h.id,
            mass_g=(h.qty * h.mass_g[0], h.qty * h.mass_g[1]),
            position=(h.x_mm, 0.0, h.z_mm),
            density=float(h.density),
        )
        for h in cfg.hardware
        if h.section == section and h.mass_g is not None and h.density
    ]


def fin_slot_corner_r(cfg: LureConfig) -> float:
    """Slot corner radius: the configured one, at least the CNC pocket tool's on wood."""
    r = cfg.fin.slot.corner_r_mm
    if cfg.process.method == "cnc":
        from luregen.geometry.cnc_halves import pocket_corner_r

        r = max(r, pocket_corner_r(cfg))
    return r


def fin_report(cfg: LureConfig, fs, fin_solid, table, shape: str) -> dict[str, Any]:
    """The `geometry.fin` report block: fin, slot, screw and their checks."""
    from luregen.geometry import fin as fn

    f = cfg.fin
    sc = f.screw
    rho = float(f.density)
    return {
        "material": f.material,
        "density": rho,
        "volume_cm3": _r(fin_solid.volume / 1000.0, 4),
        "mass_g": _r(rho * fin_solid.volume / 1000.0),
        "centroid_mm": [_r(v) for v in _centroid(fin_solid)],
        "x_cut_mm": _r(fs.x_cut),
        "z_axis_mm": _r(fs.z_axis),
        "length_mm": fs.length,
        "root_height_mm": _r(fs.root_height),
        "span_mm": fs.span,
        "thickness_mm": fs.thickness,
        "fork_depth_mm": fs.fork_depth,
        "tail_end_mm": {"depth": _r(fs.tail_depth_at_cut), "width": _r(fs.tail_width_at_cut)},
        "slot": {
            "x_mm": [_r(fs.slot_x0), _r(fs.x_cut)],
            "z_mm": [_r(fs.z_axis - fs.slot_height / 2), _r(fs.z_axis + fs.slot_height / 2)],
            "width_mm": _r(fs.slot_width),
            "height_mm": _r(fs.slot_height),
            "depth_mm": fs.slot_depth,
            "corner_r_mm": _r(fs.slot_corner_r),
            "clearance_mm": fs.slot_clearance,
            "walls_mm": fn.slot_walls(fs, table, shape),
        },
        "tab": {
            "x_mm": [_r(fs.tab_x0), _r(fs.x_cut)],
            "height_mm": _r(fs.tab_height),
            "thickness_mm": fs.thickness,
        },
        "screw": {
            "id": sc.id,
            "name": sc.name,
            "x_mm": _r(fs.screw_x),
            "z_mm": _r(fs.screw_z),
            "pilot_d_mm": fs.pilot_d,
            "tab_hole_d_mm": fs.tab_hole_d,
            "length_mm": fs.screw_length,
            "body_width_mm": _r(fn.local_width(fs, table, shape)),
            "mass_g": list(sc.mass_g) if sc.mass_g else None,
            "measured": sc.measured,
        },
        "print": {"orientation": "flat", "note": "printed lying on its side, no supports"},
    }


def fin_point_mass(cfg: LureConfig, fs) -> PointMass:
    sc = cfg.fin.screw
    return PointMass(
        name=sc.name or sc.id,
        mass_g=(sc.mass_g[0], sc.mass_g[1]),
        position=(fs.screw_x, 0.0, fs.screw_z),
        density=float(sc.density),
    )


def _r(v: float, n: int = 3) -> float:
    return round(float(v), n)


def _centroid(solid) -> tuple[float, float, float]:
    from build123d import CenterOf

    c = solid.center(CenterOf.MASS)
    return (float(c.X), float(c.Y), float(c.Z))


def mesh_extents(solid) -> tuple:
    """(min, max) xyz of a fine mesh. OCCT bounding boxes of trimmed B-spline faces cover
    the untrimmed surface, so they overstate a clipped cavity's extent."""
    from luregen.geometry.export import tessellate

    v = tessellate(solid, tolerance=0.05, max_triangles=None).mesh.vertices
    return v.min(axis=0), v.max(axis=0)


def side_slice(solid) -> list[list[list[float]]]:
    """The solid cut by the y = 0 plane, as closed [x, z] polylines (outer and holes)."""
    import numpy as np
    from build123d import Face, Plane

    bb = solid.bounding_box()
    size = 2 * max(bb.size.X, bb.size.Z) + 20
    plane = Plane(origin=(bb.center().X, 0, bb.center().Z), x_dir=(1, 0, 0), z_dir=(0, -1, 0))
    loops = []
    for face in (solid & Face.make_rect(size, size, plane)).faces():
        for wire in [face.outer_wire(), *face.inner_wires()]:
            pts = []
            for edge in wire.order_edges():
                for p in edge.positions(np.linspace(0, 1, SLICE_SAMPLES)):
                    pts.append([round(p.X, 2), round(p.Z, 2)])
            loops.append(pts)
    return loops


class GlideFamily:
    name = "glide"

    def scaling_volumes(self, cfg: LureConfig, table: StationTable, envelope: Envelope) -> dict:
        """Displaced volume at the reference length as {cubic, area, const} mm³ for the
        weight → length step (`physics.scaling`): the envelope (minus the tail tip a fin
        replaces) scales with L³; the joint gap and the fin (fixed widths) with L²; the
        hardware (band mid) not at all."""
        from luregen.geometry import joint as jt

        env = envelope.solid
        cubic, area = float(env.volume), 0.0
        if cfg.joint is not None:
            area -= float(jt.gap_wedge(env, joint_spec(cfg, table)).volume)
        if cfg.fin is not None:
            from luregen.geometry import fin as fn

            tip, fin_v = fn.volumes(fn.fin_spec(cfg, table), env)
            cubic -= tip
            area += fin_v
        const = 0.0
        for h in cfg.hardware:
            if h.mass_g and h.density:
                const += h.qty * 1000.0 * 0.5 * (h.mass_g[0] + h.mass_g[1]) / h.density
        if cfg.fin is not None and cfg.fin.screw.mass_g and cfg.fin.screw.density:
            const += 1000.0 * 0.5 * sum(cfg.fin.screw.mass_g) / cfg.fin.screw.density
        return {"cubic": cubic, "area": area, "const": const}

    def build(
        self, cfg: LureConfig, table: StationTable, envelope: Envelope, emit: Emit
    ) -> FamilyBuild:
        from luregen.geometry import cavity as cav
        from luregen.geometry import joint as jt
        from luregen.geometry import pockets as pk
        from luregen.geometry.validate import assert_valid

        is_print = cfg.process.method == "print"
        shape = envelope.section_shape or cfg.profile.section_shape
        spec = joint_spec(cfg, table)
        env = envelope.solid
        bbox = env.bounding_box()
        rho_body = float(cfg.process.material_density)
        clearance = cfg.internals.feature_clearance_mm
        warnings: list[str] = []

        def wall(section: str) -> float:
            if is_print:
                return getattr(cfg.internals, section).wall_thk_mm
            return cfg.dfm.cnc.min_wood_wall_mm

        # 1. pockets on the full body ---------------------------------------------------
        emit("joint", 0.0, "ballast and trim pockets")
        ballast_g = cfg.ballast.mass_g or cfg.internals.ballast_seed_g
        rho_ballast = float(cfg.ballast.density)
        x_ballast = spec.x_joint - cfg.ballast.x_from_joint_mm
        if is_print:
            ballast = pk.ballast_pocket(
                env,
                table,
                shape,
                x_ballast,
                cfg.ballast.z_above_belly_mm,
                1000.0 * ballast_g / rho_ballast,
                wall("head"),
            )
        else:  # CNC halves: a closed glue-face pocket, ballast set in before glue-up (#40)
            from luregen.geometry import cnc_halves as ch

            ballast = ch.ballast_pocket(cfg, table, shape, spec, x_ballast, ballast_g / rho_ballast)
        ballast_fill = (env & ballast.solid()).solids()[0]
        body = env
        sealed: dict[str, list] = {"head": [], "tail": []}  # CNC pockets, cut after the split
        if is_print:
            body = (env - ballast.solid()).solids()[0]
            assert_valid(body, solids=1, shells=1, what="body after ballast pocket")
        else:
            sealed["head"].append(ballast)
            for pocket in ch.air_pockets(cfg, table, shape, spec):
                sealed[pocket.kind.split(":")[1]].append(pocket)

        trim = None
        trim_report: dict[str, Any] | None = None
        if cfg.buoyancy.trim_pocket:
            rho_trim = float(cfg.internals.trim_density or rho_ballast)
            capacity_g = cfg.internals.trim_capacity_g or hardware_band_g(cfg) + 1.0
            x_trim = table.tail[0] - cfg.internals.trim_x_from_tail_mm
            try:
                trim = pk.trim_pocket(
                    env, table, shape, x_trim, 1000.0 * capacity_g / rho_trim, wall("tail")
                )
            except pk.PocketError as exc:
                warnings.append(f"trim pocket skipped: {exc}")
        if trim is not None:
            trim_volume = (env & trim.solid()).volume
            body = (body - trim.solid()).solids()[0]
            assert_valid(body, solids=1, shells=1, what="body after trim pocket")
            capacity = rho_trim * trim_volume / 1000.0
            fill_g = min(cfg.internals.trim_fill_g or 0.0, capacity)
            if (cfg.internals.trim_fill_g or 0.0) > capacity + 1e-6:
                warnings.append(
                    f"trim fill {cfg.internals.trim_fill_g} g exceeds the pocket capacity "
                    f"{capacity:.2f} g; clamped"
                )
            trim_report = {
                "section": "tail",
                "x_mm": _r(trim.x),
                "r_mm": _r(trim.r),
                "z_top_mm": _r(trim.z_top),
                "volume_cm3": _r(trim_volume / 1000.0, 4),
                "capacity_g": _r(capacity),
                "material": cfg.internals.trim_material,
                "density": rho_trim,
                "fill_g": _r(fill_g),
                "note": "nominal (band-mid) putty fill; the solver report sizes the band-end "
                "corrections",
            }

        # tail fin: cut the tail tip, the fin slot and the screw hole on the full body
        fin_spec = fin = None
        if cfg.fin is not None:
            from luregen.geometry import fin as fn

            emit("joint", 0.3, "tail fin slot")
            fin_spec = fn.fin_spec(cfg, table, fin_slot_corner_r(cfg))
            fn.check_spec(fin_spec, spec.tail_apex_x)
            body = fn.cut_tail_end(body, fin_spec)
            fin = fn.fin_solid(fin_spec)

        # 2. joint: pin bore, split, eye slots --------------------------------------------
        emit("joint", 0.5, "V-joint split")
        split = jt.split_body(body, spec)
        line_tie = jt.line_tie_location(table, spec.line_tie_z)
        hinge_eyes = jt.hinge_eye_locations(spec)

        # 3. internals ----------------------------------------------------------------------
        sections = {"head": split.head, "tail": split.tail}
        for name, pockets in sealed.items():
            for i, pocket in enumerate(pockets):
                cut = (sections[name] - pocket.solid()).solids()[0]
                assert_valid(cut, solids=1, shells=2 + i, what=f"{name} {pocket.kind} pocket")
                sections[name] = cut
        internals: dict[str, Any] = {}
        cavities: dict[str, Any] = {}
        if is_print:
            min_skin_rule = cfg.dfm.print.min_skin_to_cavity_mm
            roof = cfg.dfm.print.cavity_roof
            bosses = {
                "head": [line_tie, *hinge_eyes]
                + [
                    (h.x_mm, h.z_mm)
                    for h in cfg.hardware
                    if h.section == "head" and h.id.startswith("screw_eye")
                ],
                "tail": [
                    (h.x_mm, h.z_mm)
                    for h in cfg.hardware
                    if h.section == "tail" and h.id.startswith("screw_eye")
                ],
            }
            for i, name in enumerate(SECTIONS):
                emit("internals", i / len(SECTIONS), f"{name} cavity")
                knobs = getattr(cfg.internals, name)
                t = knobs.wall_thk_mm
                if t < cfg.dfm.print.min_wall_mm:
                    warnings.append(
                        f"{name}: wall_thk_mm {t} < dfm.print.min_wall_mm "
                        f"{cfg.dfm.print.min_wall_mm}"
                    )
                grow = max(t, clearance)
                if name == "head":
                    keep = jt.shifted_head_prism(spec, bbox, t)
                    keepouts = [ballast.solid(grow)]
                else:
                    keep = jt.shifted_tail_prism(spec, bbox, t)
                    keepouts = [jt.pin_bore(spec, bbox, grow), *jt.eye_slots(spec, grow)]
                    if trim is not None:
                        keepouts.append(trim.solid(grow))
                    if fin_spec is not None:
                        keepouts += fn.keepouts(fin_spec, bbox, grow)
                r_boss = cfg.internals.boss_r_mm
                keepouts += [pk.boss(x, z, r_boss + clearance) for x, z in bosses[name]]
                loft = cav.inset_loft(table, t, shape, roof)
                cavity, dropped = cav.clip_cavity(
                    loft, [cav.keel_box(bbox, knobs.keel_top_z_mm), keep], keepouts, name
                )
                skin = cav.min_skin(sections[name], cavity)
                if skin < min_skin_rule:
                    warnings.append(
                        f"{name}: cavity skin {skin:.2f} mm < dfm.print.min_skin_to_cavity_mm "
                        f"{min_skin_rule}"
                    )
                sections[name] = cav.hollow(sections[name], cavity, name)
                cavities[name] = cavity
                lo, hi = mesh_extents(cavity)
                internals[name] = {
                    "wall_thk_mm": t,
                    "keel_top_z_mm": knobs.keel_top_z_mm,
                    "roof": roof,
                    "cavity_volume_cm3": _r(cavity.volume / 1000.0, 4),
                    "cavity_x_mm": [_r(lo[0], 2), _r(hi[0], 2)],
                    "cavity_z_mm": [_r(lo[2], 2), _r(hi[2], 2)],
                    "cavity_ceiling_z_mm": _r(hi[2], 2),
                    "min_skin_mm": _r(skin),
                    "dropped_cavity_mm3": _r(dropped, 1),
                    "bosses": [[_r(x, 2), _r(z, 2)] for x, z in bosses[name]],
                    "boss_r_mm": r_boss,
                }
        for name, solid in sections.items():
            shells = 2 if is_print else 1 + len(sealed[name])
            assert_valid(solid, solids=1, shells=shells, what=f"{name} section")
        emit("internals", 1.0, "sections valid")

        # mass-property inputs --------------------------------------------------------------
        mass_inputs = []
        for name, solid in sections.items():
            parts = [Part("body", solid.volume, _centroid(solid), rho_body)]
            if name in cavities:
                c = cavities[name]
                parts.append(Part("cavity", c.volume, _centroid(c), 0.0))
            if name == "head":
                parts.append(
                    Part("ballast", ballast_fill.volume, _centroid(ballast_fill), rho_ballast)
                )
            if name == "tail" and trim_report and trim_report["fill_g"] > 0:
                from luregen.physics.buoyancy import TrimSpec

                parts.append(TrimSpec.from_report(trim_report).part(trim_report["fill_g"]))
            hardware = point_masses(cfg, name)
            if name == "tail" and fin is not None:
                parts.append(Part("fin", fin.volume, _centroid(fin), float(cfg.fin.density)))
                hardware.append(fin_point_mass(cfg, fin_spec))
            mass_inputs.append(SectionInput(name, parts, hardware))

        report = {
            "joint": {
                "x_joint_mm": _r(spec.x_joint),
                "gap_mm": spec.gap,
                "angle_deg": spec.angle_deg,
                "head_apex_x_mm": _r(spec.head_apex_x),
                "tail_apex_x_mm": _r(spec.tail_apex_x),
                "head_apex_r_mm": _r(spec.head_apex_r),
                "tail_notch_r_mm": _r(spec.tail_notch_r),
                "pin_x_mm": _r(spec.pin_x),
                "pin_bore_mm": spec.pin_bore_d,
                "hinge_eyes_mm": [[_r(x, 2), _r(z, 2)] for x, z in hinge_eyes],
                "line_tie_mm": [_r(line_tie[0], 2), _r(line_tie[1], 2)],
                "gap_volume_cm3": _r(split.gap_volume / 1000.0, 4),
                "feature_volume_cm3": _r(split.feature_volume / 1000.0, 4),
            },
            "ballast": {
                "section": "head",
                "material": cfg.ballast.material,
                "density": rho_ballast,
                "mass_g": _r(rho_ballast * ballast_fill.volume / 1000.0),
                "pinned": cfg.ballast.mass_g is not None
                and "ballast.mass_g" not in cfg.solver.solved_knobs,
                "x_mm": _r(ballast.x),
                "z_axis_mm": _r(ballast.z_axis),
                "r_mm": _r(ballast.r),
                "length_mm": _r(ballast.length),
                "volume_cm3": _r(ballast_fill.volume / 1000.0, 4),
                "centroid_mm": [_r(v) for v in _centroid(ballast_fill)],
                **({} if is_print else {"glue_face_pocket": ballast.as_dict()}),
            },
            **(
                {}
                if is_print
                else {
                    "cnc_air_pockets": [p.as_dict() for p in [*sealed["head"][1:], *sealed["tail"]]]
                }
            ),
            "trim_pocket": trim_report,
            "fin": None if fin is None else fin_report(cfg, fin_spec, fin, table, shape),
            "internals": internals or None,
            "hardware_band_g": _r(hardware_band_g(cfg)),
        }
        diagram = {
            "x_joint": spec.x_joint,
            "slices": {
                name: side_slice(solid)
                for name, solid in {**sections, **({"fin": fin} if fin is not None else {})}.items()
            },
            "fills": {"ballast": side_slice(ballast_fill)},
            "keel_top_z": {n: getattr(cfg.internals, n).keel_top_z_mm for n in SECTIONS}
            if is_print
            else {},
            "hardware": [
                {"name": h.name or h.id, "x": h.x_mm, "z": h.z_mm, "section": h.section}
                for h in cfg.hardware
            ],
        }
        parts_out = {"fin": fin} if fin is not None else {}
        return FamilyBuild(sections, mass_inputs, report, diagram, warnings, parts_out)


FAMILY = GlideFamily()
