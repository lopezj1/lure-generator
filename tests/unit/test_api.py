import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from luregen import __version__
from luregen.cli import make_app

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def test_health(monkeypatch):
    monkeypatch.setenv("LUREGEN_GIT_SHA", "abc1234")
    client = TestClient(create_app(REPO))
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["git_sha"]


def test_spa_mounted_when_dist_exists(tmp_path: Path):
    assert TestClient(create_app(tmp_path)).get("/").status_code == 404
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<p>spa</p>")
    client = TestClient(create_app(tmp_path))
    assert client.get("/").text == "<p>spa</p>"
    assert client.get("/api/health").status_code == 200


def test_dump_openapi(tmp_path: Path):
    out = tmp_path / "openapi.json"
    res = CliRunner().invoke(
        make_app(), ["serve", "--root", str(tmp_path), "--dump-openapi", str(out)]
    )
    assert res.exit_code == 0, res.output
    schema = json.loads(out.read_text())
    assert "/api/health" in schema["paths"]


def test_committed_openapi_is_current(tmp_path: Path):
    """web/openapi.json must match the app (regenerate with `lure serve --dump-openapi`)."""
    committed = json.loads((REPO / "web" / "openapi.json").read_text())
    assert committed == create_app(tmp_path).openapi()
