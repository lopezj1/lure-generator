"""W1 (#62, #64): build listing, confined file serving, binary toolpaths."""

import gzip
import json
import shutil
import struct
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
STAMP = "20260101-120000-abc1234"
OLD = "20251231-090000-abc1234-dirty"
BASE = f"/api/builds/glide/v1/{STAMP}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    variant = tmp_path / "builds" / "glide" / "v1"
    for stamp in (OLD, STAMP):
        (variant / stamp / "model").mkdir(parents=True)
        report = {
            "variant": "v1",
            "family": "glide",
            "created": "2026-01-01T12:00:00",
            "git": {"sha": "abc1234", "dirty": stamp.endswith("dirty")},
            "status": "envelope",
            "warnings": ["placeholder masses"],
        }
        (variant / stamp / "report.json").write_text(json.dumps(report))
    build = variant / STAMP
    (build / "resolved.toml").write_text('name = "v1"\n')
    (build / "model" / "body.stl").write_bytes(b"solid body\nendsolid body\n")
    shutil.copy(FIXTURES / "orca_2.5.1_cube.gcode", build / "cube.gcode")
    shutil.copy(FIXTURES / "fusion_3drough_excerpt.nc", build / "rough.nc")
    (build / ".hidden").write_text("x")
    (variant / "latest").symlink_to(STAMP, target_is_directory=True)
    (tmp_path / "builds" / ".jobs").mkdir()
    (tmp_path / "builds" / ".jobs" / "job.log").write_text("log")
    (tmp_path / "secret.txt").write_text("outside builds/")
    return tmp_path


@pytest.fixture
def client(root: Path) -> TestClient:
    return TestClient(create_app(root))


def test_list_builds(client):
    builds = client.get("/api/builds").json()
    assert [b["stamp"] for b in builds] == [STAMP, OLD]  # newest first, .jobs skipped
    new, old = builds
    assert new["latest"] is True and old["latest"] is False
    assert new["path"] == f"builds/glide/v1/{STAMP}"
    assert new["status"] == "envelope" and new["git_sha"] == "abc1234"
    assert old["git_dirty"] is True
    assert new["warnings"] == ["placeholder masses"]
    names = [f["name"] for f in new["files"]]
    assert names == ["cube.gcode", "report.json", "resolved.toml", "rough.nc", "model/body.stl"]
    assert client.get("/api/builds?family=glide&variant=v1").json() == builds
    assert client.get("/api/builds?family=other").json() == []
    assert client.get("/api/builds?family=..").status_code == 422


def test_list_builds_without_builds_folder(tmp_path):
    assert TestClient(create_app(tmp_path)).get("/api/builds").json() == []


@pytest.mark.parametrize(
    ("name", "media"),
    [
        ("report.json", "application/json"),
        ("resolved.toml", "text/plain; charset=utf-8"),
        ("model/body.stl", "model/stl"),
        ("cube.gcode", "text/plain; charset=utf-8"),
    ],
)
def test_build_files(client, name, media):
    res = client.get(f"{BASE}/files/{name}")
    assert res.status_code == 200
    assert res.headers["content-type"] == media
    assert res.headers["content-disposition"].startswith("inline")


def test_build_file_latest_and_download(client):
    res = client.get("/api/builds/glide/v1/latest/files/model/body.stl?download=true")
    assert res.status_code == 200
    assert res.content.startswith(b"solid body")
    assert res.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/files/..%2F..%2F..%2F..%2Fsecret.txt",
        f"{BASE}/files/model/..%2F..%2Freport.json",
        f"{BASE}/files/.hidden",
        f"{BASE}/files/missing.txt",
        "/api/builds/glide/v1/.latest/files/report.json",
        "/api/builds/.jobs/x/y/files/job.log",
    ],
)
def test_build_files_are_confined(client, url):
    assert client.get(url).status_code in (400, 404, 422)


def test_symlink_out_of_builds_is_refused(client, root):
    (root / "builds" / "glide" / "v1" / STAMP / "leak.txt").symlink_to(root / "secret.txt")
    (root / "builds" / "glide" / "escape").symlink_to(root, target_is_directory=True)
    assert client.get(f"{BASE}/files/leak.txt").status_code == 404
    assert client.get("/api/builds/glide/escape/builds/files/secret.txt").status_code == 404


def _header(data: bytes) -> dict:
    magic, version, _flags, length = struct.unpack_from("<4sHHI", data)
    assert (magic, version) == (b"LGTP", 1)
    return json.loads(data[12 : 12 + length])


def test_toolpath_gzip(client, root):
    res = client.get(f"{BASE}/toolpath/cube.gcode")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["content-encoding"] == "gzip"
    assert res.headers["vary"] == "Accept-Encoding"
    header = _header(res.content)  # httpx undid the gzip
    assert header["kind"] == "print" and header["segments"] > 0
    cache = root / "builds" / "glide" / "v1" / STAMP / "cube.gcode.print.bin"
    assert gzip.decompress((cache.parent / (cache.name + ".gz")).read_bytes()) == res.content
    names = [f["name"] for f in client.get("/api/builds").json()[0]["files"]]
    assert "cube.gcode.print.bin" not in names  # caches stay hidden


def test_toolpath_identity_and_kind(client):
    res = client.get(f"{BASE}/toolpath/rough.nc", headers={"Accept-Encoding": "identity"})
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert _header(res.content)["kind"] == "cnc"
    forced = client.get("/api/builds/glide/v1/latest/toolpath/rough.nc?kind=cnc")
    assert forced.status_code == 200
    assert client.get(f"{BASE}/toolpath/rough.nc?kind=laser").status_code == 422


def test_toolpath_header(client):
    header = client.get(f"{BASE}/toolpath/cube.gcode/header").json()
    assert header["kind"] == "print"
    assert header["layer_count"] > 0
    assert header["source"]["name"] == "cube.gcode"
    assert "arrays" in header


def test_toolpath_needs_gcode(client):
    assert client.get(f"{BASE}/toolpath/report.json").status_code == 422
    assert client.get(f"{BASE}/toolpath/missing.gcode").status_code == 404
