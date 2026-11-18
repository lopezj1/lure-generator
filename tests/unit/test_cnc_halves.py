"""CNC halves: glue-face pockets, centerplane split and side-view outlines (#40)."""

from pathlib import Path

import numpy as np
import pytest
import trimesh

from luregen.geometry import cnc_halves as ch
from luregen.profile.table import StationTable

REPO = Path(__file__).resolve().parents[2]
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"


@pytest.fixture(scope="module")
def table() -> StationTable:
    return StationTable.load_csv(PRESET)


def test_tool_diameter_from_id():
    assert ch.tool_diameter_from_id("flat_3.175") == 3.175
    with pytest.raises(ch.CncGeometryError):
        ch.tool_diameter_from_id("bogus")


def test_pocket_volume_matches_solid():
    p = ch.GlueFacePocket(60, -12, 18, 7, 1.6, 1.85)
    assert p.solid().volume == pytest.approx(p.volume, rel=1e-4)
    assert p.footprint().area == pytest.approx(ch.rounded_rect_area(18, 7, 1.85), rel=1e-3)
    bb = p.solid().bounding_box()
    y_lo, y_hi = bb.min.Y, bb.max.Y
    assert y_lo == pytest.approx(-1.6) and y_hi == pytest.approx(1.6)
    assert p.as_dict()["volume_cm3"] == pytest.approx(p.volume / 1000, abs=1e-4)


def test_size_glue_face_pocket(table):
    volume = 1000 * 8.0 / 18.0
    wall, corner = 3.5, 1.8375
    p = ch.size_glue_face_pocket(table, "ellipse", 65.5, -17.5, volume, wall, corner, (5, 80))
    assert p.volume == pytest.approx(volume, rel=2e-3)
    assert p.corner_r == corner and p.height >= 2 * corner
    assert p.length <= 30 and p.depth >= 0.8
    assert p.z >= -17.5  # lowest centre that fits, never below the target
    body = ch._Body(table, "ellipse")
    assert ch._fits(body, p, wall, 5, 80)
    xs = np.linspace(p.x - p.length / 2, p.x + p.length / 2, 9)
    cz, v = body.at(xs)
    assert np.all(p.z - p.height / 2 >= cz - v + wall - 1e-9)  # wood below the pocket


def test_pocket_that_cannot_fit(table):
    with pytest.raises(ch.CncGeometryError, match="no room"):
        ch.size_glue_face_pocket(table, "ellipse", 10, -2, 5000, 3.5, 1.8, (3, 80), 20)


def test_split_halves_and_outlines():
    mesh = trimesh.creation.icosphere(subdivisions=4, radius=10)
    mesh.apply_scale([4, 0.8, 2])  # 80 × 16 × 40, symmetric about y = 0
    hole = trimesh.creation.box([10, 4, 6])  # a sealed pocket across the glue face
    mesh = mesh.difference(hole, engine="manifold")
    right, left = ch.split_halves(mesh)
    assert right.is_watertight and left.is_watertight
    assert right.vertices[:, 1].min() == pytest.approx(0.0, abs=1e-5)
    assert left.vertices[:, 1].max() == pytest.approx(0.0, abs=1e-5)
    assert right.volume + left.volume == pytest.approx(mesh.volume, rel=1e-6)
    assert ch.max_half_width(right) == pytest.approx(8.0, rel=0.01)
    sil = ch.silhouette_xz(right)
    assert sil.area == pytest.approx(np.pi * 40 * 20, rel=0.01)
    assert len(sil.interiors) == 0  # projection ignores the pocket
    glue = ch.glue_face_xz(mesh)
    assert len(glue.interiors) == 1  # the pocket shows on the glue face
    assert glue.area == pytest.approx(sil.area - 60, rel=0.01)
