import cv2
import numpy as np
import pytest
from _profile_synth import BG, BODY, PX_PER_MM, Scene, lure_outline

from luregen.profile.extract import (
    ProfileError,
    anchor_tips,
    body_frame,
    largest_contour,
    resample_closed,
    segment,
    smooth_contour,
    split_rails,
)


def ellipse_image(a=87.5, b=25.0, body=BODY, bg=BG) -> np.ndarray:
    s = Scene(220, 80)
    s.big[:] = bg
    s.ellipse(110, 40, a, b, value=body)
    return s.render()


def roughness(points: np.ndarray) -> float:
    p = resample_closed(points, 1500)
    d2 = p[:-2] - 2 * p[1:-1] + p[2:]
    return float(np.sqrt((d2**2).sum(axis=1).mean()))


# --- segmentation (#10) ---


def test_segment_dark_and_light_subjects_match():
    dark = segment(ellipse_image(body=60, bg=235))
    light = segment(ellipse_image(body=235, bg=60))
    area = np.pi * 87.5 * 25 * PX_PER_MM**2
    for mask in (dark, light):
        assert abs(np.count_nonzero(mask) / area - 1) < 0.01
    assert np.count_nonzero(dark != light) < 0.002 * dark.size


def test_segment_keeps_largest_blob_and_fills_holes():
    s = Scene(220, 80)
    s.ellipse(110, 40, 87.5, 25)
    s.ellipse(15, 15, 5, 5)  # debris
    s.ellipse(110, 40, 3, 3, value=BG)  # eye highlight
    mask = segment(s.render())
    n, _ = cv2.connectedComponents(mask)
    assert n == 2  # background + body
    assert mask[mask.shape[0] // 2, mask.shape[1] // 2] == 255


def test_segment_excludes_polygons():
    img = ellipse_image()
    left_half = np.array([[0, 0], [440, 0], [440, 320], [0, 320]], dtype=float)
    mask = segment(img, exclude=[left_half])
    assert not mask[:, :440].any() and mask[:, 440:].any()


def test_rembg_flag_reports_unavailable():
    with pytest.raises(ProfileError, match="rembg"):
        segment(ellipse_image(), use_rembg=True)


def test_blank_image_has_no_silhouette():
    with pytest.raises(ProfileError):
        largest_contour(np.zeros((50, 50), np.uint8))


def test_contour_is_subpixel_on_pixel_edges():
    mask = np.zeros((100, 200), np.uint8)
    mask[20:60, 30:150] = 255
    xy = largest_contour(mask)
    assert np.ptp(xy[:, 0]) == pytest.approx(120) and np.ptp(xy[:, 1]) == pytest.approx(40)


# --- smoothing and rails (#12) ---


@pytest.fixture
def jagged_lure() -> tuple[np.ndarray, float, float]:
    """A pointed lure with ±0.3 mm outline noise and a thin spike on its back."""
    length, depth = 175.0, 50.0
    rng = np.random.default_rng(0)
    outline = lure_outline(length, depth, 250)
    outline = outline + rng.normal(0, 0.3, outline.shape)
    s = Scene(220, 90)
    s.polygon(outline + [20, 50])
    spike = np.array([[100, 70], [100.3, 85], [100.6, 70]])
    s.polygon(spike)
    return s.render(), length, depth


@pytest.mark.parametrize("method", ["efd", "spline"])
def test_smoothing_removes_jaggies_and_spike_but_keeps_length(jagged_lure, method):
    img, length, depth = jagged_lure
    raw = largest_contour(segment(img))
    smooth = anchor_tips(smooth_contour(raw, method=method), raw)
    assert roughness(smooth) < 0.25 * roughness(raw)
    assert np.ptp(smooth[:, 0]) / PX_PER_MM == pytest.approx(length, rel=0.005)
    assert np.ptp(smooth[:, 1]) / PX_PER_MM == pytest.approx(depth, rel=0.01)  # no spike


def test_unanchored_smoothing_shrinks_less_than_two_percent(jagged_lure):
    img, length, _ = jagged_lure
    raw = largest_contour(segment(img))
    shrink = 1 - np.ptp(smooth_contour(raw)[:, 0]) / np.ptp(raw[:, 0])
    assert 0 <= shrink < 0.02  # why anchor_tips exists


def test_rails_are_single_valued_and_meet_at_tips():
    t = np.linspace(0, 2 * np.pi, 800, endpoint=False)
    pts = np.column_stack([50 + 50 * np.cos(t), 10 * np.sin(t)])
    r = split_rails(pts, n=200)
    assert np.all(np.diff(r.x) > 0) and np.all(r.top >= r.bottom)
    assert r.nose == pytest.approx((0, 0), abs=1e-9) and r.tail == pytest.approx((100, 0))
    expected = 10 * np.sqrt(1 - ((r.x - 50) / 50) ** 2)
    np.testing.assert_allclose(r.top, expected, atol=0.05)
    np.testing.assert_allclose(r.bottom, -expected, atol=0.05)
    top, bot = r.at(np.array([0.0, 100.0]))
    np.testing.assert_allclose([top, bot], 0, atol=1e-9)


def test_rails_take_the_envelope_where_the_outline_folds_back():
    # a "C" shape: the outline crosses some x lines four times
    pts = np.array([[0, 0], [10, 5], [2, 3], [10, 10], [-0.0, 12], [-3, 6]], dtype=float)
    r = split_rails(pts, n=50)
    assert np.all(r.top >= r.bottom) and np.isfinite(r.top).all()


def test_body_frame_puts_nose_at_origin_either_way():
    pts = np.array([[10, 50], [110, 40], [60, 20], [60, 70]], dtype=float)
    left = body_frame(pts, "left")
    right = body_frame(pts, "right")
    np.testing.assert_allclose(left.to_body(pts)[0], [0, 0])
    np.testing.assert_allclose(left.to_body(pts)[1], [100, 10])
    np.testing.assert_allclose(right.to_body(pts)[0], [100, -10])
    np.testing.assert_allclose(right.to_px(right.to_body(pts)), pts)
