"""DFM rule registry (F5): one failing-case fixture per rule, plus registry behaviour.

Fixtures are synthetic: boxes as sections (a void is an inverted inner box), hand-written
report geometry, and the sample variant's config with the process switched as needed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import trimesh

from luregen.config import load_config
from luregen.dfm import REGISTRY, Violation, evaluate, load_rules
from luregen.dfm.mesh import closest_points, first_hit, split_shells
from luregen.dfm.model import DfmModel
from luregen.dfm.registry import Rule, run_rule
from luregen.dfm.report import dfm_md

SAMPLE = Path(__file__).resolve().parents[2] / "spec/designs/glide/variants/striper_175_petg.toml"
CNC = ["process.method=cnc", "process.material=basswood", "process.rigging=through_wire"]

load_rules()


@pytest.fixture(scope="module")
def print_cfg():
    return load_config(SAMPLE)


@pytest.fixture(scope="module")
def cnc_cfg():
    return load_config(SAMPLE, CNC)


def box(size, center=(0, 0, 0)) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=size)
    m.apply_translation(center)
    return m


def hollow(outer, *voids) -> trimesh.Trimesh:
    """A section mesh: outer box plus each void as an inverted inner box."""
    parts = [outer]
    for v in voids:
        v = v.copy()
        v.invert()
        parts.append(v)
    return trimesh.util.concatenate(parts)


def internals(wall=2.0, skin=1.5) -> dict:
    return {"wall_thk_mm": wall, "min_skin_mm": skin}


# Section 40 × 20 × 30 mm centred at (20, 0, 0): x 0–40, y ±10, z ±15.
OUTER = ((40, 20, 30), (20, 0, 0))
# A tall narrow void (steep walls, 4 mm ceiling span = a short bridge).
NARROW = ((20, 4, 10), (20, 0, 2))


def print_model(*voids, geometry=None) -> DfmModel:
    mesh = hollow(box(*OUTER), *(box(*v) for v in voids))
    geo = {"internals": {"head": internals()}} if geometry is None else geometry
    return DfmModel(geo, {}, {"head": mesh})


def with_eye(cfg, x, z, section="head"):
    from luregen.config.models import HardwareItem

    cfg = cfg.model_copy(deep=True)
    cfg.hardware = [
        HardwareItem(
            id="screw_eye_210",
            name="eye",
            section=section,
            x_mm=x,
            z_mm=z,
            mass_g=(0.3, 0.3),
            material="stainless",
            density=8.0,
        )
    ]
    return cfg


def pocket(x=20.0, z=0.0, length=10.0, height=6.0, depth=1.5, corner_r=1.84, kind="ballast"):
    return {
        "x": x,
        "z": z,
        "length": length,
        "height": height,
        "depth": depth,
        "corner_r": corner_r,
        "kind": kind,
    }


def cnc_model(voids=(), pockets=(), body=None, geometry=None) -> DfmModel:
    mesh = hollow(box(*OUTER), *(box(*v) for v in voids))
    geo = {"joint": {"tail_notch_r_mm": 2.3, "pin_bore_mm": 1.8, "hinge_eyes_mm": []}}
    if pockets:
        geo["ballast"] = {"section": "head", "glue_face_pocket": pockets[0]}
        geo["cnc_air_pockets"] = list(pockets[1:])
    geo.update(geometry or {})
    meshes = {"head": mesh, "body": body if body is not None else box(*OUTER)}
    return DfmModel(geo, {}, meshes)


def pocket_void(p) -> tuple:
    return ((p["length"], 2 * p["depth"], p["height"]), (p["x"], 0, p["z"]))


# --- one failing case per rule ------------------------------------------------------------
# Each returns (model, cfg) that must violate the rule.


def fail_single_void(cfg, _):
    return print_model(NARROW, ((4, 4, 4), (5, 0, 0))), cfg


def fail_print_min_wall(cfg, _):
    return print_model(NARROW, geometry={"internals": {"head": internals(wall=1.0)}}), cfg


def fail_overhang(cfg, _):
    return print_model(((20, 12, 10), (20, 0, 2))), cfg  # flat 12 mm wide ceiling


def fail_screw_eye_solid(cfg, _):
    # void floor 3 mm above the belly (z −15): the eye at the belly has 3 mm of solid
    return print_model(((20, 12, 10), (20, 0, -7))), with_eye(cfg, 20, -15)


def fail_boss_layers(cfg, _):
    cfg = with_eye(cfg, 20, -15)
    cfg.dfm.print.boss_min_layers = 60  # 12 mm at 0.2 mm; the floor gives ~8 mm
    return print_model(((20, 4, 10), (20, 0, -2))), cfg


def fail_no_internal_voids(_, cfg):
    return cnc_model(voids=[((6, 3, 6), (20, 4, 0))]), cfg  # off the glue face


def fail_cnc_min_wall(_, cfg):
    p = pocket(z=10.0, height=6.0)  # top at z 13, 2 mm under the top surface
    return cnc_model(voids=[pocket_void(p)], pockets=[p]), cfg


def fail_corner_radius(_, cfg):
    return cnc_model(geometry={"joint": {"tail_notch_r_mm": 1.0}}), cfg


def fail_pocket_depth(_, cfg):
    p = pocket(depth=12.0)
    return cnc_model(pockets=[p]), cfg


def shelf_body() -> trimesh.Trimesh:
    """A half (y > 0) with a shelf over empty space: faces under the shelf look at −y."""
    return trimesh.util.concatenate([box((10, 5, 10), (5, 2.5, 0)), box((10, 2, 10), (15, 4, 0))])


def fail_undercuts(_, cfg):
    return cnc_model(body=shelf_body()), cfg


def fail_axial_holes(_, cfg):
    hole = {"name": "wire bore", "axis": "x", "y_mm": 2.0}
    return cnc_model(geometry={"holes": [hole]}), cfg


def fail_roundover(_, cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.process.body_style = "profile25d"
    ro = {
        "requested_mm": 6.0,
        "applied_mm": 4.0,
        "limited_by": "max_fillet",
        "min_curvature_radius_mm": 3.0,
    }
    model = cnc_model()
    model.envelope = {"roundover": ro}
    return model, cfg


def fail_through_wire(_, cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.process.rigging = "screw_eye"
    return cnc_model(), cfg


FAILING: dict[str, Callable] = {
    "print.single_void": fail_single_void,
    "print.min_wall": fail_print_min_wall,
    "print.cavity_overhang": fail_overhang,
    "print.screw_eye_solid": fail_screw_eye_solid,
    "print.boss_layers": fail_boss_layers,
    "cnc.no_internal_voids": fail_no_internal_voids,
    "cnc.min_wall": fail_cnc_min_wall,
    "cnc.corner_radius": fail_corner_radius,
    "cnc.pocket_depth": fail_pocket_depth,
    "cnc.no_undercuts": fail_undercuts,
    "cnc.no_axial_holes": fail_axial_holes,
    "cnc.roundover": fail_roundover,
    "cnc.through_wire": fail_through_wire,
}


def test_every_rule_has_a_failing_case():
    assert set(FAILING) == set(REGISTRY)


@pytest.mark.parametrize("rule_id", sorted(FAILING))
def test_rule_fails_on_its_fixture(rule_id, print_cfg, cnc_cfg):
    model, cfg = FAILING[rule_id](print_cfg, cnc_cfg)
    rule = REGISTRY[rule_id]
    assert rule.applies(cfg)
    found = run_rule(rule, model, cfg)
    assert found, f"{rule_id} found nothing"
    assert all(v.rule == rule_id and v.severity == rule.severity for v in found)
    assert not any("could not be evaluated" in v.message for v in found), found
    # the whole registry reports it too, with the right status
    result = evaluate(model, cfg, only=[rule_id])
    assert result.status == ("fail" if rule.severity == "error" else "warn")


# --- passing counterparts -----------------------------------------------------------------


def test_print_rules_pass_on_a_good_section(print_cfg):
    cfg = with_eye(print_cfg, 20, -15)  # floor at z −3: 12 mm of solid above the belly eye
    result = evaluate(print_model(NARROW), cfg)
    assert result.status == "pass", result.violations
    assert set(result.checked) == {r for r in REGISTRY if r.startswith("print.")}


def test_cnc_rules_pass_on_a_good_section(cnc_cfg):
    p = pocket()
    result = evaluate(cnc_model(voids=[pocket_void(p)], pockets=[p]), cnc_cfg)
    assert result.status == "pass", result.violations
    assert "cnc.roundover" not in result.checked  # loft3d: no roundover rule


def test_undeclared_glue_face_void_is_an_internal_void(cnc_cfg):
    p = pocket()
    found = run_rule(REGISTRY["cnc.no_internal_voids"], cnc_model(voids=[pocket_void(p)]), cnc_cfg)
    assert found and "not a declared glue-face pocket" in found[0].message


def test_overhang_bridge_and_angle(print_cfg):
    # A 4 mm wide flat ceiling bridges; at 7 mm the max_bridge_mm threshold decides.
    rule = REGISTRY["print.cavity_overhang"]
    assert not run_rule(rule, print_model(NARROW), print_cfg)
    wide = print_model(((20, 7, 10), (20, 0, 2)))
    assert not run_rule(rule, wide, print_cfg)
    cfg = print_cfg.model_copy(deep=True)
    cfg.dfm.print.max_bridge_mm = 5.0
    assert run_rule(rule, wide, cfg)


def test_eye_too_close_to_a_void_is_an_error(print_cfg):
    # 6 mm of solid along the thread, but the void wall runs 1 mm beside it
    cfg = with_eye(print_cfg, 20, -15)
    model = print_model(((20, 4, 20), (23.6, 0, 1)))
    found = run_rule(REGISTRY["print.screw_eye_solid"], model, cfg)
    assert found and "from a void" in found[0].message


def test_corner_radius_checks_pocket_corners(cnc_cfg):
    found = run_rule(
        REGISTRY["cnc.corner_radius"], cnc_model(pockets=[pocket(corner_r=0.5)]), cnc_cfg
    )
    assert [v.section for v in found] == ["head"]


# --- registry -------------------------------------------------------------------------------


def test_rule_tags(print_cfg, cnc_cfg):
    r = Rule("x", "error", "x", lambda m, c: [], process=("cnc",), body_style=("profile25d",))
    assert not r.applies(print_cfg) and not r.applies(cnc_cfg)
    prof = cnc_cfg.model_copy(deep=True)
    prof.process.body_style = "profile25d"
    assert r.applies(prof)
    assert not Rule("y", "warn", "y", lambda m, c: [], family=("other",)).applies(print_cfg)


def test_a_crashing_rule_is_reported_as_an_error(print_cfg):
    def boom(model, cfg):
        raise KeyError("geometry")

    found = run_rule(Rule("t.boom", "warn", "boom", boom), DfmModel({}), print_cfg)
    assert found[0].severity == "warn" and "could not be evaluated" in found[0].message


def test_violation_severity_override_and_report(print_cfg):
    r = Rule(
        "t.mix", "error", "mix", lambda m, c: [Violation("a"), Violation("b", severity="warn")]
    )
    found = run_rule(r, DfmModel({}), print_cfg)
    assert [v.severity for v in found] == ["error", "warn"]
    model, cfg = fail_overhang(print_cfg, None)
    block = evaluate(model, cfg).as_dict()
    assert block["status"] == "fail" and block["n_errors"] >= 1
    md = "\n".join(dfm_md(block))
    assert "## DFM" in md and "print.cavity_overhang" in md


# --- mesh helpers ---------------------------------------------------------------------------


def test_mesh_queries():
    m = box((10, 10, 10))
    _, d, _ = closest_points(m, [[0, 0, 9], [0, 0, 0]])
    assert d == pytest.approx([4.0, 5.0])
    assert first_hit(m, [0, 0, -20], [0, 0, 1]) == pytest.approx(15.0)
    assert first_hit(m, [0, 0, 20], [0, 0, 1]) == np.inf
    shells = split_shells(hollow(box((10, 10, 10)), box((4, 4, 4))))
    assert len(shells.inners) == 1
    # inner normals point into the material: the void's top face looks up
    top = shells.inners[0].face_normals[shells.inners[0].triangles_center[:, 2] > 1.9]
    assert np.allclose(top[:, 2], 1.0)
