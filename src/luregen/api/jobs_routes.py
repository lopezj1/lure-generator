"""/api/jobs: submit, list, inspect, stream (SSE) and cancel CLI jobs."""

import contextlib
from collections.abc import AsyncIterable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from luregen.api.jobs import (
    ID_PATTERN,
    Job,
    JobArgsError,
    JobEvent,
    JobInfo,
    JobManager,
    JobRequest,
)


class JobCreated(BaseModel):
    id: str


JobId = Annotated[str, Path(pattern=ID_PATTERN)]


def get_manager(request: Request) -> JobManager:
    return request.app.state.jobs


Manager = Annotated[JobManager, Depends(get_manager)]


def get_job(job_id: JobId, manager: Manager) -> Job:
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return job


JobDep = Annotated[Job, Depends(get_job)]


def stream_start(
    job: JobDep,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int | None, Query(ge=0, description="Resume after this event id.")] = None,
) -> tuple[Job, int]:
    """Resume point: `Last-Event-ID` (EventSource reconnects) wins over `?after=`.

    A finished job with nothing left to send answers 204, which stops EventSource from
    reconnecting.
    """
    start = after or 0
    if last_event_id:
        with contextlib.suppress(ValueError):
            start = max(0, int(last_event_id))
    if job.final and start >= job.state.last_event_id:
        raise HTTPException(204)
    return job, start


def jobs_router() -> APIRouter:
    router = APIRouter(prefix="/jobs", tags=["jobs"])

    @router.post("", status_code=202)
    async def submit_job(req: JobRequest, manager: Manager) -> JobCreated:
        """Queue a job; it runs `python -m luregen --progress json <cmd> …` in the data root."""
        if not manager.started:
            raise HTTPException(503, "job runner is not running")
        try:
            job = manager.submit(req)
        except JobArgsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JobCreated(id=job.id)

    @router.get("")
    async def list_jobs(
        manager: Manager, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[JobInfo]:
        """Recent jobs first."""
        return [j.info() for j in manager.recent(limit)]

    @router.get("/{job_id}")
    async def get_job_info(job: JobDep) -> JobInfo:
        return job.info()

    @router.get("/{job_id}/history")
    async def job_history(job: JobDep, after: Annotated[int, Query(ge=0)] = 0) -> list[JobEvent]:
        """All events so far (id > `after`) as JSON: the non-streaming twin of /events."""
        return job.events_after(after)

    @router.get(
        "/{job_id}/events",
        response_class=EventSourceResponse,
        responses={204: {"description": "Finished job, nothing after Last-Event-ID."}},
    )
    async def job_events(
        start: Annotated[tuple[Job, int], Depends(stream_start)],
    ) -> AsyncIterable[ServerSentEvent]:
        """SSE stream of `log` / `progress` / `result` / `status` events (data: JSON, id:
        per-job counter). Replays from the start (or after `Last-Event-ID` / `?after=`),
        then follows live, and ends after the final `status` event."""
        job, after = start
        async for ev in job.follow(after):
            yield ServerSentEvent(event=ev.event, id=str(ev.id), data=ev.data)

    @router.delete("/{job_id}")
    async def cancel_job(job: JobDep, manager: Manager) -> JobInfo:
        """Cancel: drop a queued job, or SIGTERM the running job's process group (SIGKILL
        after a timeout). Returns the final state; 409 if the job had already finished."""
        if job.final:
            raise HTTPException(409, f"job {job.id} already {job.state.status}")
        await manager.cancel(job)
        return job.info()

    return router
