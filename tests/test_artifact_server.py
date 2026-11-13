"""The artifact origin: what it serves, what it refuses, and the headers on both.

Two transport levels on purpose. Most cases go straight through the ASGI app,
but one case starts uvicorn on an ephemeral port, because the whole point of
this origin is what a *browser* sees across a socket: header set, no cookies,
and no API surface next to the artifact bytes.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from itertools import count

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.status import BuildStatus
from app.main import create_app
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.web.artifact_server import artifact_csp, create_artifact_app, start_artifact_server
from tests.helpers import make_endpoint, make_prompt

DOC = "<!DOCTYPE html><html><head><title>a</title></head><body><b>hi</b></body></html>"
_endpoint_names = count()


def make_artifact_build(
    db: Database,
    *,
    html: str = DOC,
    status: BuildStatus = BuildStatus.PUBLISHED,
    hidden: bool = False,
    prompt_hidden: bool = False,
) -> int:
    """A build with one version, parked in whatever state the test asks for."""
    name = f"art-{next(_endpoint_names)}"
    endpoint, models = make_endpoint(db, name=name)
    prompt = make_prompt(db, title=f"prompt {name}", body=f"build {name}")
    builds = BuildRepo(db)
    build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=models[0].id)
    version = builds.add_version(build.id, html=html, raw_response=html)
    if status is not BuildStatus.QUEUED:
        # Walk the real state machine rather than writing the column directly.
        for step in (BuildStatus.GENERATING, BuildStatus.VALIDATING, status):
            builds.set_status(build.id, step)
    if hidden:
        builds.set_hidden(build.id, True)
    if prompt_hidden:
        from app.storage.repos.prompts import PromptRepo

        PromptRepo(db).set_hidden(prompt.id, True)
    return version.id


async def _client(app) -> httpx.AsyncClient:  # noqa: ANN001
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://artifacts.test")


# --- serving ------------------------------------------------------------------
async def test_a_published_artifact_is_served_byte_for_byte(settings: Settings, db: Database) -> None:
    version_id = make_artifact_build(db)
    app = create_artifact_app(settings, db=db)
    async with await _client(app) as client:
        response = await client.get(f"/artifact/{version_id}")
    assert response.status_code == 200
    assert response.text == DOC
    assert response.headers["content-type"] == "text/html; charset=utf-8"


async def test_an_unpublished_build_is_not_served(settings: Settings, db: Database) -> None:
    for status in (BuildStatus.REJECTED, BuildStatus.FAILED, BuildStatus.CANCELLED, BuildStatus.QUEUED):
        version_id = make_artifact_build(db, status=status)
        app = create_artifact_app(settings, db=db)
        async with await _client(app) as client:
            response = await client.get(f"/artifact/{version_id}")
        assert response.status_code == 404, status
        assert "not available" in response.text


async def test_hiding_a_build_or_its_prompt_stops_serving_it(settings: Settings, db: Database) -> None:
    app = create_artifact_app(settings, db=db)
    hidden_build = make_artifact_build(db, hidden=True)
    hidden_prompt = make_artifact_build(db, prompt_hidden=True)
    async with await _client(app) as client:
        for version_id in (hidden_build, hidden_prompt):
            response = await client.get(f"/artifact/{version_id}")
            assert response.status_code == 404


async def test_an_unknown_version_is_a_plain_not_found(settings: Settings, db: Database) -> None:
    app = create_artifact_app(settings, db=db)
    async with await _client(app) as client:
        missing = await client.get("/artifact/424242")
        junk = await client.get("/anything-else")
        api = await client.post("/api/prompts", json={})
    assert missing.status_code == 404 and junk.status_code == 404
    # There is no API on this origin: every route is GET/HEAD only, so artifact
    # JS cannot even attempt a write against it.
    assert api.status_code == 405
    assert missing.headers["content-type"].startswith("text/plain")


async def test_the_size_ceiling_is_enforced_at_serve_time(bare_settings: Settings, db: Database) -> None:
    version_id = make_artifact_build(db, html=DOC + "<!--" + "x" * 4000 + "-->")
    settings = replace(bare_settings, max_artifact_kb=1)
    app = create_artifact_app(settings, db=db)
    async with await _client(app) as client:
        response = await client.get(f"/artifact/{version_id}")
    assert response.status_code == 413


# --- headers ------------------------------------------------------------------
async def test_the_csp_allows_only_inline_paint_and_forbids_every_reach(
    settings: Settings, db: Database
) -> None:
    version_id = make_artifact_build(db)
    app = create_artifact_app(settings, db=db)
    async with await _client(app) as client:
        response = await client.get(f"/artifact/{version_id}")

    csp = response.headers["content-security-policy"]
    for directive in (
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors http://localhost:8080",
    ):
        assert directive in csp, directive

    # X-Frame-Options is deliberately absent: browsers disagree about whether
    # SAMEORIGIN ignores ports, and this framing pair is port-specific.
    assert "x-frame-options" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert "no-cache" in response.headers["cache-control"]
    # Nothing on this origin may carry or set credentials.
    assert "set-cookie" not in response.headers


async def test_the_two_origins_reference_each_other_the_right_way(
    bare_settings: Settings, db: Database
) -> None:
    """A swapped pair would break framing in a way that looks like a browser bug."""
    gallery = TestClient(create_app(bare_settings))
    artifact = create_artifact_app(bare_settings, db=db)
    async with await _client(artifact) as client:
        framed = await client.get("/artifact/1")
    page = gallery.get("/")

    assert "frame-src http://localhost:8081" in page.headers["content-security-policy"]
    assert "frame-ancestors http://localhost:8080" in framed.headers["content-security-policy"]


def test_the_pair_follows_configuration(bare_settings: Settings) -> None:
    moved = replace(
        bare_settings,
        port=9000,
        artifact_port=9001,
        artifact_base_url="http://gallery.example:9001",
        extra_frame_ancestors=["https://vpn.example"],
    )
    csp = artifact_csp(moved)
    assert "frame-ancestors http://gallery.example:9000 https://vpn.example" in csp


def test_ipv6_literals_survive_the_port_swap(bare_settings: Settings) -> None:
    moved = replace(bare_settings, artifact_base_url="http://[::1]:8081")
    assert "frame-ancestors http://[::1]:8080" in artifact_csp(moved)


async def test_health_is_a_bare_ok_on_the_artifact_origin(settings: Settings, db: Database) -> None:
    app = create_artifact_app(settings, db=db)
    async with await _client(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"
    assert "content-security-policy" not in response.headers


# --- over a real socket -------------------------------------------------------
async def test_the_origin_really_listens_and_answers(
    bare_settings: Settings, db: Database, free_tcp_port: int
) -> None:
    version_id = make_artifact_build(db)
    settings = replace(bare_settings, artifact_port=free_tcp_port, host="127.0.0.1")
    server, task = await start_artifact_server(create_artifact_app(settings, db=db), settings)

    try:
        deadline = asyncio.get_running_loop().time() + 5
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{free_tcp_port}"
                ) as client:
                    response = await client.get(f"/artifact/{version_id}")
                    rejected = await client.get("/artifact/999999")
                break
            except httpx.TransportError as exc:  # still binding
                last_error = exc
                await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"artifact origin never answered: {last_error}")

        assert response.status_code == 200 and response.text == DOC
        assert rejected.status_code == 404
        # frame-ancestors follows FP_ARTIFACT_BASE_URL's host with FP_PORT, which
        # is the documented pairing rule for the two origins.
        assert "frame-ancestors http://localhost:8080" in response.headers["content-security-policy"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
