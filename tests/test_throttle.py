"""The submission throttle: ``FP_RATE_LIMIT_PER_HOUR``, in memory, per address.

A gallery that spends someone else's API budget needs a ceiling a visitor can
run into, and it needs to say so plainly. These tests pin both the arithmetic
and the fact that hitting the ceiling creates nothing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.main import create_app
from app.storage.db import Database
from app.storage.repos.prompts import PromptRepo
from app.web.security import client_key
from app.web.throttle import SubmissionThrottle
from tests.helpers import make_endpoint, sibling_builds


# --- the counter --------------------------------------------------------------
def test_zero_means_no_limit() -> None:
    throttle = SubmissionThrottle(0)
    assert all(throttle.check("a", now=float(i)) is None for i in range(500))


def test_a_window_holds_per_hour_submissions() -> None:
    throttle = SubmissionThrottle(2)
    assert throttle.check("a", now=0.0) is None
    assert throttle.check("a", now=1.0) is None
    wait = throttle.check("a", now=2.0)
    assert wait is not None and 3598 <= wait <= 3600


def test_the_window_slides_rather_than_resetting() -> None:
    throttle = SubmissionThrottle(1)
    assert throttle.check("a", now=0.0) is None
    assert throttle.check("a", now=100.0) is not None
    # The first hit leaves the window at t=3600, so one second later it is free.
    assert throttle.check("a", now=3601.0) is None


def test_a_refused_submission_does_not_extend_the_wait() -> None:
    throttle = SubmissionThrottle(1)
    assert throttle.check("a", now=0.0) is None
    first = throttle.check("a", now=10.0)
    assert first is not None
    deadline = first + 10.0
    for moment in range(11, 40):
        later = throttle.check("a", now=float(moment))
        assert later is not None
        # The deadline is fixed: the wait counts down with the clock, so
        # hammering the form buys nothing.
        assert later + moment == pytest.approx(deadline)


def test_addresses_are_counted_apart() -> None:
    throttle = SubmissionThrottle(1)
    assert throttle.check("a", now=0.0) is None
    assert throttle.check("b", now=0.0) is None
    assert throttle.check("a", now=0.0) is not None


def test_remaining_tracks_the_window() -> None:
    throttle = SubmissionThrottle(3)
    assert throttle.remaining("a", now=0.0) == 3
    throttle.check("a", now=0.0)
    assert throttle.remaining("a", now=1.0) == 2
    assert throttle.remaining("a", now=3601.0) == 3


# --- who gets counted ---------------------------------------------------------
def _request(headers: dict[str, str], host: str | None = "10.0.0.7") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec-version": "2.1"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "path": "/new",
            "raw_path": b"/new",
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (host, 4444) if host else None,
            "server": ("gallery.example", 8080),
        }
    )


def test_client_key_reads_a_proxy_header_then_the_peer() -> None:
    assert client_key(_request({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})) == "203.0.113.9"
    assert client_key(_request({})) == "10.0.0.7"
    assert client_key(_request({"x-forwarded-for": "  "})) == "10.0.0.7"
    assert client_key(_request({}, host=None)) == "unknown"


# --- the routes honour it -----------------------------------------------------
@pytest.fixture
def pairs(db: Database) -> list[str]:
    endpoint, models = make_endpoint(db, name="throttled", models=("m1", "m2"))
    return [f"{endpoint.id}:{m.id}" for m in models]


@pytest.fixture
def capped_client(settings: Settings, db: Database, pairs: list[str]):  # noqa: ANN201
    """An app where an address gets two submissions an hour."""
    app_settings = replace(settings, seed_on_start=False, rate_limit_per_hour=2)
    with TestClient(create_app(app_settings)) as client:
        yield client


def test_the_form_stops_counting_and_says_when_to_try_again(
    capped_client: TestClient, db: Database, pairs: list[str]
) -> None:
    first = capped_client.post("/new", data={"body": "one", "b": pairs[:1]}, follow_redirects=False)
    second = capped_client.post("/new", data={"body": "two", "b": pairs[:1]}, follow_redirects=False)
    assert first.status_code == second.status_code == 303

    third = capped_client.post("/new", data={"body": "three", "b": pairs[:1]}, follow_redirects=False)
    assert third.status_code == 200
    assert "Too many submissions from this address" in third.text
    assert "Try again in" in third.text
    assert "three" in third.text  # the text is not lost to the ceiling
    assert len(PromptRepo(db).list()) == 2


def test_reading_the_form_is_free(bare_settings: Settings, db: Database, pairs: list[str]) -> None:
    settings = replace(bare_settings, rate_limit_per_hour=1)
    with TestClient(create_app(settings)) as client:
        for _ in range(5):
            assert client.get("/new").status_code == 200
        allowed = client.post(
            "/new", data={"body": "one", "b": pairs[:1]}, follow_redirects=False
        )
        assert allowed.status_code == 303
        refused = client.post("/new", data={"body": "two", "b": pairs[:1]}, follow_redirects=False)
        assert refused.status_code == 200
        assert "Too many submissions" in refused.text


def test_the_api_answers_429(bare_settings: Settings, db: Database, pairs: list[str]) -> None:
    settings = replace(bare_settings, rate_limit_per_hour=1)
    body = {"body": "a dice roller", "targets": [{"endpoint_id": int(pairs[0].split(":")[0]), "model_row_id": int(pairs[0].split(":")[1])}]}
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/prompts", json=body).status_code == 202
        refused = client.post("/api/prompts", json=body)
        assert refused.status_code == 429
        assert "retry in" in refused.json()["detail"]


def test_cancelling_is_never_throttled(bare_settings: Settings, db: Database, pairs: list[str]) -> None:
    """Cancelling saves work, so a ceiling on it would be the wrong way round."""
    settings = replace(bare_settings, rate_limit_per_hour=1)
    body = {"body": "a dice roller", "targets": [{"endpoint_id": int(pairs[0].split(":")[0]), "model_row_id": int(pairs[0].split(":")[1])}]}
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/prompts", json=body).status_code == 202
        assert client.post("/api/prompts", json=body).status_code == 429
        for _ in range(5):
            assert client.post("/api/jobs/1/cancel").status_code != 429


def test_a_refined_build_reports_the_ceiling(capped_client: TestClient, db: Database) -> None:
    _, builds = sibling_builds(db, count=1)
    build_id = builds[0].id
    throttle = capped_client.app.state.container.throttle  # type: ignore[attr-defined]
    throttle.check("testclient")
    throttle.check("testclient")

    response = capped_client.post(
        f"/build/{build_id}/refine", data={"instruction": "make it bigger"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("refine=rate-limited")
