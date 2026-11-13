"""Domain layer: entities, status enums, state machines.

No I/O, no database, no HTTP, no provider types. Everything here is pure so the
rules of the system can be reasoned about (and tested) without any of the
outer layers present.
"""

from __future__ import annotations

from app.domain.errors import DomainError, TransitionError
from app.domain.status import (
    BuildStatus,
    JobKind,
    JobState,
    PromptSource,
    TERMINAL_JOB_STATES,
    is_terminal_build,
)

__all__ = [
    "BuildStatus",
    "DomainError",
    "JobKind",
    "JobState",
    "PromptSource",
    "TERMINAL_JOB_STATES",
    "TERMINAL_BUILD_STATUSES",
    "TransitionError",
    "is_terminal_build",
]

TERMINAL_BUILD_STATUSES = frozenset(
    {BuildStatus.PUBLISHED, BuildStatus.FAILED, BuildStatus.REJECTED, BuildStatus.CANCELLED}
)
