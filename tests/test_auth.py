"""Session cookie, login, and the CSRF gate on ``/admin``.

The threat model these tests hold: a page generated on the artifact origin is a
different *origin* but the same *host*, and cookies ignore ports. So the admin
surface is held by three independent things — an httpOnly + SameSite=Strict
cookie, a token that must be echoed back on every write, and a custom header on
JSON writes. Each one is asserted separately, because each one is load-bearing.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.web.auth import (
    SESSION_COOKIE,
    AdminGuard,
    Session,
    SessionError,
    SessionNotConfigured,
    SessionSigner,
    new_session,
    set_session_cookie,
    signer_for,
)
from app.web.security import SecurityHeaders
from tests.helpers import CSRF_FIELD_RE, admin_post, csrf_token_on, sign_in


def set_cookie(response, name: str = SESSION_COOKIE) -> dict[str, str]:
    """The Set-Cookie header as a dict: ``{"value": ..., "httponly": "1", ...}``."""
    header = response.headers["set-cookie"]
    head, *attrs = [part.strip() for part in header.split(";")]
    cookie_name, _, value = head.partition("=")
    assert cookie_name == name
    out = {key.lower(): val.strip() for key, _, val in (attr.partition("=") for attr in attrs)}
    out["value"] = value
    return out


# --- the signer ---------------------------------------------------------------
def test_signer_round_trips_a_session() -> None:
    signer = SessionSigner("k" * 40)
    # The cookie stores a whole-second expiry, so the round trip is exact only for
    # an integral value; that is what the browser sees.
    session = Session(authed=True, csrf="tok", exp=float(int(time.time()) + 60))
    assert signer.decode(signer.encode(session)) == session


def test_signer_rejects_a_changed_body() -> None:
    signer = SessionSigner("k" * 40)
    token = signer.encode(Session(authed=False, csrf="tok", exp=time.time() + 60))
    body, _, signature = token.partition(".")
    forged = f'{body[:-4]}.aaaa.{signature}'
    with pytest.raises(SessionError):
        signer.decode(forged)


def test_signer_rejects_a_changed_payload_under_an_old_signature() -> None:
    """A cookie signed by *this* key but for an anonymous session cannot be
    escalated by editing the body: the signature covers the body."""
    signer = SessionSigner("k" * 40)
    token = signer.encode(Session(authed=False, csrf="tok", exp=time.time() + 60))
    _, _, signature = token.partition(".")
    other = SessionSigner("j" * 40).encode(Session(authed=True, csrf="tok", exp=time.time() + 60))
    other_body, _, _ = other.partition(".")
    with pytest.raises(SessionError):
        signer.decode(f"{other_body}.{signature}")


def test_signer_rejects_garbage() -> None:
    signer = SessionSigner("k" * 40)
    for token in ("", "no-dot", ".only-signature", "!!!!.deadbeef"):
        with pytest.raises(SessionError):
            signer.decode(token)


def test_signer_refuses_a_short_secret() -> None:
    with pytest.raises(ValueError):
        SessionSigner("too-short")


def test_signer_for_needs_a_configured_secret() -> None:
    with pytest.raises(SessionNotConfigured):
        signer_for(Settings(seed_on_start=False, session_secret=None))
    assert signer_for(Settings(seed_on_start=False, session_secret="z" * 40)) is signer_for(
        Settings(seed_on_start=False, session_secret="z" * 40)
    )


# --- gating ------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/admin", "/admin/endpoints", "/admin/builds", "/admin/failures"])
def test_admin_pages_redirect_anonymous_visitors(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_login_page_is_public_and_sets_a_hardened_cookie(client: TestClient) -> None:
    response = client.get("/admin/login")
    assert response.status_code == 200
    assert "csrf_token" in response.text
    cookie = set_cookie(response)
    assert "httponly" in cookie
    assert cookie["samesite"] == "strict"
    # Off by default: the expected deployment is a LAN over plain http.
    assert "secure" not in cookie


def test_cookie_secure_flag_follows_config(settings: Settings) -> None:
    with TestClient(create_app(replace(settings, cookie_secure=True))) as client:
        response = client.get("/admin/login")
        assert "secure" in set_cookie(response)


def test_the_guard_response_still_carries_the_csp(client: TestClient) -> None:
    """SecurityHeaders sits outside the guard, so even a 303 from the guard is
    covered by the CSP."""
    response = client.get("/admin", follow_redirects=False)
    assert "content-security-policy" in response.headers


def test_admin_link_is_offered_to_logged_out_visitors(client: TestClient) -> None:
    assert 'href="/admin"' in client.get("/").text


# --- login -------------------------------------------------------------------
def test_wrong_password_does_not_authenticate(client: TestClient) -> None:
    token = csrf_token_on(client)
    response = client.post("/admin/login", data={"password": "nope", "csrf_token": token})
    assert response.status_code == 401
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_then_logout(client: TestClient) -> None:
    sign_in(client)
    assert client.get("/admin").status_code == 200
    response = client.post(
        "/admin/logout",
        data={"csrf_token": csrf_token_on(client, "/admin")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_needs_the_csrf_token(client: TestClient) -> None:
    response = client.post("/admin/login", data={"password": "test-admin-password"})
    assert response.status_code == 403


def test_failed_logins_are_throttled(settings: Settings) -> None:
    strict = replace(settings, login_max_attempts_per_hour=2)
    with TestClient(create_app(strict)) as client:
        token = csrf_token_on(client)
        for _ in range(2):
            assert client.post("/admin/login", data={"password": "x", "csrf_token": token}).status_code == 401
        throttled = client.post("/admin/login", data={"password": "test-admin-password", "csrf_token": token})
        assert throttled.status_code == 429
        assert "try again later" in throttled.text


def test_empty_password_never_logs_in() -> None:
    """A Settings with no password must not become an open door."""
    from app.web.auth import check_password

    assert check_password("", Settings(seed_on_start=False, admin_password=None)) is False


def test_forged_cookie_from_another_secret_is_ignored(settings: Settings) -> None:
    attacker = SessionSigner("a" * 40)
    with TestClient(create_app(settings)) as client:
        client.cookies.set(SESSION_COOKIE, attacker.encode(new_session(600)))
        assert client.get("/admin", follow_redirects=False).status_code == 303


def test_expired_session_is_ignored(settings: Settings) -> None:
    signer = signer_for(settings)
    with TestClient(create_app(settings)) as client:
        stale = Session(authed=True, csrf="tok", exp=time.time() - 1)
        client.cookies.set(SESSION_COOKIE, signer.encode(stale))
        assert client.get("/admin", follow_redirects=False).status_code == 303
        # The login form is still usable: it gets its own fresh anonymous session.
        assert client.get("/admin/login").status_code == 200


def test_the_session_cookie_is_not_a_route_secret(client: TestClient) -> None:
    """Being logged in must not be inferred from anything but the signed cookie."""
    sign_in(client)
    response = client.get("/admin/endpoints")
    assert response.status_code == 200
    assert "csrf_token" in response.text


# --- csrf gate ---------------------------------------------------------------
def test_admin_write_without_a_token_is_rejected(client: TestClient) -> None:
    sign_in(client)
    response = client.post("/admin/endpoints/1/enabled", data={"enabled": "1"})
    assert response.status_code == 403
    assert "csrf" in response.text


def test_admin_write_with_a_wrong_token_is_rejected(client: TestClient) -> None:
    sign_in(client)
    assert admin_post(client, "/admin/endpoints/1/enabled", {"enabled": "1"}, token="guessed").status_code == 403


def test_a_token_from_another_session_does_not_work(settings: Settings, client: TestClient) -> None:
    """Tokens are per session, not per deployment: two browsers holding two
    sessions cannot use each other's token, even with one shared secret."""
    sign_in(client)
    with TestClient(create_app(replace(settings, seed_on_start=False))) as other:
        foreign = csrf_token_on(other, "/admin/login")
        assert admin_post(
            client, "/admin/endpoints/1/enabled", {"enabled": "1"}, token=foreign
        ).status_code == 403


