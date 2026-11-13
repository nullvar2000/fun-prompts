"""State machines and slug rules, tested without any I/O."""

from __future__ import annotations

import pytest

from app.domain.errors import TransitionError
from app.domain.slugs import slugify, unique_slug
from app.domain.status import (
    BuildStatus,
    JobState,
    can_build_transition,
    can_job_transition,
    is_publishable,
    is_terminal_build,
    require_build_transition,
    require_job_transition,
)


def test_published_builds_are_final() -> None:
    assert not can_build_transition(BuildStatus.PUBLISHED, BuildStatus.QUEUED)
    assert is_terminal_build(BuildStatus.PUBLISHED)
    assert not is_publishable(BuildStatus.PUBLISHED)


@pytest.mark.parametrize(
    "status", [BuildStatus.REJECTED, BuildStatus.FAILED, BuildStatus.CANCELLED]
)
def test_unsuccessful_builds_can_be_rerun_in_place(status: BuildStatus) -> None:
    assert can_build_transition(status, BuildStatus.QUEUED)


def test_validation_can_retry_generation() -> None:
    assert can_build_transition(BuildStatus.GENERATING, BuildStatus.VALIDATING)
    assert can_build_transition(BuildStatus.VALIDATING, BuildStatus.REJECTED)
    assert can_build_transition(BuildStatus.GENERATING, BuildStatus.GENERATING)


def test_queued_build_cannot_skip_straight_to_published() -> None:
    assert not can_build_transition(BuildStatus.QUEUED, BuildStatus.PUBLISHED)


def test_illegal_transition_reports_both_ends() -> None:
    with pytest.raises(TransitionError) as exc:
        require_build_transition(BuildStatus.PUBLISHED, BuildStatus.GENERATING)
    assert exc.value.current == "published"
    assert exc.value.target == "generating"


def test_running_job_cannot_jump_to_done_through_queued() -> None:
    assert can_job_transition(JobState.RUNNING, JobState.DONE)
    assert not can_job_transition(JobState.RUNNING, JobState.QUEUED)
    assert can_job_transition(JobState.CANCELLING, JobState.CANCELLED)


def test_interrupted_jobs_only_go_back_to_the_queue_or_fail() -> None:
    assert can_job_transition(JobState.INTERRUPTED, JobState.QUEUED)
    assert not can_job_transition(JobState.INTERRUPTED, JobState.RUNNING)
    with pytest.raises(TransitionError):
        require_job_transition(JobState.DONE, JobState.QUEUED)


def test_slugify_handles_prompt_titles() -> None:
    assert slugify("Neon Cyber-Rider!") == "neon-cyber-rider"
    assert slugify("!!!") == "prompt"
    assert unique_slug("dice-roller", {"dice-roller"}) == "dice-roller-2"
    assert unique_slug("dice-roller", {"dice-roller", "dice-roller-2"}) == "dice-roller-3"
    assert unique_slug("pomodoro", set()) == "pomodoro"
