"""The worker loop: claiming, concurrency limits, recovery, and crash containment."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from app.build.orchestrator import BuildPipeline, Target
from app.domain.status import BuildStatus, JobState
from app.storage.db import Database
from app.storage.repos.jobs import JobRepo
from app.worker.queue import BuildWorker
from tests.fakes import FakeProvider
from tests.helpers import make_endpoint

GOOD = "<!DOCTYPE html><html><head><title>ok</title></head><body><b>b</b></body></html>"


def _pipeline(db: Database, settings, provider: FakeProvider) -> BuildPipeline:  # noqa: ANN001
    return BuildPipeline(db, replace(settings, worker_poll_interval_ms=100), provider_for=lambda e: provider)


def _worker(db: Database, pipeline: BuildPipeline, settings) -> BuildWorker:  # noqa: ANN001
    return BuildWorker(jobs=pipeline.jobs, builds=pipeline.builds, pipeline=pipeline, settings=settings)


async def _wait_for(predicate, timeout: float = 5.0) -> bool:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_the_worker_runs_queued_jobs_through_to_publication(
    db: Database, settings
) -> None:
    fake = FakeProvider(responses=[GOOD])
    pipeline = _pipeline(db, settings, fake)
    endpoint, models = make_endpoint(db, name="worker-ep")
    prompt = pipeline.submit(
        body="a palette generator with click to copy",
        targets=[Target(endpoint.id, models[0].id)],
    )
    worker = _worker(db, pipeline, settings)
    await worker.start()
    try:
        assert await _wait_for(lambda: bool(pipeline.builds.list()))
    finally:
        await worker.stop()

    build = pipeline.builds.list()[0]
    assert build.status is BuildStatus.PUBLISHED
    assert pipeline.jobs.get(prompt.jobs[0].id).state is JobState.DONE  # type: ignore[union-attr]


async def test_the_concurrency_cap_is_real_and_is_actually_used(
    db: Database, settings
) -> None:
    slow = FakeProvider(responses=[GOOD], delay_s=0.2)
    pipeline = _pipeline(db, replace(settings, worker_concurrency=2), slow)
    endpoint, models = make_endpoint(db, name="cap-ep", models=("m1",))
    for n in range(4):
        pipeline.submit(body=f"a counter app number {n}", targets=[Target(endpoint.id, models[0].id)])

    worker = _worker(db, pipeline, replace(settings, worker_concurrency=2))
    await worker.start()
    try:
        assert await _wait_for(lambda: len(pipeline.builds.list()) == 4)
    finally:
        await worker.stop()

    assert slow.max_in_flight <= 2, "the cap was exceeded"
    assert slow.max_in_flight == 2, "the cap should actually be reached, not serialised"


async def test_a_crashing_job_does_not_take_the_loop_down(
    db: Database, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProvider(responses=[GOOD])
    pipeline = _pipeline(db, settings, fake)
    endpoint, models = make_endpoint(db, name="crash-ep")
    pipeline.submit(body="a thing that crashes the worker", targets=[Target(endpoint.id, models[0].id)])

    def explode(job):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "run_job", explode)
    worker = _worker(db, pipeline, settings)
    await worker.start()
    try:
        assert await _wait_for(lambda: JobRepo(db).recent(1)[0].state is JobState.FAILED)
    finally:
        await worker.stop()

    assert "boom" in JobRepo(db).recent(1)[0].error


def test_interrupted_jobs_become_visible_failures(db: Database, settings) -> None:
    fake = FakeProvider(responses=[GOOD])
    pipeline = _pipeline(db, settings, fake)
    endpoint, models = make_endpoint(db, name="recov-ep")
    result = pipeline.submit(body="an interrupted build", targets=[Target(endpoint.id, models[0].id)])
    claimed = pipeline.jobs.claim_next()
    pipeline.builds.set_status(result.builds[0].id, BuildStatus.GENERATING)

    worker = _worker(db, pipeline, settings)
    assert worker.recover_interrupted() == 1

    assert pipeline.jobs.get(claimed.id).state is JobState.INTERRUPTED  # type: ignore[union-attr]
    assert pipeline.builds.get(result.builds[0].id).status is BuildStatus.FAILED
    failures = pipeline.builds.failures(result.builds[0].id)
    assert failures[0]["stage"] == "interrupted"
    # A second recovery has nothing left to do.
    assert worker.recover_interrupted() == 0


async def test_start_is_idempotent_and_stop_is_safe(db: Database, settings) -> None:
    fake = FakeProvider(responses=[GOOD])
    pipeline = _pipeline(db, settings, fake)
    worker = _worker(db, pipeline, settings)
    await worker.start()
    await worker.start()
    assert worker.status()["pending"] == 0
    await worker.stop()
    await worker.stop()
    assert worker.running_job_ids == ()
