"""The rotating status copy an in-flight build shows.

The rotation is computed from the build's own clock, not scripted: the same
build at the same moment says the same thing to every viewer, the line still
moves while a visitor watches (the polling re-renders it), and terminal
statuses keep their headline alone — nothing is in progress to keep company.
"""

from __future__ import annotations

import datetime as dt

from app.domain.models import Build
from app.domain.status import BuildStatus
from app.web.status_copy import FLAVOUR, ROTATE_SECONDS, build_report

_START = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
_ISO = _START.isoformat()


def _build(status: BuildStatus, updated_at: str = _ISO) -> Build:
    return Build(
        id=1,
        prompt_id=1,
        endpoint_id=1,
        model_row_id=1,
        status=status,
        updated_at=updated_at,
    )


def _flavour_at(status: BuildStatus, seconds: int) -> str:
    report = build_report(_build(status), "test-endpoint", now=_START + dt.timedelta(seconds=seconds))
    assert report["flavour"] is not None
    return report["flavour"]


def test_the_flavour_line_rotates_as_the_build_waits() -> None:
    seen = [_flavour_at(BuildStatus.GENERATING, ROTATE_SECONDS * turn) for turn in range(5)]
    assert seen[0] != seen[1] and seen[1] != seen[2] and seen[2] != seen[3]
    assert seen[4] == seen[0]  # wraps around, so the wait never runs out of lines


def test_the_same_moment_says_the_same_thing_to_every_viewer() -> None:
    first = build_report(_build(BuildStatus.QUEUED), "e", now=_START + dt.timedelta(seconds=40))
    second = build_report(_build(BuildStatus.QUEUED), "e", now=_START + dt.timedelta(seconds=40, milliseconds=500))
    assert first == second  # same whole second: same report, line included


def test_the_headline_names_the_endpoint_being_asked() -> None:
    report = build_report(_build(BuildStatus.GENERATING, _ISO), "litellm", now=_START)
    assert "generating — asking litellm…" in report["headline"]
    assert report["elapsed"] == 0


def test_terminal_statuses_report_the_fact_without_company() -> None:
    for status in (BuildStatus.REJECTED, BuildStatus.FAILED, BuildStatus.CANCELLED):
        report = build_report(_build(status), "litellm", now=_START)
        assert report["flavour"] is None and report["elapsed"] is None
        assert report["headline"]  # every status still has its sentence


def test_a_truncated_rejection_blames_the_ceiling_not_the_artifact() -> None:
    failures = [{"stage": "truncated"}, {"stage": "truncated"}]
    report = build_report(_build(BuildStatus.REJECTED), "litellm", failures=failures, now=_START)
    assert "ran out of tokens before the document finished" in report["headline"]
    assert report["flavour"] is None


def test_a_rejection_for_other_reasons_keeps_the_plain_headline() -> None:
    for failures in ([{"stage": "validate"}], [], None):
        report = build_report(_build(BuildStatus.REJECTED), "litellm", failures=failures, now=_START)
        assert "did not pass the gate" in report["headline"]


def test_the_three_working_statuses_all_have_lines() -> None:
    for status in (BuildStatus.QUEUED, BuildStatus.GENERATING, BuildStatus.VALIDATING):
        assert len(FLAVOUR[str(status)]) >= 2  # rotation needs more than one line


def test_an_unreadable_timestamp_still_says_something() -> None:
    report = build_report(_build(BuildStatus.QUEUED, updated_at=""), "e", now=_START)
    assert "queued —" in report["headline"]
    assert report["flavour"] == FLAVOUR["queued"][0] and report["elapsed"] == 0
