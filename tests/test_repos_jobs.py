"""The durable job queue: claiming, cancelling and crash recovery."""

from __future__ import annotations

import pytest

from app.domain.errors import TransitionError
from app.domain.status import JobKind, JobState
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.jobs import JobRepo
from tests.helpers import make_endpoint, make_prompt


@pytest.fixture
def jobs(db: Database) -> JobRepo:
    return JobRepo(db)


def test_enqueue_starts_queued_with_no_attempts(jobs: JobRepo) -> None:
    job = jobs.enqueue(JobKind.BUILD)
    assert job.state is JobState.QUEUED
    assert job.attempts == 0
    assert job.run_after


def test_claim_next_runs_the_oldest_due_job_and_counts_the_attempt(jobs: JobRepo) -> None:
    first = jobs.enqueue(JobKind.BUILD)
    second = jobs.enqueue(JobKind.REFINE)

    claimed = jobs.claim_next()
    assert claimed is not None and claimed.id == first.id
    assert claimed.state is JobState.RUNNING
    assert claimed.attempts == 1

    again = jobs.claim_next()
    assert again is not None and again.id == second.id
    assert jobs.claim_next() is None


def test_a_future_run_after_holds_a_job_back(jobs: JobRepo) -> None:
    scheduled = jobs.enqueue(JobKind.BUILD, run_after="2999-01-01T00:00:00+00:00")
    assert jobs.claim_next() is None
    assert jobs.get(scheduled.id).state is JobState.QUEUED  # type: ignore[union-attr]


def test_two_workers_cannot_claim_the_same_job(jobs: JobRepo) -> None:
    job = jobs.enqueue(JobKind.BUILD)
    claimed = [jobs.claim_next() for _ in range(3)]
    ids = [c.id for c in claimed if c is not None]
    assert ids == [job.id]


def test_only_legal_job_transitions_are_allowed(jobs: JobRepo) -> None:
    job = jobs.enqueue(JobKind.BUILD)
    jobs.claim_next()
    with pytest.raises(TransitionError):
        jobs.transition(job.id, JobState.QUEUED)
    done = jobs.transition(job.id, JobState.DONE)
    assert done.state is JobState.DONE
    with pytest.raises(TransitionError):
        jobs.transition(job.id, JobState.RUNNING)


def test_failure_reason_is_kept_on_the_row(jobs: JobRepo) -> None:
    job = jobs.enqueue(JobKind.PROBE)
    jobs.claim_next()
    failed = jobs.transition(job.id, JobState.FAILED, error="upstream 502")
    assert failed.error == "upstream 502"


def test_cancel_is_cooperative_for_running_jobs(jobs: JobRepo) -> None:
    queued = jobs.enqueue(JobKind.BUILD)
    assert jobs.request_cancel(queued.id).state is JobState.CANCELLED  # type: ignore[union-attr]

    running = jobs.enqueue(JobKind.BUILD)
    jobs.claim_next()
    cancelling = jobs.request_cancel(running.id)
    assert cancelling.state is JobState.CANCELLING  # type: ignore[union-attr]
    assert jobs.cancel_requested(running.id) is True
    assert jobs.cancel_requested(queued.id) is False


def test_interrupted_jobs_are_recovered_and_can_be_requeued(jobs: JobRepo) -> None:
    running = jobs.enqueue(JobKind.BUILD)
    jobs.claim_next()
    untouched = jobs.enqueue(JobKind.BUILD)

    recovered = jobs.recover_interrupted()
    assert [j.id for j in recovered] == [running.id]
    assert jobs.get(running.id).state is JobState.INTERRUPTED  # type: ignore[union-attr]
    assert jobs.get(untouched.id).state is JobState.QUEUED  # type: ignore[union-attr]

    requeued = jobs.requeue(running.id)
    assert requeued.state is JobState.QUEUED and requeued.attempts == 1


def test_pending_count_ignores_finished_work(jobs: JobRepo) -> None:
    jobs.enqueue(JobKind.BUILD)
    jobs.enqueue(JobKind.BUILD)
    first = jobs.claim_next()
    second = jobs.claim_next()

    jobs.transition(second.id, JobState.DONE)
    assert jobs.pending_count() == 1

    jobs.request_cancel(first.id)
    assert jobs.pending_count() == 1  # cancelling still counts: it may be in flight


def test_jobs_can_be_traced_back_to_their_build(db: Database, jobs: JobRepo) -> None:
    endpoint, models = make_endpoint(db)
    prompt = make_prompt(db)
    build = BuildRepo(db).create(
        prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id
    )
    job = jobs.enqueue(JobKind.BUILD, build_id=build.id, endpoint_id=endpoint.id)
    linked = jobs.for_build(build.id)
    assert [j.id for j in linked] == [job.id]
    assert linked[0].build_id == build.id


def test_recent_lists_newest_first(jobs: JobRepo) -> None:
    ids = [jobs.enqueue(JobKind.DISCOVER).id for _ in range(3)]
    assert [j.id for j in jobs.recent(2)] == list(reversed(ids))[:2]
    assert jobs.recent(10, state=JobState.QUEUED)
