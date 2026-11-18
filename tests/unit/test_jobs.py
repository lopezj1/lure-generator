import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the `web` dependency group")

from fastapi.testclient import TestClient  # noqa: E402

from luregen.api.app import create_app  # noqa: E402
from luregen.api.jobs import (  # noqa: E402
    JOBS_DIR,
    BuildJob,
    FabCncJob,
    JobArgsError,
    ProfileJob,
    cli_args,
)

REPO = Path(__file__).resolve().parents[2]

# Fake jobs: a `build` request for spec/<name>.toml runs SCRIPTS[name] with `python -c`.
_PRE = "import json, os, signal, sys, time\n"
_P = "print('@@progress ' + json.dumps({}), file=sys.stderr, flush=True)\n"
SCRIPTS = {
    "ok": _PRE
    + "print('hello', flush=True)\n"
    + _P.format("{'stage': 'envelope', 'frac': 0.5, 'msg': 'lofting'}")
    + "print('@@progress {bad json', file=sys.stderr, flush=True)\n"
    + "print('@@result ' + json.dumps({'build_dir': 'builds/x'}), file=sys.stderr)\n",
    "fail": _PRE + "print('config error: nope', file=sys.stderr)\nsys.exit(3)\n",
    "segv": _PRE + "os.kill(os.getpid(), signal.SIGSEGV)\n",
    "sleep": _PRE + _P.format("{'stage': 'wait'}") + "time.sleep(60)\n",
    "stubborn": _PRE
    + "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    + _P.format("{'stage': 'wait'}")
    + "time.sleep(60)\n",
}


def fake_argv(root: Path, req) -> list[str]:
    cli_args(root, req)  # same validation as real jobs
    return [sys.executable, "-c", SCRIPTS[Path(req.args.variant).stem]]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "spec").mkdir(parents=True)
    for name in SCRIPTS:
        (root / "spec" / f"{name}.toml").write_text("")
    return root


def make_app(root: Path, **manager):
    app = create_app(root)
    app.state.jobs.argv_builder = fake_argv
    for k, v in manager.items():
        setattr(app.state.jobs, k, v)
    return app


def submit(client: TestClient, name: str) -> str:
    res = client.post("/api/jobs", json={"op": "build", "args": {"variant": f"spec/{name}.toml"}})
    assert res.status_code == 202, res.text
    return res.json()["id"]


def wait(client: TestClient, job_id: str, until=lambda j: j["status"] not in ("queued", "running")):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if until(job):
            return job
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for job {job_id}: {job}")


def sse(text: str) -> list[dict]:
    """Parse an SSE body into [{event, id, data}] (ignoring comments)."""
    events = []
    for block in text.strip().split("\n\n"):
        ev = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            ev[key] = json.loads(value) if key == "data" else value
        if ev:
            events.append(ev)
    return events


def test_success_events_and_result(root: Path):
    with TestClient(make_app(root)) as client:
        job_id = submit(client, "ok")
        res = client.get(f"/api/jobs/{job_id}/events")  # follows live until the end
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = sse(res.text)
        job = client.get(f"/api/jobs/{job_id}").json()

    assert [int(e["id"]) for e in events] == list(range(1, len(events) + 1))
    assert [(e["event"], e["data"].get("status")) for e in events if e["event"] == "status"] == [
        ("status", "queued"),
        ("status", "running"),
        ("status", "succeeded"),
    ]
    assert events[-1]["event"] == "status"
    kinds = [e["event"] for e in events]
    assert kinds.index("progress") < kinds.index("result") < len(kinds) - 1
    assert {"stream": "stdout", "line": "hello"} in [e["data"] for e in events]
    progress = next(e["data"] for e in events if e["event"] == "progress")
    assert progress == {"stage": "envelope", "frac": 0.5, "msg": "lofting"}
    # A malformed @@ line is kept as a log line.
    assert {"stream": "stderr", "line": "@@progress {bad json"} in [e["data"] for e in events]

    assert job["status"] == "succeeded" and job["returncode"] == 0
    assert job["result"] == {"build_dir": "builds/x"}
    assert job["progress"]["stage"] == "envelope"
    assert job["last_event_id"] == len(events)
    assert "pid" not in job

    folder = root / JOBS_DIR
    state = json.loads((folder / f"{job_id}.json").read_text())
    assert state["status"] == "succeeded" and state["result"] == {"build_dir": "builds/x"}
    log = (folder / f"{job_id}.log").read_text()
    assert "hello\n" in log and '@@result {"build_dir": "builds/x"}' in log


