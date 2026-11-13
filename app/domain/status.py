"""Status enums and the state machines that govern them.

Storing statuses as text (``StrEnum``) keeps the SQLite rows readable without a
join against a lookup table, and lets the transition rules live here rather than
scattered through the repository code.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.errors import TransitionError


class PromptSource(StrEnum):
    SEED = "seed"
    USER = "user"
    FORK = "fork"


class BuildStatus(StrEnum):
    """Lifecycle of one prompt built by one endpoint+model target."""

    QUEUED = "queued"
    GENERATING = "generating"
    VALIDATING = "validating"
    PUBLISHED = "published"
    #: The model answered, but the artifact failed validation (or extraction).
    REJECTED = "rejected"
    #: A transport/provider failure, or the job died.
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Cancel was requested while the job was in flight.
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The process died mid-job; recovered at startup, never silently rerun.
    INTERRUPTED = "interrupted"


class JobKind(StrEnum):
    BUILD = "build"
    REFINE = "refine"
    PROBE = "probe"
    DISCOVER = "discover"


TERMINAL_JOB_STATES = frozenset(
    {JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}
)

#: A published build stays published; it is unlisted via its ``hidden`` flag, so
#: moderation never destroys build history.
_BUILD_TRANSITIONS: dict[BuildStatus, frozenset[BuildStatus]] = {
    BuildStatus.QUEUED: frozenset(
        {
            BuildStatus.GENERATING,
            BuildStatus.CANCELLED,
            BuildStatus.FAILED,
        }
    ),
    BuildStatus.GENERATING: frozenset(
        {
            BuildStatus.VALIDATING,
            BuildStatus.FAILED,
            BuildStatus.CANCELLED,
            # Repair retry: validate failed, ask again with a stricter instruction.
            BuildStatus.GENERATING,
        }
    ),
    BuildStatus.VALIDATING: frozenset(
        {
            BuildStatus.PUBLISHED,
            BuildStatus.REJECTED,
            BuildStatus.FAILED,
            BuildStatus.CANCELLED,
            # A failed validation sends the build back for the repair retry.
            BuildStatus.GENERATING,
        }
    ),
    BuildStatus.REJECTED: frozenset({BuildStatus.QUEUED}),
    BuildStatus.FAILED: frozenset({BuildStatus.QUEUED}),
    BuildStatus.CANCELLED: frozenset({BuildStatus.QUEUED}),
    BuildStatus.PUBLISHED: frozenset(),
}

_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {JobState.DONE, JobState.FAILED, JobState.CANCELLING, JobState.INTERRUPTED}
    ),
    JobState.CANCELLING: frozenset({JobState.CANCELLED, JobState.FAILED}),
    JobState.INTERRUPTED: frozenset({JobState.QUEUED, JobState.FAILED}),
    JobState.DONE: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def can_build_transition(current: BuildStatus, target: BuildStatus) -> bool:
    return target in _BUILD_TRANSITIONS[BuildStatus(current)]


def require_build_transition(current: BuildStatus, target: BuildStatus) -> None:
    if not can_build_transition(current, target):
        raise TransitionError("build", str(current), str(target))


def can_job_transition(current: JobState, target: JobState) -> bool:
    return target in _JOB_TRANSITIONS[JobState(current)]


def require_job_transition(current: JobState, target: JobState) -> None:
    if not can_job_transition(current, target):
        raise TransitionError("job", str(current), str(target))


def is_terminal_build(status: BuildStatus) -> bool:
    """Terminal means no further generation work is expected for this status."""
    return status in {
        BuildStatus.PUBLISHED,
        BuildStatus.REJECTED,
        BuildStatus.FAILED,
        BuildStatus.CANCELLED,
    }


def is_publishable(status: BuildStatus) -> bool:
    return status in {BuildStatus.QUEUED, BuildStatus.GENERATING, BuildStatus.VALIDATING}
