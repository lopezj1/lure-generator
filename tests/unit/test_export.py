from pathlib import Path

import numpy as np
import pytest
from build123d import Box, import_step

from luregen.geometry.envelope import loft3d, profile25d
from luregen.geometry.export import (
    ExportError,
    check_3mf,
    check_stl,
    export_all,
    read_3mf_mesh,
    tessellate,
)
from luregen.profile.table import StationTable

REPO = Path(__file__).resolve().parents[2]
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"
MB = 1024 * 1024


@pytest.fixture(scope="module")
def preset() -> StationTable:
    return StationTable.load_csv(PRESET)


@pytest.mark.parametrize("shape", ["ellipse", "teardrop"])
def test_loft_export_round_trip(preset, shape, tmp_path):
    solid = loft3d(preset, shape)
    info = export_all(solid, tmp_path, stem="body")
    assert {p.name for p in tmp_path.iterdir()} == {"body.step", "body.stl", "body.3mf"}

    back = import_step(tmp_path / "body.step")
    assert back.is_valid and len(back.solids()) == 1
    assert back.volume == pytest.approx(solid.volume, rel=1e-6)

    for key in ("stl", "3mf"):
        m = info[key]
        assert m["ok"] and m["watertight"] and m["manifold_ok"] and m["mesher"] == "loft_grid"
        assert abs(m["volume_error"]) < 0.005
    assert info["stl"]["triangles"] == info["3mf"]["triangles"]
    assert (tmp_path / "body.stl").stat().st_size < 5 * MB
    assert check_stl(tmp_path / "body.stl").ok
    v, f = read_3mf_mesh(tmp_path / "body.3mf")
    assert v.shape[1] == 3 and f.max() < len(v)


def test_profile25d_export(preset, tmp_path):
    solid, _ = profile25d(preset, 4.0)
    info = export_all(solid, tmp_path)
    assert info["stl"]["ok"] and info["stl"]["mesher"] == "occt"
    assert abs(info["stl"]["volume_error"]) < 0.005
    assert info["stl"]["size_bytes"] < 5 * MB
    assert check_3mf(tmp_path / "body.3mf").ok


def test_tessellation_tolerance_and_budget(preset):
    solid = loft3d(preset)
    fine = tessellate(solid, tolerance=0.005)
    coarse = tessellate(solid, tolerance=0.05)
    assert len(fine.mesh.faces) > len(coarse.mesh.faces)
    err = [abs(t.mesh.volume / solid.volume - 1) for t in (fine, coarse)]
    assert err[0] < err[1] < 0.01
    capped = tessellate(solid, tolerance=0.002, max_triangles=5000)
    assert len(capped.mesh.faces) <= 5000 and capped.tolerance > 0.002


def test_box_mesh_via_occt(tmp_path):
    box = Box(10, 20, 30)
    tess = tessellate(box)
    assert tess.mesher == "occt" and len(tess.mesh.faces) == 12
    assert tess.mesh.volume == pytest.approx(6000.0)
    assert np.allclose(tess.mesh.bounds, [[-5, -10, -15], [5, 10, 15]])


def test_open_mesh_fails_check(tmp_path):
    path = tmp_path / "open.stl"
    import trimesh

    trimesh.creation.box().subdivide().export(path)
    assert check_stl(path).ok
    mesh = trimesh.creation.box()
    mesh.update_faces(np.arange(len(mesh.faces) - 1))
    mesh.export(path)
    assert not check_stl(path).watertight
    with pytest.raises(ExportError):
        raise ExportError("sanity")