def test_sse_resume_and_end(root: Path):
    with TestClient(make_app(root, ring_size=3)) as client:
        job_id = submit(client, "ok")
        wait(client, job_id)
        full = sse(client.get(f"/api/jobs/{job_id}/events").text)
        last = len(full)

        # Last-Event-ID (EventSource reconnect) resumes after that id, from the file when
        # the ring no longer holds it (ring_size=3).
        resumed = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "2"})
        assert sse(resumed.text) == full[2:]
        tail = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(last - 1)})
        assert sse(tail.text) == full[-1:]
        assert sse(client.get(f"/api/jobs/{job_id}/events?after=4").text) == full[4:]
        # Header wins over ?after=.
        both = client.get(f"/api/jobs/{job_id}/events?after=1", headers={"Last-Event-ID": "5"})
        assert sse(both.text) == full[5:]
        # Nothing left on a finished job: 204, which stops EventSource reconnecting.
        done = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(last)})
        assert done.status_code == 204

        history = client.get(f"/api/jobs/{job_id}/history").json()
        assert [(h["id"], h["event"], h["data"]) for h in history] == [
            (int(e["id"]), e["event"], e["data"]) for e in full
        ]
        assert client.get(f"/api/jobs/{job_id}/history?after={last - 1}").json() == history[-1:]


def test_failure_returncode(root: Path):
    with TestClient(make_app(root)) as client:
        job = wait(client, submit(client, "fail"))
    assert job["status"] == "failed"
    assert job["returncode"] == 3
    assert job["error"] == "exit code 3: config error: nope"


def test_segfault_is_isolated(root: Path):
    with TestClient(make_app(root)) as client:
        job = wait(client, submit(client, "segv"))
        assert client.get("/api/health").status_code == 200
        after = wait(client, submit(client, "ok"))
    assert job["status"] == "failed"
    assert job["returncode"] == -11
    assert job["error"].startswith("killed by signal SIGSEGV")
    assert after["status"] == "succeeded"


def _state(root: Path, job_id: str) -> dict:
    return json.loads((root / JOBS_DIR / f"{job_id}.json").read_text())


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("name", ["sleep", "stubborn"])
def test_cancel_running(root: Path, name: str):
    with TestClient(make_app(root, kill_timeout=0.5)) as client:
        job_id = submit(client, name)
        queued_id = submit(client, "ok")  # one worker: waits behind the first
        wait(client, job_id, until=lambda j: j["progress"] is not None)
        pid = _state(root, job_id)["pid"]

        t0 = time.monotonic()
        res = client.delete(f"/api/jobs/{job_id}")
        assert res.status_code == 200
        job = res.json()
        assert time.monotonic() - t0 < 5
        assert job["status"] == "cancelled"
        signal_name = "SIGTERM" if name == "sleep" else "SIGKILL"  # stubborn ignores TERM
        assert job["error"] == f"cancelled (killed by signal {signal_name})"
        assert _gone(pid)
        assert client.delete(f"/api/jobs/{job_id}").status_code == 409

        events = sse(client.get(f"/api/jobs/{job_id}/events").text)
        assert events[-1]["data"]["status"] == "cancelled"
        assert wait(client, queued_id)["status"] == "succeeded"


def test_cancel_queued(root: Path):
    with TestClient(make_app(root)) as client:
        running = submit(client, "sleep")
        queued = submit(client, "ok")
        assert client.delete(f"/api/jobs/{queued}").json()["status"] == "cancelled"
        client.delete(f"/api/jobs/{running}")
        assert wait(client, running)["status"] == "cancelled"
        job = client.get(f"/api/jobs/{queued}").json()
    assert job["started_at"] is None and job["error"] == "cancelled before start"
    assert not (root / JOBS_DIR / f"{queued}.log").exists()


def test_restart_recovery(root: Path):
    # Shutdown interrupts the running job (and kills it) and the queued one.
    with TestClient(make_app(root)) as client:
        running = submit(client, "sleep")
        queued = submit(client, "ok")
        wait(client, running, until=lambda j: j["progress"] is not None)
        pid = _state(root, running)["pid"]
    assert _gone(pid)
    assert _state(root, running)["status"] == "interrupted"
    assert _state(root, queued)["status"] == "interrupted"

    # A crash leaves state files saying running/queued: startup marks them interrupted.
    folder = root / JOBS_DIR
    for job_id, status in [(running, "running"), (queued, "queued")]:
        state = _state(root, job_id)
        state["status"] = status
        (folder / f"{job_id}.json").write_text(json.dumps(state))
    (folder / "garbage.json").write_text("{not json")

    with TestClient(make_app(root)) as client:
        jobs = client.get("/api/jobs").json()
        assert [j["id"] for j in jobs] == [queued, running]  # recent first
        assert {j["status"] for j in jobs} == {"interrupted"}
        crashed = client.get(f"/api/jobs/{running}").json()
        assert crashed["error"] == "server stopped while the job was running"
        assert client.get(f"/api/jobs/{queued}").json()["error"] == (
            "server stopped before the job started"
        )
        # Replay comes from the events file and ends with the new status.
        events = sse(client.get(f"/api/jobs/{running}/events").text)
        assert [int(e["id"]) for e in events] == list(range(1, len(events) + 1))
        assert events[-1]["data"]["status"] == "interrupted"
        # New jobs still run after recovery.
        assert wait(client, submit(client, "ok"))["status"] == "succeeded"


