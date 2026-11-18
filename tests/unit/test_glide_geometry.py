"""Glide geometry: V-joint split, inset-loft cavities, pockets and bosses."""

import math
from pathlib import Path

import numpy as np
import pytest
from build123d import Box, Pos

from luregen.geometry import cavity as cav
from luregen.geometry import joint as jt
from luregen.geometry import pockets as pk
from luregen.geometry.envelope import loft3d, section_profile
from luregen.geometry.validate import assert_valid
from luregen.profile.table import StationTable

REPO = Path(__file__).resolve().parents[2]
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"


def spec(apex_r=0.0, **kw):
    base = dict(
        x_joint=87.5,
        gap=2.3,
        angle_deg=37.0,
        head_apex_r=apex_r,
        pin_offset=3.0,
        pin_bore_d=1.8,
        hinge_eye_z=(-12.0, 6.0),
        eye_slot_w=2.5,
        eye_slot_h=6.0,
        line_tie_z=-4.0,
    )
    return jt.JointSpec(**{**base, **kw})


@pytest.fixture(scope="module")
def table() -> StationTable:
    return StationTable.load_csv(PRESET)


@pytest.fixture(scope="module")
def env(table):
    return loft3d(table)


@pytest.fixture(scope="module")
def split(env):
    return jt.split_body(env, spec())


# --- joint ----------------------------------------------------------------------------


def test_split_volumes_sum_to_envelope_minus_gap(env, split, table):
    assert_valid(split.head, solids=1, shells=1)
    assert_valid(split.tail, solids=1, shells=1)
    total = split.head.volume + split.tail.volume + split.gap_volume + split.feature_volume
    assert total == pytest.approx(env.volume, rel=1e-6)
    # analytic gap wedge: joint cross-section area × x-thickness gap / cos(angle)
    i = int(np.argmin(np.abs(table.x - 87.5)))
    area = math.pi * table.half_width[i] * table.v_radius[i]
    wedge = area * 2.3 / math.cos(math.radians(37.0))
    assert split.gap_volume == pytest.approx(wedge, rel=0.03)
    hb, tb = split.head.bounding_box(), split.tail.bounding_box()
    assert pytest.approx(spec().head_apex_x, abs=1e-3) == hb.max.X
    assert spec().tail_apex_x > tb.min.X  # notch arms reach forward of the apex


def test_cnc_apex_radius_rule():
    assert jt.smallest_tool_radius(["flat_6.35", "flat_3.175", "ball_3.175"]) == 1.5875
    assert jt.head_apex_radius(2.3, "print", ["flat_3.175"]) == 0.0
    assert jt.head_apex_radius(2.3, "cnc", ["flat_3.175"]) == 0.0  # notch r = gap ≥ tool r
    r = jt.head_apex_radius(1.5, "cnc", ["flat_6.35"])
    assert r == pytest.approx(3.175 - 1.5)
    assert spec(apex_r=r, gap=1.5).tail_notch_r == pytest.approx(3.175)


def test_rounded_apex_split_is_valid(env):
    s = jt.split_body(env, spec(apex_r=1.0))
    assert_valid(s.head, solids=1, shells=1)
    assert_valid(s.tail, solids=1, shells=1)


def test_joint_locations(table):
    x, z = jt.line_tie_location(table, -4.0)
    assert z == -4.0 and 0 < x < 3
    xa = spec().head_apex_x
    assert jt.hinge_eye_locations(spec()) == [(xa, -12.0), (xa, 6.0)]


# --- cavity ---------------------------------------------------------------------------


@pytest.mark.parametrize("roof", ["gable45", "teardrop"])
def test_roof_section_is_self_supporting_and_inside(roof):
    unit = section_profile("ellipse", cav.CAVITY_SAMPLES)
    hw, v = 6.0, 20.0
    pts = cav.roof_section(unit, hw, v, roof)
    assert pts.shape == unit.shape
    assert pts[0] == pytest.approx([0.0, v])  # seam stays at the top
    ell = (pts[:, 0] / hw) ** 2 + (pts[:, 1] / v) ** 2
    assert ell.max() <= 1 + 1e-9  # never outside the inset section
    upper = pts[pts[:, 1] > 0.5 * v]
    if roof == "gable45":
        assert (upper[:, 1] + np.abs(upper[:, 0]) <= v + 1e-9).all()
    # roof slope ≥ 45° near the top: |dz/dy| ≥ 1 between neighbouring top samples
    top = pts[(pts[:, 1] > 0.8 * v) & (pts[:, 0] > 0)]
    top = top[np.argsort(top[:, 0])]
    slope = np.abs(np.diff(top[:, 1]) / np.diff(top[:, 0]))
    assert (slope >= 1 - 1e-6).all()


def _cavity_volume(table, env, wall, keel):
    loft = cav.inset_loft(table, wall)
    bbox = env.bounding_box()
    keep = [cav.keel_box(bbox, keel), jt.shifted_head_prism(spec(), bbox, wall)]
    cavity, _ = cav.clip_cavity(loft, keep, [], "head")
    return cavity


def test_cavity_volume_monotonic_in_knobs(table, env):
    v = {w: _cavity_volume(table, env, w, -8.0).volume for w in (1.6, 2.4)}
    assert v[1.6] > v[2.4]
    k = {z: _cavity_volume(table, env, 2.0, z).volume for z in (-12.0, -4.0)}
    assert k[-12.0] > k[-4.0]


def test_hollow_head_is_one_solid_two_shells(table, env, split):
    cavity = _cavity_volume(table, env, 2.0, -8.0)
    skin = cav.min_skin(split.head, cavity)
    assert skin > 1.2
    hollowed = cav.hollow(split.head, cavity, "head")
    assert_valid(hollowed, solids=1, shells=2)
    assert hollowed.volume == pytest.approx(split.head.volume - cavity.volume, rel=1e-5)


def test_keepout_splitting_the_cavity_keeps_the_largest(table, env):
    loft = cav.inset_loft(table, 2.0)
    bbox = env.bounding_box()
    slab = Pos(60, 0, 0) * Box(4, 60, 80)  # splits the cavity in two
    cavity, dropped = cav.clip_cavity(loft, [cav.keel_box(bbox, -8.0)], [slab], "t")
    assert dropped > 0 and cavity.volume > dropped
    assert_valid(cavity, solids=1, shells=1)
    with pytest.raises(cav.CavityError, match="float"):
        cav.clip_cavity(loft, [], [pk.boss(80.0, 0.0, 1.0)], "t")


# --- pockets --------------------------------------------------------------------------


def test_ballast_pocket_holds_the_ballast_volume(table, env):
    target = 1000 * 8.0 / 18.0
    p = pk.ballast_pocket(env, table, "ellipse", 65.5, 6.0, target, 2.0)
    inside = (env & p.solid()).volume
    assert inside == pytest.approx(target, rel=0.01)
    assert p.z_axis == pytest.approx(pk.belly_z(table, 65.5) + 6.0)
    # side skin at the axis height ≥ wall
    assert pk.half_width_at(table, "ellipse", 65.5, p.z_axis) - p.r >= 2.0 - 1e-9
    body = (env - p.solid()).solids()[0]
    assert_valid(body, solids=1, shells=1)  # open pocket: no extra void


def test_trim_pocket_capacity(table, env):
    target = 235.0
    t = pk.trim_pocket(env, table, "ellipse", 140.0, target, 2.0)
    assert (env & t.solid()).volume == pytest.approx(target, rel=0.01)
    with pytest.raises(pk.PocketError):
        pk.trim_pocket(env, table, "ellipse", 140.0, target, 6.0)
