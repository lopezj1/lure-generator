"""Mass properties against analytic values: an ellipsoid with a known void and point masses."""

import math

import pytest
from build123d import CenterOf, Pos, Sphere, scale

from luregen.physics.massprops import (
    Part,
    PointMass,
    SectionInput,
    band_props,
    combine,
    lure_props,
    section_props,
)

A, B, C = 40.0, 10.0, 15.0  # ellipsoid semi-axes (x, y, z), centred at (A, 0, 0)
R_VOID, VOID_AT = 5.0, (30.0, 0.0, 4.0)
RHO = 1.27


def ellipsoid_volume(a, b, c):
    return 4 / 3 * math.pi * a * b * c


def sphere_volume(r):
    return 4 / 3 * math.pi * r**3


def analytic_section(hw=()):
    v_env = ellipsoid_volume(A, B, C)
    v_void = sphere_volume(R_VOID)
    v_body = v_env - v_void
    # body centroid: envelope moment minus the void's moment
    pairs = zip((A, 0, 0), VOID_AT, strict=True)
    body_c = tuple((v_env * e - v_void * v) / v_body for e, v in pairs)
    parts = [Part("body", v_body, body_c, RHO), Part("cavity", v_void, VOID_AT, 0.0)]
    return SectionInput("head", parts, list(hw)), v_env, v_body


def test_section_with_void_matches_analytic():
    section, v_env, v_body = analytic_section()
    p = section_props(section)
    assert p.mass_g == pytest.approx(RHO * v_body / 1000)
    assert p.volume_mm3 == pytest.approx(v_env)  # the sealed void displaces
    assert p.cb == pytest.approx((A, 0, 0))  # CB = centroid of the envelope
    # CG moves away from the void (behind it in x, below it in z)
    assert p.cg[0] > A and p.cg[2] < 0
    expected_cg_x = (v_env * A - sphere_volume(R_VOID) * VOID_AT[0]) / v_body
    assert p.cg[0] == pytest.approx(expected_cg_x)
    assert p.density == pytest.approx(1000 * p.mass_g / v_env)


def test_point_masses_add_mass_and_displacement():
    hook = PointMass("hook", (1.0, 2.0), (60.0, 0.0, -20.0), density=8.0)
    section, v_env, v_body = analytic_section([hook])
    p = section_props(section, "max")
    m_body = RHO * v_body / 1000
    assert p.mass_g == pytest.approx(m_body + 2.0)
    v_hook = 1000 * 2.0 / 8.0
    assert p.volume_mm3 == pytest.approx(v_env + v_hook)
    assert p.cb[0] == pytest.approx((v_env * A + v_hook * 60.0) / (v_env + v_hook))
    lo = section_props(section, "min")
    mid = section_props(section, "mid")
    assert lo.mass_g < mid.mass_g < p.mass_g
    assert mid.mass_g == pytest.approx(m_body + 1.5)


def test_combine_and_ratio():
    head, *_ = analytic_section()
    tail = SectionInput("tail", [Part("body", 1000.0, (120.0, 0, 2.0), 0.8)])
    lp = lure_props([head, tail])
    h, t = lp.sections["head"], lp.sections["tail"]
    assert lp.lure.mass_g == pytest.approx(h.mass_g + t.mass_g)
    assert lp.lure.volume_mm3 == pytest.approx(h.volume_mm3 + t.volume_mm3)
    assert lp.ratio() == pytest.approx(h.mass_g / t.mass_g)
    assert lp.lure.cg[0] == pytest.approx(
        (h.mass_g * h.cg[0] + t.mass_g * t.cg[0]) / (h.mass_g + t.mass_g)
    )
    assert combine([h]).cb == pytest.approx(h.cb)
    d = lp.as_dict()
    assert d["lure"]["head_tail_mass_ratio"] == pytest.approx(lp.ratio(), abs=1e-4)
    assert set(band_props([head, tail])) == {"min", "mid", "max"}


def test_occt_ellipsoid_with_void_matches_analytic():
    """Measure real solids the way the glide family does and compare with the analytic case."""
    env = Pos(A, 0, 0) * scale(Sphere(1.0), (A, B, C))
    void = Pos(*VOID_AT) * Sphere(R_VOID)
    body = (env - void).solids()[0]
    assert len(body.shells()) == 2

    def c(s):
        v = s.center(CenterOf.MASS)
        return (v.X, v.Y, v.Z)

    measured = SectionInput(
        "head",
        [Part("body", body.volume, c(body), RHO), Part("cavity", void.volume, c(void), 0.0)],
    )
    exact, *_ = analytic_section()
    m, e = section_props(measured), section_props(exact)
    assert m.mass_g == pytest.approx(e.mass_g, rel=1e-3)
    assert m.volume_mm3 == pytest.approx(e.volume_mm3, rel=1e-3)
    assert m.cg == pytest.approx(e.cg, abs=0.01)
    assert m.cb == pytest.approx(e.cb, abs=0.01)
