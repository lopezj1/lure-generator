import cv2
import numpy as np
import pytest
from _profile_synth import PX_PER_MM, Scene, tilt, tilt_matrix

from luregen.profile.extract import ProfileError
from luregen.profile.pipeline import ExtractOptions, extract_profile
from luregen.profile.scale import detect_marker, length_scale, rectify

LENGTH, DEPTH, MARKER = 175.0, 50.0, 30.0


def scene(marker: bool = True) -> np.ndarray:
    """Ellipse body with a marker centered below it (the recommended placement)."""
    s = Scene(220, 150)
    s.ellipse(110, 100, LENGTH / 2, DEPTH / 2)
    if marker:
        s.marker(110 - MARKER / 2, 10 + MARKER, MARKER)
    return s.render()


def true_corners() -> np.ndarray:
    """Marker corners in OpenCV pixel-center coordinates (TL, TR, BR, BL)."""
    k = PX_PER_MM
    x0, y0 = (110 - MARKER / 2) * k, (150 - 10 - MARKER) * k
    side = MARKER * k
    return np.array([[x0, y0], [x0 + side, y0], [x0 + side, y0 + side], [x0, y0 + side]]) - 0.5


@pytest.mark.parametrize("amount", [0.0, 0.12])
def test_marker_corners_are_subpixel_accurate(amount):
    img = scene()
    expected = true_corners()
    if amount:
        img = tilt(img, amount)
        m = tilt_matrix(img.shape, amount)
        expected = cv2.perspectiveTransform(expected.reshape(-1, 1, 2), m).reshape(-1, 2)
    corners = detect_marker(img)
    assert corners is not None
    assert np.abs(corners - expected).max() < 0.15


def test_no_marker_detected():
    assert detect_marker(scene(marker=False)) is None


def test_rectify_square_marker_gives_scale():
    img = scene()
    out, scale = rectify(img, detect_marker(img), MARKER)
    assert scale.method == "aruco"
    assert scale.mm_per_px == pytest.approx(1 / PX_PER_MM, rel=0.002)
    assert np.abs(np.subtract(out.shape[:2], img.shape[:2])).max() <= 2


@pytest.mark.parametrize("amount", [0.0, 0.12, 0.2])
def test_tilted_ellipse_is_recovered_within_one_percent(amount):
    img = tilt(scene(), amount) if amount else scene()
    result = extract_profile(img, ExtractOptions(scale="aruco", marker_mm=MARKER))
    t = result.table
    assert t.length == pytest.approx(LENGTH, rel=0.01)
    assert t.max_depth == pytest.approx(DEPTH, rel=0.01)
    assert result.side.scale.method == "aruco"
    assert not result.notes


def test_far_marker_adds_a_note():
    s = Scene(260, 120)
    s.ellipse(150, 60, LENGTH / 2, DEPTH / 2)
    s.marker(5, 115, MARKER)
    result = extract_profile(s.render(), ExtractOptions(marker_mm=MARKER))
    assert any("marker widths" in n for n in result.notes)


def test_length_fallback_when_no_marker():
    result = extract_profile(scene(marker=False), ExtractOptions(length_mm=LENGTH))
    assert result.side.scale.method == "length"
    assert result.table.length == pytest.approx(LENGTH)
    assert result.table.max_depth == pytest.approx(DEPTH, rel=0.01)


def test_missing_scale_is_an_error():
    with pytest.raises(ProfileError, match="known total length"):
        extract_profile(scene(marker=False), ExtractOptions())
    with pytest.raises(ProfileError, match="no DICT_4X4_50 marker"):
        extract_profile(scene(marker=False), ExtractOptions(scale="aruco", length_mm=LENGTH))


def test_length_scale_validates():
    assert length_scale(700, 175).mm_per_px == pytest.approx(0.25)
    with pytest.raises(ProfileError):
        length_scale(0, 175)