def test_a_write_without_the_session_cookie_is_not_performed(client: TestClient) -> None:
    """The cross-site case: SameSite=Strict means the cookie is simply absent, so
    the guard never reaches the action at all."""
    sign_in(client)
    tampered = client.get("/admin", follow_redirects=False)
    assert tampered.status_code == 200
    client.cookies.set(SESSION_COOKIE, "not-a-cookie-we-signed")
    response = admin_post(client, "/admin/endpoints/1/enabled", {"enabled": "1"})
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_admin_write_accepts_the_token_in_a_header(client: TestClient) -> None:
    sign_in(client)
    token = csrf_token_on(client, "/admin")
    response = client.post(
        "/admin/builds/999999/tags",
        json={"tag": "x"},
        headers={"x-csrf-token": token, "x-requested-with": "fetch"},
    )
    assert response.status_code == 404  # the tag route ran; it is the build that is missing


def test_json_write_without_the_ajax_header_is_rejected(client: TestClient) -> None:
    sign_in(client)
    token = csrf_token_on(client, "/admin")
    response = client.post("/admin/builds/999999/tags", json={"tag": "x", "csrf_token": token})
    assert response.status_code == 403


def test_oversized_admin_body_is_refused_before_it_is_parsed(client: TestClient) -> None:
    sign_in(client)
    token = csrf_token_on(client, "/admin")
    response = client.post(
        "/admin/endpoints/1/enabled",
        data={"enabled": "1", "csrf_token": token, "pad": "a" * 70_000},
    )
    assert response.status_code == 413


