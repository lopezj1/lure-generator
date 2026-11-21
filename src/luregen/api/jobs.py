"""Job runner: an asyncio queue that runs `python -m luregen --progress json <cmd> …`.

Each job is a subprocess in its own session (process group), so a crash (e.g. an OCCT
segfault) only fails that job and cancel can signal the whole group (slicer included).

Per job, under `<root>/builds/.jobs/`:
- `<id>.json`: state (`JobState`), rewritten on every status change and result;
- `<id>.log`: plain stdout + stderr lines, for humans;
- `<id>.events.jsonl`: every `JobEvent` with its id, for SSE replay (`Last-Event-ID`).

Events: stderr `@@progress {…}` → `progress`, `@@result {…}` → `result`, any other line
→ `log`; status changes → `status`. Ids increase by 1 per job, starting at 1.

Restart policy: jobs found `running` or `queued` at startup are marked `interrupted`
(nothing is re-run behind the user's back; resubmit instead). A `running` job's process
group is sent SIGTERM if its recorded command line still matches.
"""

import asyncio
import contextlib
import json
import os
import re
import secrets
import signal
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from luregen.progress import PROGRESS_PREFIX, RESULT_PREFIX

JOBS_DIR = Path("builds") / ".jobs"
ID_PATTERN = r"^\d{8}-\d{6}-[0-9a-f]{6}$"
_OVERRIDE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*=[^\r\n\0]*$")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


# ---- requests ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileArgs(_Args):
    """`lure profile IMAGE --out OUT …`; paths are relative to the data root."""

    image: str
    out: str = Field(description="Station table CSV to write (inside the data root).")
    scale: Literal["aruco", "paper", "length", "auto"] | None = None
    length: float | None = Field(None, gt=0)
    marker_mm: float | None = Field(None, gt=0)
    paper: str | None = Field(
        None,
        pattern=r"^(letter|a4|\d+(\.\d+)?x\d+(\.\d+)?)$",
        description="Sheet under the subject (paper scale): letter, a4 or <W>x<H> in mm.",
    )
    paper_inset_mm: float | None = Field(None, ge=0, le=30)
    top: str | None = None
    width_ratio: float | None = Field(None, gt=0)
    n_stations: int | None = Field(None, ge=2, le=500)
    nose: Literal["left", "right"] | None = None
    smoothing: Literal["efd", "spline"] | None = None
    harmonics: int | None = Field(None, ge=4, le=60)
    rembg: bool = False


class BuildArgs(_Args):
    """`lure build VARIANT [--set k=v …]`."""

    variant: str = Field(description="Variant TOML, relative to the data root.")


class FabArgs(_Args):
    """`lure fab BUILD --format print|cnc [--machine M]`."""

    build: str = Field(description="Build folder (or its `latest` link) under builds/.")
    machine: str | None = None


class ProfileJob(_Args):
    op: Literal["profile"]
    args: ProfileArgs


class BuildJob(_Args):
    op: Literal["build"]
    args: BuildArgs
    overrides: list[str] = Field(
        default_factory=list, max_length=100, description="`--set` overrides, `a.b=value`."
    )


class FabPrintJob(_Args):
    op: Literal["fab_print"]
    args: FabArgs


class FabCncJob(_Args):
    op: Literal["fab_cnc"]
    args: FabArgs


JobRequest = Annotated[ProfileJob | BuildJob | FabPrintJob | FabCncJob, Field(discriminator="op")]


# ---- state and events -------------------------------------------------------------------


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


FINAL = {JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled, JobStatus.interrupted}


class JobProgress(BaseModel):
    stage: str
    frac: float | None = None
    msg: str = ""


class JobInfo(BaseModel):
    id: str
    op: str
    request: JobRequest
    command: list[str] = Field(description="The CLI command line (cwd = data root).")
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    progress: JobProgress | None = None
    last_event_id: int = 0


class JobState(JobInfo):
    """What `<id>.json` holds: the API view plus the process id."""

    pid: int | None = None


class LogData(BaseModel):
    stream: Literal["stdout", "stderr"]
    line: str


class StatusData(BaseModel):
    status: JobStatus
    returncode: int | None = None
    error: str | None = None


class LogEvent(BaseModel):
    id: int
    event: Literal["log"] = "log"
    data: LogData


class ProgressEvent(BaseModel):
    id: int
    event: Literal["progress"] = "progress"
    data: JobProgress


