"""The submission and status API.

Handlers are plain ``def`` (not ``async def``) on purpose: they touch SQLite,
and FastAPI runs synchronous handlers on a threadpool instead of blocking the
event loop the worker needs.

Endpoints and models are *read* here but not written: creating them belongs to
the authenticated admin panel (M5), and a write API without that authentication
in place would be a hole that is easy to forget later.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.build.guard import PromptRejected
from app.build.orchestrator import Target
from app.container import Container
from app.domain.errors import ValidationError
from app.domain.status import BuildStatus, JobState

router = APIRouter(prefix="/api", tags=["build"])


class TargetBody(BaseModel):
    endpoint_id: int
    model_row_id: int


class SubmitBody(BaseModel):
    body: str = Field(min_length=1)
    title: str | None = None
    summary: str = ""
    targets: list[TargetBody] = Field(min_length=1)


def _container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - startup ordering guard
        raise HTTPException(503, "application is not initialised")
    return container


@router.get("/targets")
def list_targets(request: Request) -> list[dict]:
    container = _container(request)
    return [
        {
            "endpoint_id": endpoint.id,
            "endpoint_name": endpoint.name,
            "model_row_id": model.id,
            "model_id": model.model_id,
            "label": model.label,
        }
        for endpoint, model in container.endpoints.enabled_targets()
    ]


@router.post("/prompts", status_code=202)
def submit_prompt(payload: SubmitBody, request: Request) -> dict:
    container = _container(request)
    targets = [Target(t.endpoint_id, t.model_row_id) for t in payload.targets]
    try:
        result = container.pipeline.submit(
            body=payload.body, title=payload.title, summary=payload.summary, targets=targets
        )
    except PromptRejected as exc:
        # Deliberately vague to the caller: describing the pattern that matched
        # teaches how to evade it. The rule id is kept in guard_log for the admin.
        raise HTTPException(422, detail="prompt was refused by the submission guard") from exc
    except ValidationError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    return {
        "prompt": {"id": result.prompt.id, "slug": result.prompt.slug},
        "builds": [{"id": b.id, "status": str(b.status)} for b in result.builds],
        "jobs": [{"id": j.id, "state": str(j.state)} for j in result.jobs],
    }


@router.get("/builds/{build_id}")
def get_build(build_id: int, request: Request) -> dict:
    container = _container(request)
    build = container.builds.get(build_id)
    if build is None:
        raise HTTPException(404, "no such build")
    version = container.builds.latest_version(build_id)
    return {
        "id": build.id,
        "prompt_id": build.prompt_id,
        "endpoint_id": build.endpoint_id,
        "model_row_id": build.model_row_id,
        "status": str(build.status),
        "latest_version": build.latest_version,
        "tags": list(build.tags),
        "version": None
        if version is None
        else {
            "id": version.id,
            "version_no": version.version_no,
            "bytes": len(version.html.encode()),
            "model_returned": version.model_returned,
            "finish_reason": version.finish_reason,
            "latency_ms": version.latency_ms,
            "usage": version.usage,
        },
        "failures": [
            {"stage": f["stage"], "error": f["error_text"], "violations": f["violations"]}
            for f in container.builds.failures(build_id)
        ],
    }


@router.get("/jobs")
def job_status(request: Request) -> dict:
    container = _container(request)
    return {
        "worker": container.worker.status(),
        "recent": [
            {"id": job.id, "kind": str(job.kind), "state": str(job.state), "error": job.error}
            for job in container.jobs.recent(20)
        ],
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request) -> dict:
    container = _container(request)
    job = container.pipeline.request_cancel(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return {"id": job.id, "state": str(job.state)}


@router.get("/statuses")
def statuses() -> dict:
    """The vocabulary the API uses, so clients do not hardcode it."""
    return {
        "build": [str(s) for s in BuildStatus],
        "job": [str(s) for s in JobState],
    }