def test_the_body_survives_the_gate_so_the_route_can_read_it(client: TestClient) -> None:
    """The guard buffers the body to find the token; the route must still see the
    other fields. If Starlette ever stops replaying it, this is what fails."""
    sign_in(client)
    response = admin_post(client, "/admin/endpoints", {"name": "survivor", "base_url": "http://127.0.0.1:9/v1"})
    assert response.status_code == 303
    listing = client.get("/admin/endpoints").text
    assert "survivor" in listing


def test_non_admin_writes_need_no_csrf_token(client: TestClient) -> None:
    """The gate is scoped to /admin: the public submission API must keep working."""
    response = client.post("/api/prompts", json={"body": "x", "targets": []})
    assert response.status_code == 422  # validated by the API, not blocked by the guard


# --- the gate on its own -----------------------------------------------------
def _bare_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.add_middleware(AdminGuard, settings=settings)
    app.add_middleware(SecurityHeaders, settings=settings)

    @app.post("/admin/echo")
    async def echo(request: Request) -> dict:
        return {"fields": sorted((await request.form()).keys())}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    return app


def test_gate_orders_auth_before_csrf_and_blocks_writes(settings: Settings) -> None:
    with TestClient(_bare_app(settings)) as app:
        # Anonymous: every method is redirected to the login page.
        assert app.get("/admin/echo", follow_redirects=False).status_code == 303
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = app.request(method, "/admin/echo", data={"a": "1"}, follow_redirects=False)
            assert response.status_code == 303, method

        # Authenticated with a session whose token is "tok": only the right token passes.
        signed = app.post("/admin/echo", data={"a": "1", "csrf_token": "wrong"}, follow_redirects=False)
        assert signed.status_code == 303  # still no session cookie at all

        response = JSONResponse({})
        set_session_cookie(response, settings, Session(authed=True, csrf="tok", exp=time.time() + 600))
        app.cookies.set(SESSION_COOKIE, set_cookie(response)["value"])
        assert app.post("/admin/echo", data={"a": "1", "csrf_token": "wrong"}).status_code == 403
        accepted = app.post("/admin/echo", data={"a": "1", "csrf_token": "tok"})
        assert accepted.status_code == 200
        assert accepted.json()["fields"] == ["a", "csrf_token"]


def test_gate_refuses_to_run_admin_without_a_signing_key(settings: Settings) -> None:
    """Defence in depth: ``bootstrap_secrets`` always fills a secret in, so a
    process that somehow has none must hand out no session and accept none."""
    broken = replace(settings, session_secret=None)
    with TestClient(_bare_app(broken)) as client:
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 503
        assert "session secret" in response.text
        # The rest of the app is untouched by that decision.
        assert client.get("/healthz").status_code == 200


def test_set_session_cookie_writes_a_verifiable_cookie(settings: Settings) -> None:
    response = JSONResponse({})
    set_session_cookie(response, settings, new_session(600))
    cookie = set_cookie(response)
    assert not cookie["value"].startswith('"'), "no quoting: the cookie value carries no ="
    session = signer_for(settings).decode(cookie["value"])
    assert session.authed is False and session.csrf
    assert int(cookie["max-age"]) == settings.session_ttl_hours * 3600


def test_login_page_shows_remaining_attempts(client: TestClient) -> None:
    html = client.get("/admin/login").text
    match = CSRF_FIELD_RE.search(html)
    assert match
    assert "attempt(s) left" in html