class StatusEvent(BaseModel):
    id: int
    event: Literal["status"] = "status"
    data: StatusData


class ResultEvent(BaseModel):
    id: int
    event: Literal["result"] = "result"
    data: dict[str, Any]


JobEvent = Annotated[
    LogEvent | ProgressEvent | StatusEvent | ResultEvent, Field(discriminator="event")
]


_EVENT = TypeAdapter(JobEvent)


def parse_event(line: str) -> LogEvent | ProgressEvent | StatusEvent | ResultEvent:
    return _EVENT.validate_json(line)


# ---- validation + argv ------------------------------------------------------------------


class JobArgsError(ValueError):
    """A request names a path outside the data root, a missing file or a bad flag."""


def _inside(root: Path, rel: str, what: str, *, kind: str | None = None) -> str:
    """Resolve `rel` against `root`; it must stay inside. Returns the root-relative path."""
    if not rel or "\0" in rel:
        raise JobArgsError(f"{what}: empty or invalid path")
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise JobArgsError(f"{what}: {rel!r} is outside the data root")
    if kind == "file" and not path.is_file():
        raise JobArgsError(f"{what}: {rel!r} is not a file")
    if kind == "dir" and not path.is_dir():
        raise JobArgsError(f"{what}: {rel!r} is not a folder")
    return str(path.relative_to(root))


def cli_args(root: Path, req: Any) -> list[str]:
    """Validate `req` against `root` and return the `lure` arguments (after `--progress`)."""
    root = root.resolve()
    a = req.args
    match req.op:
        case "build":
            variant = _inside(root, a.variant, "variant", kind="file")
            if not variant.endswith(".toml"):
                raise JobArgsError(f"variant: {a.variant!r} is not a .toml file")
            argv = ["build", variant]
            for o in req.overrides:
                if not _OVERRIDE.match(o):
                    raise JobArgsError(f"override {o!r}: expected key.path=value")
                argv += ["--set", o]
            return argv
        case "fab_print" | "fab_cnc":
            build = _inside(root, a.build, "build", kind="dir")
            if not Path(build).is_relative_to("builds") or Path(build).is_relative_to(JOBS_DIR):
                raise JobArgsError(f"build: {a.build!r} is not a build folder under builds/")
            argv = ["fab", build, "--format", "print" if req.op == "fab_print" else "cnc"]
            if a.machine is not None:
                if not _NAME.match(a.machine):
                    raise JobArgsError(f"machine: {a.machine!r} is not a machine folder name")
                argv += ["--machine", a.machine]
            return argv
        case "profile":
            argv = ["profile", _inside(root, a.image, "image", kind="file")]
            out = _inside(root, a.out, "out")
            if not out.endswith(".csv"):
                raise JobArgsError(f"out: {a.out!r} is not a .csv path")
            argv += ["--out", out]
            if a.top is not None:
                argv += ["--top", _inside(root, a.top, "top", kind="file")]
            for flag, value in [
                ("--scale", a.scale),
                ("--length", a.length),
                ("--marker-mm", a.marker_mm),
                ("--paper", a.paper),
                ("--paper-inset-mm", a.paper_inset_mm),
                ("--width-ratio", a.width_ratio),
                ("--n-stations", a.n_stations),
                ("--nose", a.nose),
                ("--smoothing", a.smoothing),
                ("--harmonics", a.harmonics),
            ]:
                if value is not None:
                    argv += [flag, str(value)]
            if a.rembg:
                argv.append("--rembg")
            return argv
    raise JobArgsError(f"unknown op {req.op!r}")  # pragma: no cover (pydantic rejects it)


def default_argv(root: Path, req: Any) -> list[str]:
    return [sys.executable, "-m", "luregen", "--progress", "json", *cli_args(root, req)]


ArgvBuilder = Callable[[Path, Any], list[str]]


# ---- jobs -------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _describe_rc(rc: int) -> str:
    if rc < 0:
        try:
            return f"killed by signal {signal.Signals(-rc).name}"
        except ValueError:
            return f"killed by signal {-rc}"
    return f"exit code {rc}"


