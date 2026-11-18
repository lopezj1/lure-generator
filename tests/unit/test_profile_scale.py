import cv2
import numpy as np
import pytest
from _profile_synth import PX_PER_MM, Scene, tilt, tilt_matrix

from luregen.profile.extract import ProfileError
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


def test_length_scale_validates():
    assert length_scale(700, 175).mm_per_px == pytest.approx(0.25)
    with pytest.raises(ProfileError):
        length_scale(0, 175)