def test_api_errors(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.toml"
    outside.write_text("")
    (root / "spec" / "link.toml").symlink_to(outside)
    (root / "spec" / "notes.txt").write_text("")
    (root / "builds" / "glide" / "v" / "b1").mkdir(parents=True)

    def post(body):
        return client.post("/api/jobs", json=body)

    def build(variant, overrides=()):
        return post({"op": "build", "args": {"variant": variant}, "overrides": list(overrides)})

    with TestClient(make_app(root)) as client:
        for variant in ["../outside.toml", str(outside), "spec/link.toml", "spec/nope.toml"]:
            res = build(variant)
            assert res.status_code == 422, variant
            assert "variant" in res.json()["detail"]
        assert build("spec/notes.txt").status_code == 422
        assert build("spec").status_code == 422
        assert build("spec/ok.toml", ["a.b=1\nc=2"]).status_code == 422
        assert build("spec/ok.toml", ["--out-root=/tmp"]).status_code == 422
        assert build("spec/ok.toml", ["a.b=--x"]).status_code == 202  # a value, never a flag
        assert post({"op": "shell", "args": {}}).status_code == 422
        assert post({"op": "build", "args": {"variant": "spec/ok.toml", "x": 1}}).status_code == 422
        fab = {"op": "fab_print", "args": {"build": "builds/glide/v/b1"}, "overrides": ["a=1"]}
        assert post(fab).status_code == 422  # overrides only exist for build
        for build_dir in ["spec", "builds/.jobs", "builds/../..", "builds/nope"]:
            res = post({"op": "fab_cnc", "args": {"build": build_dir}})
            assert res.status_code == 422, build_dir
        res = post({"op": "fab_cnc", "args": {"build": "builds/glide/v/b1", "machine": "-x"}})
        assert res.status_code == 422
        profile = {"op": "profile", "args": {"image": "spec/ok.toml", "out": "../x.csv"}}
        assert post(profile).status_code == 422

        assert client.get("/api/jobs/nope").status_code == 422
        assert client.get("/api/jobs/20260101-000000-abcdef").status_code == 404
        assert client.get("/api/jobs/20260101-000000-abcdef/events").status_code == 404
        assert client.delete("/api/jobs/20260101-000000-abcdef").status_code == 404

    # Without the lifespan (no `with`), the runner is not started.
    body = {"op": "build", "args": {"variant": "spec/ok.toml"}}
    assert TestClient(make_app(root)).post("/api/jobs", json=body).status_code == 503


def test_cli_args(root: Path):
    (root / "builds" / "glide" / "v" / "b1").mkdir(parents=True)
    (root / "builds" / "glide" / "v" / "latest").symlink_to("b1")
    (root / "uploads").mkdir()
    (root / "uploads" / "side.jpg").write_text("")

    req = BuildJob(op="build", args={"variant": "./spec/ok.toml"}, overrides=["size.length_mm=150"])
    assert cli_args(root, req) == ["build", "spec/ok.toml", "--set", "size.length_mm=150"]
    req = FabCncJob(op="fab_cnc", args={"build": "builds/glide/v/latest", "machine": "shapeoko"})
    assert cli_args(root, req) == [
        "fab",
        "builds/glide/v/b1",
        "--format",
        "cnc",
        "--machine",
        "shapeoko",
    ]
    req = ProfileJob(
        op="profile",
        args={"image": "uploads/side.jpg", "out": "uploads/side.csv", "length": 175, "rembg": True},
    )
    assert cli_args(root, req) == [
        "profile",
        "uploads/side.jpg",
        "--out",
        "uploads/side.csv",
        "--length",
        "175.0",
        "--rembg",
    ]
    with pytest.raises(JobArgsError, match="not a .csv"):
        cli_args(root, ProfileJob(op="profile", args={"image": "uploads/side.jpg", "out": "a.txt"}))


def test_real_build_then_fab_stub(tmp_path: Path, monkeypatch):
    """One real `python -m luregen` build job on the sample variant, then `fab` (a stub)."""
    monkeypatch.setenv("LUREGEN_GIT_SHA", "abc1234")
    shutil.copytree(REPO / "spec", tmp_path / "spec")
    variant = "spec/designs/glide/variants/striper_175_petg.toml"
    with TestClient(create_app(tmp_path)) as client:
        res = client.post("/api/jobs", json={"op": "build", "args": {"variant": variant}})
        job_id = res.json()["id"]
        events = sse(client.get(f"/api/jobs/{job_id}/events").text)
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "succeeded", job["error"]
        assert job["command"][1:] == ["-m", "luregen", "--progress", "json", "build", variant]
        build_dir = job["result"]["build_dir"]
        assert build_dir.startswith("builds/glide/striper_175_petg/")
        assert (tmp_path / build_dir / "model" / "body.step").is_file()
        assert any(e["event"] == "result" for e in events)
        assert any(e["data"].get("line", "").startswith("build: ") for e in events)

        res = client.post("/api/jobs", json={"op": "fab_print", "args": {"build": build_dir}})
        fab = wait(client, res.json()["id"])
    assert fab["status"] == "failed" and fab["returncode"] == 2
    assert "Not implemented yet" in fab["error"]
