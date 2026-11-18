"""W9 (#70): report summaries, compare two builds, GLB conversion and zip archives."""

import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402

A = "20260101-120000-abc1234"
B = "20260102-120000-def5678-dirty"
BASE_B = f"/api/builds/glide/v1/{B}"


def _report(mass: float, cg_z: float, cb_z: float, status: str) -> dict:
    return {
        "variant": "v1",
        "family": "glide",
        "created": "2026-01-01T12:00:00",
        "git": {"sha": "abc1234", "dirty": False},
        "status": "built",
        "warnings": [],
        "envelope": {"volume_cm3": 40.0},
        "sections": {
            "head": {"mass_g": mass * 0.6, "density": 1.1, "density_g_cm3": 1.1},
            "tail": {"mass_g": mass * 0.4, "density": 1.0, "density_g_cm3": 1.0},
        },
        "lure": {"mass_g": mass, "density_g_cm3": 1.05, "cb_above_cg_target_mm": 1.5},
        "verdict": {"status": status, "label": "x", "failed": [] if status == "pass" else ["cg"]},
        "summary": {
            "mass_g": mass,
            "mass_oz": round(mass / 28.349523125, 3),
            "density_g_cm3": 1.05,
            "cg_mm": [80.0, 0.0, cg_z],
            "cb_mm": [82.0, 0.0, cb_z],
            "verdict": {"status": status, "label": "x"},
        },
    }


@pytest.fixture
def root(tmp_path: Path) -> Path:
    import trimesh

    variant = tmp_path / "builds" / "glide" / "v1"
    for stamp, (mass, cg_z, cb_z, status, density) in {
        A: (40.0, -1.0, 0.5, "fail", 1.05),
        B: (42.5, -2.0, 0.25, "pass", 1.1),
    }.items():
        build = variant / stamp
        (build / "model").mkdir(parents=True)
        (build / "fab" / "cnc").mkdir(parents=True)
        (build / "report.json").write_text(json.dumps(_report(mass, cg_z, cb_z, status)))
        (build / "resolved.toml").write_text(
            f'name = "{stamp}"\n[process]\nmaterial_density = {density}\n'
            '[[hardware]]\nid = "eye"\nqty = 1\n'
        )
    build = variant / B
    trimesh.creation.box(extents=(20, 10, 4)).export(build / "model" / "body.stl")
    (build / "model" / "body.step").write_text("ISO-10303-21;\n")
    (build / "fab" / "cnc" / "T1.nc").write_text("G21\nM30\n")
    (build / "fab" / "cnc" / "T2.nc").write_text("G21\nM30\n")
    (variant / "latest").symlink_to(B, target_is_directory=True)
    (variant / "broken").mkdir()
    (variant / "broken" / "report.json").write_text('{"status": "error", "error": "loft"}')
    (tmp_path / "secret.txt").write_text("outside builds/")
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def test_summary_has_report_numbers_and_downloads(client):
    builds = {b["stamp"]: b for b in client.get("/api/builds").json()}
    rep = builds[B]["report"]
    assert rep["mass_g"] == 42.5 and rep["mass_oz"] == pytest.approx(1.499, abs=1e-3)
    assert rep["cg_minus_cb_mm"] == [-2.0, 0.0, -2.25]
    assert rep["cb_above_cg_mm"] == 2.25 and rep["cb_above_cg_target_mm"] == 1.5
    assert rep["verdict"]["status"] == "pass"
    assert rep["sections"]["head"] == {"mass_g": 25.5, "density_g_cm3": 1.1}
    assert builds[A]["report"]["verdict"]["failed"] == ["cg"]
    assert builds["broken"]["report"]["error"] == "loft"
    assert builds["broken"]["report"]["mass_g"] is None
    downloads = {d["label"]: d for d in builds[B]["downloads"]}
    assert downloads["model/body.step"]["format"] == "STEP"
    assert downloads["model/body.stl"]["href"] == f"{BASE_B}/files/model/body.stl?download=true"
    assert downloads["model/body.glb"]["href"] == f"{BASE_B}/glb/body"
    assert downloads["fab/cnc/T1.nc"]["format"] == "NC"
    zips = [d["href"].split("=")[-1] for d in builds[B]["downloads"] if d["format"] == "ZIP"]
    assert zips == ["all", "model", "cnc"]  # no fab/print files → no print zip


