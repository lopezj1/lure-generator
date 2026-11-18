"""W6 (#67): uploaded G-code served as binary toolpaths."""

import gzip
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402
from luregen.viewer.binfmt import decode  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
MULTI = FIXTURES / "orca_u1_multimaterial_excerpt.gcode"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


def _upload(client: TestClient, path: Path) -> str:
    res = client.post("/api/uploads?kind=gcode", files={"file": (path.name, path.read_bytes())})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_upload_toolpath_gzip(client, tmp_path):
    uid = _upload(client, MULTI)
    res = client.get(f"/api/uploads/{uid}/toolpath")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["content-encoding"] == "gzip"
    tp = decode(res.content)  # TestClient already inflated it
    assert tp.kind == "print"
    assert tp.meta["layer_count"] == len(tp.meta["layers"]) - 1
    assert any(m["type"] == "tool_change" for m in tp.meta["markers"])
    assert (tmp_path / "uploads" / f"{uid}.gcode.print.bin.gz").is_file()


def test_upload_toolpath_identity_matches_header(client):
    uid = _upload(client, FIXTURES / "fusion_3drough_excerpt.nc")
    res = client.get(f"/api/uploads/{uid}/toolpath", headers={"Accept-Encoding": "identity"})
    assert res.status_code == 200 and "content-encoding" not in res.headers
    assert res.content[:4] == b"LGTP"
    header = client.get(f"/api/uploads/{uid}/toolpath/header").json()
    assert header["kind"] == "cnc"
    assert header["segments"] == decode(res.content).n_segments
    assert client.get(f"/api/uploads/{uid}/toolpath?kind=print").status_code == 200
    assert client.get(f"/api/uploads/{uid}/toolpath?kind=laser").status_code == 422


def test_upload_toolpath_not_found(client, tmp_path):
    missing = "0" * 64
    assert client.get(f"/api/uploads/{missing}/toolpath").status_code == 404
    # Rejected either by this router (404/422) or by the uploads catch-all route (400).
    assert client.get("/api/uploads/..%2Fsecret/toolpath").status_code in (400, 404, 422)
    assert client.get("/api/uploads/ABC/toolpath/header").status_code == 422
    # A photo upload with the same id scheme is not G-code.
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / f"{'1' * 64}.png").write_bytes(b"\x89PNG")
    assert client.get(f"/api/uploads/{'1' * 64}/toolpath").status_code == 404


def test_upload_toolpath_raw_gzip_bytes(client):
    uid = _upload(client, MULTI)
    with client.stream("GET", f"/api/uploads/{uid}/toolpath") as res:
        raw = b"".join(res.iter_raw())
    assert gzip.decompress(raw)[:4] == b"LGTP"
