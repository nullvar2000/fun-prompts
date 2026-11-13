"""The origin check that stands in for CSRF on the public write routes.

The gallery and the submission API have no session, so there is no token to
compare. What they can check is what the browser says about where the request
came from — and every modern browser attaches ``Origin`` to a POST.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.web.security import origin_of, same_origin_write, write_origins


def _request(method: str, headers: dict[str, str] | None = None) -> Request:
    """A request as a browser at ``gallery.example:8080`` would send it."""
    all_headers = {"host": "gallery.example:8080", **(headers or {})}
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec-version": "2.1"},
            "http_version": "1.1",
            "scheme": "http",
            "method": method,
            "path": "/build/1/refine",
            "raw_path": b"/build/1/refine",
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in all_headers.items()],
            "client": ("10.0.0.5", 5555),
            "server": ("gallery.example", 8080),
        }
    )


def _refused(call) -> bool:  # noqa: ANN001 - the point is the status code
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value.status_code == 403


# --- origin parsing -----------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://gallery.example:8080/build/1", "http://gallery.example:8080"),
        ("http://gallery.example/", "http://gallery.example"),
        ("https://gallery.example/", "https://gallery.example"),
        ("https://gallery.example:443", "https://gallery.example"),
        ("http://gallery.example:80", "http://gallery.example"),
        ("http://Gallery.Example:8080", "http://gallery.example:8080"),
        ("http://[::1]:8081/x", "http://[::1]:8081"),
        ("http://[::1]/x", "http://[::1]"),
        ("http://user:pw@gallery.example:8080", "http://gallery.example:8080"),
        ("not a url", None),
        ("http://", None),
        ("http://host:99999", None),
        ("", None),
    ],
)
def test_origin_of_reduces_a_url_to_its_origin(url: str, expected: str | None) -> None:
    assert origin_of(url) == expected


# --- the check itself ---------------------------------------------------------
def test_a_write_from_the_host_being_browsed_is_allowed(bare_settings: Settings) -> None:
    same_origin_write(_request("POST", {"origin": "http://gallery.example:8080"}), bare_settings)


def test_a_write_from_another_origin_is_refused(bare_settings: Settings) -> None:
    assert _refused(lambda: same_origin_write(_request("POST", {"origin": "http://elsewhere.example"}), bare_settings))


def test_a_write_with_no_origin_header_falls_back_to_referer(bare_settings: Settings) -> None:
    same_origin_write(_request("POST", {"referer": "http://gallery.example:8080/gallery"}), bare_settings)
    assert _refused(
        lambda: same_origin_write(_request("POST", {"referer": "http://elsewhere.example/page"}), bare_settings)
    )


def test_no_origin_and_no_referer_is_not_a_refusal(bare_settings: Settings) -> None:
    """curl, a health check, a test client: none of them is a browser cross-site POST."""
    same_origin_write(_request("POST"), bare_settings)


def test_reads_are_never_checked(bare_settings: Settings) -> None:
    for method in ("GET", "HEAD", "OPTIONS"):
        same_origin_write(_request(method, {"origin": "http://elsewhere.example"}), bare_settings)


def test_a_malformed_origin_is_refused_not_crashed(bare_settings: Settings) -> None:
    assert _refused(lambda: same_origin_write(_request("POST", {"origin": "http://[::1"}), bare_settings))


def test_the_configured_gallery_origin_counts_even_behind_a_proxy(bare_settings: Settings) -> None:
    """FP_GALLERY_BASE_URL names where users browse; the Host header may say otherwise."""
    settings = replace(bare_settings, gallery_base_url="http://public.example")
    request = _request("POST", {"origin": "http://public.example"})
    assert "http://public.example" in write_origins(request, settings)
    same_origin_write(request, settings)


def test_the_artifact_origin_can_never_write(bare_settings: Settings) -> None:
    """A generated app must not be able to submit, revise or cancel."""
    settings = replace(
        bare_settings,
        artifact_base_url="http://gallery.example:8081",
        gallery_base_url="http://gallery.example:8080",
    )
    request = _request("POST", {"origin": "http://gallery.example:8081"})
    assert "http://gallery.example:8081" not in write_origins(request, settings)
    assert _refused(lambda: same_origin_write(request, settings))


# --- the routes actually enforce it ------------------------------------------
def test_a_cross_origin_write_is_refused_everywhere_it_matters(bare_client: TestClient) -> None:
    evil = {"Origin": "http://elsewhere.example"}
    body = {"body": "a dice roller", "targets": [{"endpoint_id": 1, "model_row_id": 1}]}

    assert bare_client.post("/api/prompts", json=body, headers=evil).status_code == 403
    assert (
        bare_client.post("/api/builds/1/refine", json={"instruction": "x"}, headers=evil).status_code == 403
    )
    assert bare_client.post("/api/jobs/1/cancel", headers=evil).status_code == 403
    # Nothing was queued or touched on the way to the 403.
    assert bare_client.get("/api/jobs").json()["recent"] == []


def test_the_check_does_not_break_the_routes_it_guards(bare_client: TestClient) -> None:
    headerless = bare_client.post("/api/jobs/1/cancel")
    assert headerless.status_code == 404  # no such job: the route ran

    same_origin = bare_client.post("/api/jobs/1/cancel", headers={"Origin": "http://testserver"})
    assert same_origin.status_code == 404


def test_a_cross_origin_read_is_fine(bare_client: TestClient) -> None:
    page = bare_client.get("/", headers={"Origin": "http://elsewhere.example"})
    assert page.status_code == 200
