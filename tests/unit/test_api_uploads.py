"""W1 (#62): uploads are content-checked, size-capped and stored by sha256."""

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api import uploads  # noqa: E402
from luregen.api.app import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
PRESET = REPO / "spec/designs/glide/profiles/presets/placeholder_175.csv"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def _png() -> bytes:
    img = np.full((40, 80, 3), 235, np.uint8)
    cv2.ellipse(img, (40, 20), (30, 10), 0, 0, 360, (60, 60, 60), -1)
    return cv2.imencode(".png", img)[1].tobytes()


def _post(client: TestClient, kind: str, data: bytes, filename: str = "f"):
    return client.post(f"/api/uploads?kind={kind}", files={"file": (filename, data)})


def test_photo_upload(client, tmp_path):
    data = _png()
    res = _post(client, "photo", data, "side.png")
    assert res.status_code == 200, res.text
    sha = hashlib.sha256(data).hexdigest()
    assert res.json() == {
        "id": sha,
        "kind": "photo",
        "size": len(data),
        "path": f"uploads/{sha}.png",
    }
    assert (tmp_path / "uploads" / f"{sha}.png").read_bytes() == data
    assert _post(client, "photo", data).json()["path"] == f"uploads/{sha}.png"  # idempotent


def test_jpeg_extension_comes_from_content(client):
    data = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
    assert _post(client, "photo", data, "photo.png").json()["path"].endswith(".jpg")


def test_csv_upload(client):
    res = _post(client, "csv", PRESET.read_bytes(), "p.csv")
    assert res.status_code == 200, res.text
    assert res.json()["path"].endswith(".csv")


@pytest.mark.parametrize(
    ("name", "ext"),
    [("orca_2.5.1_cube.gcode", "gcode"), ("fusion_3drough_excerpt.nc", "nc")],
)
def test_gcode_upload(client, name, ext):
    res = _post(client, "gcode", (FIXTURES / name).read_bytes(), name)
    assert res.status_code == 200, res.text
    assert res.json()["path"].endswith(f".{ext}")


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("photo", b"not an image at all"),
        ("photo", b"\x89PNG\r\n\x1a\n" + b"\0" * 64),  # PNG magic, broken body
        ("csv", b"x,y\n1,2\n"),
        ("csv", b"\xff\xfe\x00binary"),
        ("gcode", b"\x00\x01\x02 binary"),
        ("gcode", b"hello world\nthis is prose\n"),
    ],
)
def test_content_checks(client, tmp_path, kind, data):
    res = _post(client, kind, data)
    assert res.status_code == 422
    assert not (tmp_path / "uploads").exists() or not any((tmp_path / "uploads").iterdir())


def test_size_cap(client, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD", 1000)
    monkeypatch.setattr(uploads, "_MULTIPART_SLACK", 10**6)  # exercise the streamed count
    assert _post(client, "gcode", b"G1 X1\n" * 400).status_code == 413
    monkeypatch.setattr(uploads, "_MULTIPART_SLACK", 0)  # and the Content-Length check
    assert _post(client, "gcode", b"G1 X1\n" * 400).status_code == 413


def test_bad_kind(client):
    assert _post(client, "exe", b"MZ").status_code == 422


def test_get_upload_serves_files_inside_uploads(client, tmp_path):
    data = _png()
    path = _post(client, "photo", data, "side.png").json()["path"]
    res = client.get(f"/api/{path}")
    assert res.status_code == 200 and res.content == data
    assert res.headers["content-type"] == "image/png"
    nested = tmp_path / "uploads/profiles/abc/profile.csv"
    nested.parent.mkdir(parents=True)
    nested.write_text("x\n")
    res = client.get("/api/uploads/profiles/abc/profile.csv")
    assert res.status_code == 200 and res.text == "x\n"
    assert client.get("/api/uploads/nope.png").status_code == 404
    (tmp_path / "secret.txt").write_text("no")
    for bad in ("../secret.txt", "..%2Fsecret.txt", ".hidden", "a//b"):
        assert client.get(f"/api/uploads/{bad}").status_code in (400, 404)


def test_list_uploads(client, tmp_path):
    assert client.get("/api/uploads").json() == []
    photo = _post(client, "photo", _png(), "side.png").json()
    csv = _post(client, "csv", PRESET.read_bytes(), "p.csv").json()
    job_out = tmp_path / "uploads/profiles/abc/profile.csv"
    job_out.parent.mkdir(parents=True)
    job_out.write_bytes(PRESET.read_bytes())
    (job_out.parent / "profile_debug.png").write_bytes(_png())
    paths = {u["path"] for u in client.get("/api/uploads").json()}
    assert paths == {photo["path"], csv["path"], "uploads/profiles/abc/profile.csv"}
    csvs = client.get("/api/uploads?kind=csv").json()
    assert {u["path"] for u in csvs} == {csv["path"], "uploads/profiles/abc/profile.csv"}
    assert [u["path"] for u in client.get("/api/uploads?kind=photo").json()] == [photo["path"]]
