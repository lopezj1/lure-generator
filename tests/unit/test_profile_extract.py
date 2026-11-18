import cv2
import numpy as np
import pytest
from _profile_synth import BG, BODY, PX_PER_MM, Scene

from luregen.profile.extract import (
    ProfileError,
    largest_contour,
    segment,
)


def ellipse_image(a=87.5, b=25.0, body=BODY, bg=BG) -> np.ndarray:
    s = Scene(220, 80)
    s.big[:] = bg
    s.ellipse(110, 40, a, b, value=body)
    return s.render()


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