class Job:
    def __init__(self, state: JobState, folder: Path, ring_size: int) -> None:
        self.state = state
        self.folder = folder
        self.ring: deque[Any] = deque(maxlen=ring_size)
        self.proc: asyncio.subprocess.Process | None = None
        self.stop_as: JobStatus | None = None  # cancelled / interrupted, when requested
        self.done = asyncio.Event()
        self._changed = asyncio.Event()
        if state.status in FINAL:
            self.done.set()

    @property
    def id(self) -> str:
        return self.state.id

    @property
    def final(self) -> bool:
        return self.state.status in FINAL

    def info(self) -> JobInfo:
        return JobInfo.model_validate(self.state.model_dump(exclude={"pid"}))

    def path(self, suffix: str) -> Path:
        return self.folder / f"{self.id}{suffix}"

    def save(self) -> None:
        tmp = self.path(".json.tmp")
        tmp.write_text(self.state.model_dump_json(indent=2) + "\n")
        tmp.replace(self.path(".json"))

    def add(self, cls: type, data: Any) -> None:
        self.state.last_event_id += 1
        ev = cls(id=self.state.last_event_id, data=data)
        self.ring.append(ev)
        with self.path(".events.jsonl").open("a") as f:
            f.write(ev.model_dump_json() + "\n")
        self._changed.set()
        self._changed = asyncio.Event()

    def set_status(self, status: JobStatus, *, returncode: int | None = None, error=None):
        s = self.state
        s.status, s.returncode, s.error = status, returncode, error
        if status is JobStatus.running:
            s.started_at = _now()
        elif status in FINAL:
            s.finished_at = _now()
        self.add(StatusEvent, StatusData(status=status, returncode=returncode, error=error))
        self.save()
        if status in FINAL:
            self.done.set()

    def events_after(self, after: int) -> list[Any]:
        if self.ring and self.ring[0].id <= after + 1:
            return [e for e in self.ring if e.id > after]
        events = []
        try:
            with self.path(".events.jsonl").open() as f:
                for line in f:
                    try:
                        ev = parse_event(line)
                    except ValidationError:
                        continue
                    if ev.id > after:
                        events.append(ev)
        except FileNotFoundError:
            pass
        return events

    async def follow(self, after: int = 0) -> AsyncIterator[Any]:
        """Yield events with id > `after`, live, until the final status has been sent."""
        while True:
            changed = self._changed
            for ev in self.events_after(after):
                yield ev
                after = ev.id
            if self.final and after >= self.state.last_event_id:
                return
            if after >= self.state.last_event_id:
                await changed.wait()


