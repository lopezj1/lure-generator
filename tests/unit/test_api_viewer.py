"""W5 (#66): model parts, cached tessellation, mesh/exact sections, path safety."""

import gzip
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")
pytest.importorskip("ocp_tessellate", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402

STAMP = "20260101-120000-abc1234"
BASE = f"/api/builds/glide/v1/{STAMP}"
# body: 40 x 20 x 10 box centred at the origin with a 20 x 10 x 4 internal void
OUTER_X, VOID_X = 20 * 10, 10 * 4  # x-cut areas (y * z)
OUTER_Z, VOID_Z = 40 * 20, 20 * 10  # z-cut areas (x * y)


@pytest.fixture(scope="module")
def model_files(tmp_path_factory) -> Path:
    from build123d import Box, Pos

    from luregen.geometry.export import export_all
    from luregen.geometry.validate import assert_valid

    out = tmp_path_factory.mktemp("model")
    body = Box(40, 20, 10) - Box(20, 10, 4)
    assert_valid(body, solids=1, shells=2)
    export_all(body, out, "body")
    export_all(Pos(-30, 0, 0) * Box(10, 10, 10), out, "head")
    return out


@pytest.fixture
def root(tmp_path: Path, model_files: Path) -> Path:
    build = tmp_path / "builds" / "glide" / "v1" / STAMP
    (build / "model").mkdir(parents=True)
    for f in model_files.iterdir():
        (build / "model" / f.name).write_bytes(f.read_bytes())
    (tmp_path / "builds" / "glide" / "v1" / "latest").symlink_to(STAMP)
    (tmp_path / "builds" / "glide" / "v1" / ".hidden").mkdir()
    (tmp_path / "builds" / "glide" / "v1" / ".hidden" / "model").mkdir()
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def test_model_parts(client):
    info = client.get(f"{BASE}/model").json()
    assert [p["name"] for p in info["parts"]] == ["head", "body"]  # tree order, not alphabetical
    body = info["parts"][1]
    assert body["step"] == "model/body.step" and body["stl"] == "model/body.stl"
    assert body["bbox_min"] == pytest.approx([-20, -10, -5], abs=1e-3)
    assert info["bbox_min"] == pytest.approx([-35, -10, -5], abs=1e-3)
    assert client.get("/api/builds/glide/v1/latest/model").json()["stamp"] == STAMP


def test_model_without_parts(client, root):
    for f in (root / "builds/glide/v1" / STAMP / "model").glob("*.step"):
        f.unlink()
    assert client.get(f"{BASE}/model").status_code == 404


def test_tessellation_cached(client, root):
    r = client.get(f"{BASE}/model/tessellation?deviation=0.2")
    assert r.status_code == 200 and r.headers["x-luregen-cache"] == "miss"
    data = r.json()
    assert set(data) == {"instances", "shapes"}
    assert data["shapes"]["name"] == "v1"
    assert [p["name"] for p in data["shapes"]["parts"]] == ["head", "body"]
    assert data["shapes"]["parts"][1]["id"] == "/v1/body"
    assert data["instances"][0]["vertices"]["codec"] == "b64"
    cached = list((root / "builds/glide/v1" / STAMP / "model/.viewer").glob("*.json.gz"))
    assert len(cached) == 1
    assert json.loads(gzip.decompress(cached[0].read_bytes())) == data

    again = client.get(f"{BASE}/model/tessellation?deviation=0.2")
    assert again.headers["x-luregen-cache"] == "hit" and again.json() == data
    # without gzip in Accept-Encoding the server decompresses
    plain = client.get(
        f"{BASE}/model/tessellation?deviation=0.2", headers={"Accept-Encoding": "identity"}
    )
    assert "content-encoding" not in plain.headers and plain.json() == data
    # the hidden cache folder is not listed as build files
    listed = client.get("/api/builds").json()[0]["files"]
    assert not any(".viewer" in f["name"] for f in listed)


@pytest.mark.parametrize(
    ("axis", "offset", "area", "void", "loops"),
    [
        ("x", 0.0, OUTER_X - VOID_X, VOID_X, 2),
        ("z", 1.0, OUTER_Z - VOID_Z, VOID_Z, 2),
        ("x", 15.0, OUTER_X, 0.0, 1),  # beside the void
        ("x", 99.0, 0.0, 0.0, 0),  # misses the model
    ],
)
def test_section_mesh(client, axis, offset, area, void, loops):
    s = client.get(f"{BASE}/section", params={"axis": axis, "offset": offset, "part": "body"})
    assert s.status_code == 200
    s = s.json()
    assert s["method"] == "mesh"
    assert s["area_mm2"] == pytest.approx(area, rel=1e-6, abs=1e-6)
    assert s["void_area_mm2"] == pytest.approx(void, rel=1e-6, abs=1e-6)
    assert len(s["loops"]) == loops
    if void:
        assert sorted(lp["kind"] for lp in s["loops"]) == ["outer", "void"]
        assert s["perimeter_mm"] > 0


def test_section_all_parts(client):
    s = client.get(f"{BASE}/section", params={"axis": "y", "offset": 0}).json()
    assert s["part"] == "all" and s["plane_axes"] == ["x", "z"]
    assert {lp["part"] for lp in s["loops"]} == {"head", "body"}
    assert s["area_mm2"] == pytest.approx(40 * 10 - 20 * 4 + 10 * 10, rel=1e-6)


def test_section_exact(client):
    s = client.get(f"{BASE}/section", params={"axis": "x", "offset": 0, "exact": True}).json()
    assert s["method"] == "exact"
    assert s["area_mm2"] == pytest.approx(OUTER_X - VOID_X, rel=1e-6)
    assert s["void_area_mm2"] == pytest.approx(VOID_X, rel=1e-6)
    assert s["mesh_area_mm2"] == pytest.approx(s["area_mm2"], rel=1e-6)
    assert s["differs"] is False and abs(s["delta_pct"]) < 1e-3


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (f"{BASE}/section?axis=w&offset=0", 422),
        (f"{BASE}/section?axis=x&offset=0&part=..", 422),
        (f"{BASE}/section?axis=x&offset=0&part=tail", 404),
        (f"{BASE}/section?axis=x&offset=0&part=.viewer", 422),
        ("/api/builds/glide/v1/.hidden/model", 422),
        ("/api/builds/glide/v1/..%2F..%2F../model", 404),
        ("/api/builds/glide/v1/nope/model", 404),
        ("/api/builds/glide/v1/nope/model/tessellation", 404),
        (f"{BASE}/model/tessellation?deviation=0", 422),
    ],
)
def test_path_safety(client, path, code):
    assert client.get(path).status_code == code


def test_symlink_escape(client, root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "model").mkdir()
    (root / "builds/glide/v1/escape").symlink_to(outside, target_is_directory=True)
    assert client.get("/api/builds/glide/v1/escape/model").status_code == 404
