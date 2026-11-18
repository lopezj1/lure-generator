"""Envelope geometry against analytic values (ellipsoid loft, elliptic 2.5D slab)."""

from pathlib import Path

import numpy as np
import pytest
from build123d import CenterOf

from luregen.geometry.envelope import (
    EnvelopeError,
    build_envelope,
    check_section_seams,
    loft3d,
    mass_properties,
    profile25d,
    section_profile,
    section_wire,
)
from luregen.profile.table import StationTable

REPO = Path(__file__).resolve().parents[2]
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"


def ellipsoid_table(a=60.0, b=15.0, c=6.0, z0=3.0, n=23, half_width=None) -> StationTable:
    """Stations of an ellipsoid with semi-axes a (x), b (z), c (y), centered at (a, 0, z0)."""
    theta = np.linspace(0, np.pi, n + 2)[1:-1]  # cosine spacing: dense near the tips
    x = a * (1 - np.cos(theta))
    r = np.sqrt(1 - ((x - a) / a) ** 2)
    hw = c * r if half_width is None else np.full(n, half_width)
    return StationTable((0.0, z0), (2 * a, z0), x, np.full(n, z0), b * r, hw)


@pytest.fixture(scope="module")
def preset() -> StationTable:
    return StationTable.load_csv(PRESET)


def test_ellipsoid_loft_volume_and_centroid():
    a, b, c, z0 = 60.0, 15.0, 6.0, 3.0
    solid = loft3d(ellipsoid_table(a, b, c, z0), "ellipse")
    exact = 4 / 3 * np.pi * a * b * c
    assert solid.volume == pytest.approx(exact, rel=0.02)
    assert solid.volume == pytest.approx(exact, rel=0.002)  # actual accuracy is much better
    cg = solid.center(CenterOf.MASS)
    centroid = (cg.X, cg.Y, cg.Z)
    assert centroid == pytest.approx((a, 0.0, z0), abs=0.02 * a)
    assert centroid == pytest.approx((a, 0.0, z0), abs=0.05)


@pytest.mark.parametrize("shape", ["ellipse", "egg", "teardrop"])
@pytest.mark.parametrize("n", [8, 30])
def test_loft_valid_for_station_counts(shape, n):
    solid = loft3d(ellipsoid_table(n=n), shape)  # assert_valid + twist check inside
    assert solid.is_valid and len(solid.solids()) == 1


def test_loft_ruled_option(preset):
    smooth = loft3d(preset, ruled=False)
    ruled = loft3d(preset, ruled=True)
    assert len(ruled.faces()) > len(smooth.faces()) == 1
    assert ruled.volume == pytest.approx(smooth.volume, rel=0.05)


def test_section_profiles_share_seam():
    for shape in ("ellipse", "egg", "teardrop"):
        yz = section_profile(shape)
        assert yz[0] == pytest.approx([0.0, 1.0])
        assert yz[1, 0] > 0  # runs toward +y first
        assert np.abs(yz[:, 0]).max() <= 1.0 + 1e-9


def test_seam_check_catches_reversed_section(preset):
    rows = list(zip(preset.x, preset.center_z, preset.v_radius, preset.half_width, strict=True))
    wires = [section_wire(*r) for r in rows]
    check_section_seams(wires, preset)
    x, cz, v, hw = rows[5]
    wires[5] = section_wire(x, cz, v, -hw)  # mirrored: runs toward -y first
    with pytest.raises(EnvelopeError, match="different directions"):
        check_section_seams(wires, preset)


def test_preset_loft_bbox(preset):
    props = mass_properties(loft3d(preset))
    lo, hi = props["bbox_mm"]["min"], props["bbox_mm"]["max"]
    assert lo[0] == pytest.approx(0.0, abs=0.01) and hi[0] == pytest.approx(175.0, abs=0.01)
    assert hi[1] == pytest.approx(-lo[1], abs=1e-3)  # symmetric about y = 0
    assert hi[1] == pytest.approx(preset.half_width.max(), abs=0.05)
    assert props["centroid_mm"][1] == pytest.approx(0.0, abs=1e-3)


def elliptic_slab_volume(a, b, hw, r):
    """Hand calc: elliptic outline × 2·hw, minus a (1 − π/4)r² spandrel swept around each
    outer loop along the curve through its centroid (Pappus, inset d = 0.2234 r)."""
    perimeter = np.pi * (3 * (a + b) - np.sqrt((3 * a + b) * (a + 3 * b)))
    d = (10 - 3 * np.pi) / (12 - 3 * np.pi) * r
    return np.pi * a * b * 2 * hw - 2 * (1 - np.pi / 4) * r**2 * (perimeter - 2 * np.pi * d)


@pytest.mark.parametrize("r", [0.0, 1.0, 3.0])
def test_profile25d_matches_hand_calc(r):
    a, b, hw = 60.0, 15.0, 5.0
    table = ellipsoid_table(a, b, z0=2.0, half_width=hw)
    solid, ro = profile25d(table, r)
    assert not ro.clamped and ro.applied_mm == r
    assert solid.volume == pytest.approx(elliptic_slab_volume(a, b, hw, r), rel=0.02)
    assert solid.volume == pytest.approx(elliptic_slab_volume(a, b, hw, r), rel=0.002)
    cg = solid.center(CenterOf.MASS)
    centroid = (cg.X, cg.Y, cg.Z)
    assert centroid == pytest.approx((a, 0.0, 2.0), abs=0.05)


def test_profile25d_clamps_to_max_fillet():
    table = ellipsoid_table(60.0, 15.0, z0=2.0, half_width=5.0)
    solid, ro = profile25d(table, 4.75)  # 0.95 × half-thickness
    assert ro.clamped and ro.limited_by == "max_fillet"
    assert 0 < ro.applied_mm < 4.75 and "clamped" in ro.describe()
    assert solid.is_valid


@pytest.mark.parametrize("r", [0.5, 4.0, 0.95 * 8.628])
def test_profile25d_preset_radius_range(preset, r):
    solid, ro = profile25d(preset, r)
    assert solid.is_valid and len(solid.solids()) == 1
    assert ro.applied_mm <= r
    assert ro.clamped == (ro.applied_mm < r)
    if r > ro.min_curvature_radius_mm:
        assert ro.limited_by == "curvature"
    bb = solid.bounding_box()
    y_lo, y_hi = bb.min.Y, bb.max.Y
    assert y_hi == pytest.approx(preset.half_width.max(), abs=1e-3)
    assert y_lo == pytest.approx(-preset.half_width.max(), abs=1e-3)


def test_build_envelope_dispatch(preset):
    env = build_envelope(preset, "profile25d", roundover_r=2.0)
    assert env.style == "profile25d" and env.roundover is not None
    env = build_envelope(preset, "loft3d", section_shape="egg")
    assert env.section_shape == "egg" and env.roundover is None
    with pytest.raises(EnvelopeError):
        build_envelope(preset, "bogus")  # type: ignore[arg-type]
