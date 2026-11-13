"""Domain-level failures, independent of storage or transport."""

from __future__ import annotations


class DomainError(RuntimeError):
    """Base for rule violations detected in the domain layer."""


class TransitionError(DomainError):
    """A status change that the state machines do not allow."""

    def __init__(self, what: str, current: str, target: str) -> None:
        super().__init__(f"{what}: cannot go from {current} to {target}")
        self.what = what
        self.current = current
        self.target = target


class ValidationError(DomainError):
    """Input failed a rule before any work was scheduled."""