def test_compare(client):
    res = client.get(f"/api/builds/compare?a=glide/v1/{A}&b=glide/v1/latest")
    assert res.status_code == 200, res.text
    cmp = res.json()
    assert (cmp["a"]["stamp"], cmp["b"]["stamp"]) == (A, B)
    deltas = {d["key"]: d for d in cmp["deltas"]}
    assert deltas["mass_g"]["delta"] == 2.5
    assert deltas["cg_z"]["delta"] == -1.0
    assert deltas["cb_above_cg"] == {
        "key": "cb_above_cg",
        "label": "CB above CG",
        "unit": "mm",
        "a": 1.5,
        "b": 2.25,
        "delta": 0.75,
    }
    assert deltas["head.mass_g"]["delta"] == pytest.approx(1.5)
    assert "cg_minus_cb_x" in deltas and "envelope_volume_cm3" in deltas
    assert cmp["config_changes"] == [{"key": "process.material_density", "a": 1.05, "b": 1.1}]
    assert cmp["config_changes_truncated"] is False


@pytest.mark.parametrize(
    "query",
    [
        f"a=glide/v1/{A}",
        f"a=glide/v1&b=glide/v1/{A}",
        f"a=glide/v1/{A}&b=glide/v1/nope",
        f"a=glide/v1/{A}&b=glide/v1/..%2F..%2F..",
        f"a=glide/v1/{A}&b=../../secret.txt",
        f"a=glide/v1/{A}&b=.jobs/x/y",
    ],
)
def test_compare_rejects_bad_refs(client, query):
    assert client.get(f"/api/builds/compare?{query}").status_code in (400, 404, 422)


def test_archive_all_and_groups(client):
    res = client.get(f"{BASE_B}/archive")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert f'filename="v1-{B}.zip"' in res.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    top = f"v1-{B}/"
    assert {n.removeprefix(top) for n in names} == {
        "report.json",
        "resolved.toml",
        "fab/cnc/T1.nc",
        "fab/cnc/T2.nc",
        "model/body.step",
        "model/body.stl",
    }
    cnc = client.get("/api/builds/glide/v1/latest/archive?group=cnc")
    assert cnc.status_code == 200 and f"v1-{B}-cnc.zip" in cnc.headers["content-disposition"]
    assert sorted(zipfile.ZipFile(io.BytesIO(cnc.content)).namelist()) == [
        f"{top}fab/cnc/T1.nc",
        f"{top}fab/cnc/T2.nc",
    ]
    assert client.get(f"{BASE_B}/archive?group=print").status_code == 404
    assert client.get(f"{BASE_B}/archive?group=secret").status_code == 422


def test_glb_is_metres_y_up_and_cached(client, root):
    res = client.get(f"{BASE_B}/glb/body")
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "model/gltf-binary"
    magic, version, _length = struct.unpack_from("<4sII", res.content)
    assert (magic, version) == (b"glTF", 2)
    chunk_len = struct.unpack_from("<I", res.content, 12)[0]
    gltf = json.loads(res.content[20 : 20 + chunk_len])
    pos = gltf["accessors"][gltf["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    size = [hi - lo for lo, hi in zip(pos["min"], pos["max"], strict=True)]
    assert size == pytest.approx([0.020, 0.004, 0.010])  # x, z → y, y → z, in metres
    cache = root / "builds" / "glide" / "v1" / B / "model" / ".body.glb"
    assert cache.read_bytes() == res.content
    names = [f["name"] for f in client.get("/api/builds?variant=v1").json()[0]["files"]]
    assert "model/.body.glb" not in names
    assert client.get(f"{BASE_B}/glb/missing").status_code == 404
    assert client.get(f"{BASE_B}/glb/..").status_code in (404, 422)