class JobManager:
    """One queue, `concurrency` workers; state on disk under `<root>/builds/.jobs/`."""

    def __init__(
        self,
        root: Path,
        *,
        concurrency: int | None = None,
        argv_builder: ArgvBuilder = default_argv,
        kill_timeout: float = 5.0,
        ring_size: int = 1000,
    ) -> None:
        self.root = root.resolve()
        self.folder = self.root / JOBS_DIR
        self.concurrency = concurrency or int(os.environ.get("LUREGEN_JOB_CONCURRENCY", "1"))
        self.argv_builder = argv_builder
        self.kill_timeout = kill_timeout
        self.ring_size = ring_size
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[Job] | None = None
        self._workers: list[asyncio.Task] = []
        self._stopping = False

    @property
    def started(self) -> bool:
        return self._queue is not None and not self._stopping

    # -- lifecycle

    async def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        for file in sorted(self.folder.glob("*.json")):
            try:
                state = JobState.model_validate_json(file.read_text())
            except (ValidationError, OSError):
                continue
            job = Job(state, self.folder, self.ring_size)
            self.jobs[job.id] = job
            if not job.final:
                self._recover(job)
        self._queue = asyncio.Queue()
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    def _recover(self, job: Job) -> None:
        was = job.state.status
        if was is JobStatus.running and job.state.pid:
            self._kill_orphan(job.state.pid, job.state.command)
        job.set_status(
            JobStatus.interrupted,
            error="server stopped while the job was running"
            if was is JobStatus.running
            else "server stopped before the job started",
        )

    @staticmethod
    def _kill_orphan(pid: int, command: list[str]) -> None:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return
        if cmdline.rstrip(b"\0").split(b"\0") == [c.encode() for c in command]:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)

    async def stop(self) -> None:
        """Interrupt running jobs (SIGTERM, then SIGKILL) and mark queued ones interrupted."""
        if self._queue is None:
            return
        self._stopping = True
        running = [j for j in self.jobs.values() if j.proc is not None and not j.final]
        await asyncio.gather(*(self._terminate(j, JobStatus.interrupted) for j in running))
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        for job in self.jobs.values():
            if not job.final:
                job.set_status(JobStatus.interrupted, error="server stopped")
        self._queue = None
        self._stopping = False

    # -- API

    def submit(self, req: Any) -> Job:
        if self._queue is None or self._stopping:
            raise RuntimeError("job runner is not running")
        argv = self.argv_builder(self.root, req)  # raises JobArgsError
        state = JobState(
            id=_new_id(),
            op=req.op,
            request=req,
            command=argv,
            status=JobStatus.queued,
            created_at=_now(),
        )
        while state.id in self.jobs:
            state.id = _new_id()
        job = Job(state, self.folder, self.ring_size)
        self.jobs[job.id] = job
        job.set_status(JobStatus.queued)
        self._queue.put_nowait(job)
        return job

    def recent(self, limit: int = 50) -> list[Job]:
        jobs = sorted(self.jobs.values(), key=lambda j: (j.state.created_at, j.id), reverse=True)
        return jobs[:limit]

    async def cancel(self, job: Job) -> None:
        if job.final:
            return
        if job.proc is None:  # still queued: the worker skips it
            job.set_status(JobStatus.cancelled, error="cancelled before start")
            return
        await self._terminate(job, JobStatus.cancelled)

    # -- internals

    async def _terminate(self, job: Job, as_status: JobStatus) -> None:
        job.stop_as = as_status
        proc = job.proc
        if proc is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
            try:
                await asyncio.wait_for(asyncio.shield(job.done.wait()), self.kill_timeout)
                return
            except TimeoutError:
                continue
        await job.done.wait()

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            try:
                if not job.final and not self._stopping:
                    await self._run(job)
            finally:
                self._queue.task_done()

    async def _run(self, job: Job) -> None:
        argv = job.state.command
        log = job.path(".log").open("a", encoding="utf-8")
        log.write("$ " + " ".join(argv) + "\n")
        job.set_status(JobStatus.running)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError as exc:
            log.close()
            job.set_status(JobStatus.failed, error=f"could not start: {exc}")
            return
        job.proc = proc
        job.state.pid = proc.pid
        job.save()
        try:
            assert proc.stdout is not None and proc.stderr is not None
            await asyncio.gather(
                self._pump(job, proc.stdout, "stdout", log),
                self._pump(job, proc.stderr, "stderr", log),
            )
            rc = await proc.wait()
        except asyncio.CancelledError:
            # Server shutdown: stop() terminates the process and marks the job.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
        finally:
            log.close()
        if job.stop_as is not None:
            reason = "cancelled" if job.stop_as is JobStatus.cancelled else "server stopped"
            job.set_status(job.stop_as, returncode=rc, error=f"{reason} ({_describe_rc(rc)})")
        elif rc == 0:
            job.set_status(JobStatus.succeeded, returncode=0)
        else:
            tail = [
                e.data.line
                for e in job.ring
                if isinstance(e, LogEvent) and e.data.stream == "stderr" and e.data.line.strip()
            ]
            error = _describe_rc(rc) + (f": {tail[-1]}" if tail else "")
            job.set_status(JobStatus.failed, returncode=rc, error=error)

    async def _pump(self, job: Job, stream: asyncio.StreamReader, name: str, log) -> None:
        buf = b""
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self._line(job, name, raw.decode("utf-8", "replace").rstrip("\r"), log)
        if buf:
            self._line(job, name, buf.decode("utf-8", "replace").rstrip("\r"), log)

    def _line(self, job: Job, stream: str, line: str, log) -> None:
        log.write(line + "\n")
        log.flush()
        if stream == "stderr" and line.startswith((PROGRESS_PREFIX, RESULT_PREFIX)):
            prefix = PROGRESS_PREFIX if line.startswith(PROGRESS_PREFIX) else RESULT_PREFIX
            try:
                data = json.loads(line[len(prefix) :])
                if prefix == PROGRESS_PREFIX:
                    progress = JobProgress.model_validate(data)
                    job.state.progress = progress
                    job.add(ProgressEvent, progress)
                else:
                    if not isinstance(data, dict):
                        raise ValueError("result must be an object")
                    job.state.result = {**(job.state.result or {}), **data}
                    job.add(ResultEvent, data)
                    job.save()
                return
            except (ValueError, ValidationError):
                pass  # malformed: keep it as a log line
        job.add(LogEvent, LogData(stream=stream, line=line))
