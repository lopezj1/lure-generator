import os
import shutil
from pathlib import Path

import cv2
import pytest
from _profile_synth import Scene, lure_outline, paper_photo, tilt
from typer.testing import CliRunner

from luregen.cli import make_app
from luregen.profile.table import StationTable


def write_scene(path: Path, *, marker: bool) -> Path:
    """Ellipse body; with a marker the photo is also tilted."""
    s = Scene(220, 150)
    s.ellipse(110, 100, 87.5, 25)
    if marker:
        s.marker(95, 40, 30)
    img = s.render()
    cv2.imwrite(str(path), tilt(img, 0.1) if marker else img)
    return path


def write_lure_facing_right(path: Path) -> Path:
    """Asymmetric body (deepest toward the nose), photographed nose-right."""
    outline = lure_outline(175, 50)
    outline[:, 0] = 197.5 - outline[:, 0]
    s = Scene(220, 80)
    s.polygon(outline + [0, 45])
    cv2.imwrite(str(path), s.render())
    return path


def keep_artifact(path: Path, name: str) -> None:
    """Copy a debug image to $LUREGEN_TEST_ARTIFACTS (set in CI) for visual review."""
    if dest := os.environ.get("LUREGEN_TEST_ARTIFACTS"):
        Path(dest).mkdir(parents=True, exist_ok=True)
        shutil.copy(path, Path(dest) / name)


def test_profile_command_with_marker_and_top_view(tmp_path: Path):
    side = write_scene(tmp_path / "side.png", marker=True)
    top = tmp_path / "top.png"
    t = Scene(220, 60)
    t.ellipse(110, 30, 87.5, 8.5)
    cv2.imwrite(str(top), t.render())
    out = tmp_path / "out" / "stations.csv"

    result = CliRunner().invoke(
        make_app(), ["profile", str(side), "-o", str(out), "--top", str(top), "--n-stations", "21"]
    )
    assert result.exit_code == 0, result.output
    assert "ArUco 30 mm marker" in result.output
    table = StationTable.load_csv(out)
    assert len(table) == 21
    assert table.length == pytest.approx(175, rel=0.01)
    assert table.max_depth == pytest.approx(50, rel=0.01)
    assert table.max_width == pytest.approx(17, rel=0.01)
    for name in ("profile_debug.png", "profile_mask.png", "profile_mask_top.png"):
        assert cv2.imread(str(out.parent / name)) is not None
    keep_artifact(out.parent / "profile_debug.png", "profile_debug_aruco_top.png")


def test_profile_command_length_scale_nose_right(tmp_path: Path):
    side = write_lure_facing_right(tmp_path / "side.png")
    out = tmp_path / "stations.csv"
    args = ["profile", str(side), "-o", str(out), "--length", "175", "--nose", "right"]
    result = CliRunner().invoke(make_app(), args)
    assert result.exit_code == 0, result.output
    table = StationTable.load_csv(out)
    assert table.length == pytest.approx(175)
    assert table.max_width == pytest.approx(0.34 * table.max_depth, rel=1e-3)
    assert table.max_depth == pytest.approx(50, rel=0.01)
    # the deep end is the nose: stations are deeper near x = 0 than near the tail
    assert table.v_radius[2] > table.v_radius[-3]
    assert abs(table.tail[1]) < 0.5  # nose and tail tips both lie on the axis
    keep_artifact(tmp_path / "profile_debug.png", "profile_debug_length.png")


def test_profile_command_reports_missing_scale(tmp_path: Path):
    side = write_scene(tmp_path / "side.png", marker=False)
    result = CliRunner().invoke(make_app(), ["profile", str(side), "-o", str(tmp_path / "s.csv")])
    assert result.exit_code == 1
    assert "known total length" in result.output


def test_profile_command_rejects_bad_choice(tmp_path: Path):
    side = write_scene(tmp_path / "side.png", marker=False)
    args = ["profile", str(side), "-o", str(tmp_path / "s.csv"), "--scale", "ruler"]
    assert CliRunner().invoke(make_app(), args).exit_code == 2


def test_profile_command_paper_scale(tmp_path: Path):
    img, _ = paper_photo(tilt=(10.0, -6.0), landscape=False)
    side = tmp_path / "side.jpg"
    cv2.imwrite(str(side), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    out = tmp_path / "stations.csv"
    args = ["profile", str(side), "-o", str(out), "--scale", "paper", "--paper", "letter"]
    result = CliRunner().invoke(make_app(), args)
    assert result.exit_code == 0, result.output
    assert "letter sheet 215.9x279.4 mm" in result.output
    table = StationTable.load_csv(out)
    assert table.length == pytest.approx(175, rel=0.01)
    assert table.max_depth == pytest.approx(50, rel=0.01)
    keep_artifact(out.parent / "profile_debug.png", "profile_debug_paper.png")


def test_profile_command_rejects_bad_paper(tmp_path: Path):
    side = write_scene(tmp_path / "side.png", marker=False)
    args = ["profile", str(side), "-o", str(tmp_path / "s.csv"), "--paper", "legal"]
    result = CliRunner().invoke(make_app(), args)
    assert result.exit_code == 2
