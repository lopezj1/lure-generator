"""Paper-sheet scale (#122): synthetic photos from a tilted pinhole camera.

The sheet lies on a wood-like surface under uneven light with sensor noise, and a dark lure
silhouette lies on the sheet. Accuracy targets: corners well under a pixel, mm/px within
1%, and the extracted length and depth within 1%.
"""

import cv2
import numpy as np
import pytest
from _profile_synth import LETTER, Scene, paper_photo

from luregen.config.models import PaperSheet, ProfileConfig
from luregen.profile.extract import ProfileError
from luregen.profile.pipeline import ExtractOptions, extract_profile, options_from_config
from luregen.profile.sheet import (
    SHEETS,
    detect_sheet,
    estimate_aspect,
    order_corners,
    parse_sheet,
)

LENGTH, DEPTH = 175.0, 50.0
TILTS = [(0.0, 0.0), (12.0, -8.0), (20.0, 10.0)]


def corner_error(img: np.ndarray, truth: dict) -> float:
    corners = detect_sheet(img)
    assert corners is not None
    return float(np.abs(corners - order_corners(truth["corners_px"])).max())


@pytest.mark.parametrize("tilt", TILTS)
@pytest.mark.parametrize("landscape", [True, False])
def test_sheet_corners_are_subpixel_accurate(tilt, landscape):
    img, truth = paper_photo(tilt=tilt, landscape=landscape)
    assert corner_error(img, truth) < 0.3


def test_occluded_edge_and_corner_are_recovered():
    """Dark objects across an edge and over a corner: line fits skip the covered parts."""
    img, truth = paper_photo(lure=None, blobs=[(80, 70, 25, 15), (220, 285, 30, 12)])
    assert corner_error(img, truth) < 0.3


@pytest.mark.parametrize(
    ("tilt", "landscape"), [(TILTS[0], True), (TILTS[1], False), (TILTS[2], True)]
)
def test_paper_scale_recovers_lure_within_one_percent(tilt, landscape):
    img, truth = paper_photo(tilt=tilt, landscape=landscape, seed=3)
    result = extract_profile(img, ExtractOptions(scale="paper"))
    scale = result.side.scale
    assert scale.method == "paper"
    assert abs(scale.aspect_error) < 0.005
    assert not result.notes

    # mm/px: a known plane distance (the sheet's diagonal) measured in the rectified image
    corners_mm = np.array([[0.0, 0.0], [truth["sheet_mm"][0], truth["sheet_mm"][1]]])
    plane = cv2.perspectiveTransform(
        truth["corners_px"][[0, 2]].reshape(-1, 1, 2), scale.homography
    ).reshape(-1, 2)
    measured = np.hypot(*(plane[1] - plane[0])) * scale.mm_per_px
    assert measured == pytest.approx(np.hypot(*corners_mm[1]), rel=0.01)

    assert result.table.length == pytest.approx(LENGTH, rel=0.01)
    assert result.table.max_depth == pytest.approx(DEPTH, rel=0.01)


@pytest.mark.parametrize("landscape", [True, False])
def test_orientation_follows_the_sheet(landscape):
    img, _ = paper_photo(landscape=landscape, tilt=(10.0, 6.0))
    scale = extract_profile(img, ExtractOptions(scale="paper")).side.scale
    short, long = LETTER
    assert scale.sheet_mm == ((long, short) if landscape else (short, long))
    aspect, reliable = estimate_aspect(detect_sheet(img), img.shape)
    assert reliable
    assert aspect == pytest.approx(long / short if landscape else short / long, rel=0.005)


def test_auto_prefers_a_sheet_over_known_length():
    img, _ = paper_photo(tilt=(8.0, 4.0))
    result = extract_profile(img, ExtractOptions(scale="auto", length_mm=150.0))
    assert result.side.scale.method == "paper"
    assert result.table.length == pytest.approx(LENGTH, rel=0.01)


def test_no_sheet_found():
    img, _ = paper_photo(with_sheet=False, lure=None)
    assert detect_sheet(img) is None
    with pytest.raises(ProfileError, match="no letter sheet was found"):
        extract_profile(img, ExtractOptions(scale="paper"))


def test_plain_light_backdrop_is_not_a_sheet():
    """A bright background filling the frame must not count as a sheet (auto → length)."""
    s = Scene(220, 150)
    s.ellipse(110, 75, LENGTH / 2, DEPTH / 2)
    img = s.render()
    assert detect_sheet(img) is None
    result = extract_profile(img, ExtractOptions(length_mm=LENGTH))
    assert result.side.scale.method == "length"


def test_wrong_sheet_size_is_flagged():
    img, _ = paper_photo(tilt=(12.0, -8.0))
    a4 = extract_profile(img, ExtractOptions(scale="paper", sheet="a4", sheet_mm=SHEETS["a4"]))
    assert a4.side.scale.aspect_error == pytest.approx(-0.085, abs=0.01)
    assert any("aspect ratio is off" in n for n in a4.notes)
    with pytest.raises(ProfileError, match="not a 200x200 mm sheet"):
        extract_profile(img, ExtractOptions(scale="paper", sheet="200x200 mm", sheet_mm=(200, 200)))


def test_lure_over_the_sheet_edge_is_an_error():
    img, _ = paper_photo(lure=(276.0, 50.0), tilt=(5.0, 5.0))
    with pytest.raises(ProfileError, match="reaches the sheet's edge"):
        extract_profile(img, ExtractOptions(scale="paper"))


def test_parse_sheet():
    assert parse_sheet("Letter") == ("letter", (215.9, 279.4))
    assert parse_sheet("a4") == ("a4", (210.0, 297.0))
    assert parse_sheet("300x200") == ("300x200 mm", (200.0, 300.0))
    for bad in ("legal", "0x10", "10"):
        with pytest.raises(ProfileError):
            parse_sheet(bad)


def test_paper_config():
    cfg = ProfileConfig(
        source="preset",
        preset="x.csv",
        scale="paper",
        paper={"size": "custom", "width_mm": 300, "height_mm": 200, "inset_mm": 6},
    )
    opts = options_from_config(cfg)
    assert (opts.scale, opts.sheet_mm, opts.sheet_inset_mm) == ("paper", (200, 300), 6)
    assert opts.sheet == "200x300 mm"
    assert options_from_config(ProfileConfig(source="preset", preset="x.csv")).sheet_mm == LETTER
    with pytest.raises(ValueError, match="requires width_mm and height_mm"):
        PaperSheet(size="custom", width_mm=100)
