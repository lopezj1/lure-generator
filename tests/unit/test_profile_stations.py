from pathlib import Path

import numpy as np
import pytest

from luregen.profile.extract import ProfileError, split_rails
from luregen.profile.stations import (
    build_table,
    cosine_stations,
    width_from_ratio,
    width_from_top,
)
from luregen.profile.table import StationTable

PRESET = (
    Path(__file__).resolve().parents[2] / "spec/designs/glide/profiles/presets/placeholder_175.csv"
)


def ellipse_rails(length: float, half: float, offset: float = 0.0):
    t = np.linspace(0, 2 * np.pi, 4000, endpoint=False)
    pts = np.column_stack([length / 2 * (1 - np.cos(t)), offset + half * np.sin(t)])
    return split_rails(pts, n=2000)


def test_cosine_spacing_matches_preset_and_clusters_at_tips():
    preset = StationTable.load_csv(PRESET)
    x = cosine_stations(preset.length, len(preset))
    np.testing.assert_allclose(x, preset.x, atol=1e-3)
    gaps = np.diff(x)
    assert gaps[0] < gaps[len(gaps) // 2] and gaps[-1] < gaps[len(gaps) // 2]
    assert x[0] > 0 and x[-1] < preset.length


def test_table_from_analytic_ellipse():
    rails = ellipse_rails(175, 25)
    t = build_table(rails, 23, width_from_ratio(0.34), source="test")
    assert t.nose == (0, 0) and t.tail[0] == pytest.approx(175)
    expected = 25 * np.sqrt(1 - ((t.x - 87.5) / 87.5) ** 2)
    np.testing.assert_allclose(t.v_radius, expected, rtol=5e-3)
    np.testing.assert_allclose(t.center_z, 0, atol=1e-6)
    np.testing.assert_allclose(t.half_width, 0.34 * t.v_radius)
    assert t.max_depth == pytest.approx(50, rel=1e-3)


def test_width_from_top_view_rails_rescaled_to_side_length():
    side = ellipse_rails(175, 25)
    top = ellipse_rails(170, 8.5, offset=3.0)  # slightly shorter photo, off-axis
    t = build_table(side, 23, width_from_top(top, side.length))
    assert t.max_width == pytest.approx(17, rel=1e-3)
    np.testing.assert_allclose(t.half_width / t.v_radius, 0.34, rtol=0.01)


def test_rails_must_be_in_body_frame():
    t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    rails = split_rails(np.column_stack([10 + 50 * np.cos(t), np.sin(t)]))
    with pytest.raises(ProfileError, match="body frame"):
        build_table(rails, 10, width_from_ratio(0.34))
