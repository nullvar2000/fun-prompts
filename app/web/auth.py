"""Admin session and CSRF protection for the gallery origin (:8080).

Why this is stricter than it looks. The artifact origin is a *different port* on
the same host, and cookies ignore port boundaries: a session cookie set on
``:8080`` is also sent to ``:8081``. A generated page framed from there has an
opaque origin and cannot read the parent DOM, but it can still fire credentialed
requests at ``:8080``. So the boundary is held by three things together:

* ``SameSite=Strict`` — a request navigated to from another site carries no
  session cookie at all.
* A CSRF token that lives inside the signed session and must be echoed back in
  the request body or a header. A cross-origin page cannot read it.
* ``X-Requested-With`` on JSON writes, which the fetch spec will not let a
  cross-origin request set without a CORS handshake this origin never offers.

The token is signed with ``FP_SESSION_SECRET`` (HMAC-SHA256, base64url body,
``hmac.compare_digest`` on the way in) rather than handed to a signing library:
one small, auditable primitive is better here than a dependency with its own
defaults.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import parse_qs

from markupsafe import Markup, escape
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app import logsetup
from app.config import Settings

log = logsetup.get("app.auth")

SESSION_COOKIE = "fp_admin"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
AJAX_HEADER = "x-requested-with"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: Admin forms are small; a larger mutating body is not a form we wrote.
MAX_CSRF_BODY_BYTES = 64 * 1024

#: Reachable without a session; the login form carries its own CSRF token.
PUBLIC_ADMIN_PATHS = frozenset({"/admin/login"})


class SessionError(ValueError):
    """The cookie is missing, malformed, tampered with, or expired."""


@dataclass(frozen=True)
class Session:
    authed: bool = False
    csrf: str = ""
    exp: float = 0.0

    @property
    def expired(self) -> bool:
        return self.exp <= time.time()


class SessionSigner:
    """Signs the session cookie. Stateless: the cookie *is* the session."""

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError(
                "the session secret must be at least 32 characters; leave "
                "FP_SESSION_SECRET unset and it will be generated on first boot"
            )
        self._key = secret.encode("utf-8")

    def encode(self, session: Session) -> str:
        payload = {
            "a": 1 if session.authed else 0,
            "c": session.csrf,
            "e": int(session.exp),
        }
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        # No padding: a cookie value containing "=" gets quoted on the wire, and
        # decode() puts the padding back.
        body = body.rstrip("=")
        signature = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def decode(self, token: str) -> Session:
        body, _, signature = token.partition(".")
        if not body or not signature:
            raise SessionError("malformed session cookie")
        expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise SessionError("session cookie signature does not match")
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        except (ValueError, TypeError) as exc:
            raise SessionError("session cookie body is not readable") from exc
        return Session(authed=bool(payload.get("a")), csrf=str(payload.get("c", "")), exp=float(payload.get("e", 0)))


class SessionNotConfigured(SessionError):
    """No usable signing key, so the admin session cannot exist at all."""


def new_session(ttl_seconds: int) -> Session:
    return Session(authed=False, csrf=secrets.token_urlsafe(24), exp=time.time() + ttl_seconds)


def login_session(ttl_seconds: int) -> Session:
    """A fresh csrf token on login: the pre-login token never authorises anything."""
    return Session(authed=True, csrf=secrets.token_urlsafe(24), exp=time.time() + ttl_seconds)


#: One signer per secret value. Built lazily because ``FP_SESSION_SECRET`` is
#: filled in by ``bootstrap_secrets`` during startup, after the middleware graph
#: has already been constructed.
_signers: dict[str, SessionSigner] = {}


def signer_for(settings: Settings) -> SessionSigner:
    secret = settings.session_secret or ""
    if len(secret) < 32:
        raise SessionNotConfigured("FP_SESSION_SECRET is missing or too short")
    signer = _signers.get(secret)
    if signer is None:
        signer = SessionSigner(secret)
        _signers[secret] = signer
    return signer


def set_session_cookie(response: Response, settings: Settings, session: Session) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        signer_for(settings).encode(session),
        max_age=int(settings.session_ttl_hours * 3600),
        path="/",
        httponly=True,
        samesite="strict",
        # Off by default: this deployment is expected on a LAN over http. Enable
        # it (FP_COOKIE_SECURE) as soon as something TLS-terminating sits in front.
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


class LoginAttempts:
    """In-process login throttle.

    Per-process, not per-cluster: one container is the deployment, and the
    counter's job is to make a password guess over a LAN impractical rather than
    to be a distributed firewall.
    """

    _WINDOW_SECONDS: ClassVar[int] = 3600

    def __init__(self, max_per_hour: int) -> None:
        self.max_per_hour = max(1, max_per_hour)
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self._hits.get(key, []) if now - t < self._WINDOW_SECONDS]
        if len(recent) >= self.max_per_hour:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def remaining(self, key: str) -> int:
        now = time.time()
        recent = [t for t in self._hits.get(key, []) if now - t < self._WINDOW_SECONDS]
        return max(0, self.max_per_hour - len(recent))


def check_password(submitted: str, settings: Settings) -> bool:
    expected = settings.admin_password or ""
    if not expected:
        # bootstrap_secrets guarantees a value in a running app; a bare Settings
        # must never become a back door with an empty password.
        log.error("admin password is not configured; refusing login")
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


class AdminGuard(BaseHTTPMiddleware):
    """Loads the session, gates /admin, and rejects cross-site writes."""

    def __init__(self, app, settings: Settings | None = None) -> None:  # noqa: ANN401
        super().__init__(app)
        # Resolved per request unless injected: FP_SESSION_SECRET and
        # FP_ADMIN_PASSWORD are filled in by bootstrap_secrets during startup,
        # which is after this middleware graph has already been built.
        self._settings = settings

    def settings_for(self, request: Request) -> Settings:
        return self._settings if self._settings is not None else request.app.state.settings

    def read(self, request: Request, signer: SessionSigner | None) -> Session | None:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token or signer is None:
            return None
        try:
            session = signer.decode(token)
        except SessionError:
            # A cookie we did not sign, or one whose signature was touched.
            return None
        return None if session.expired else session

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = self.settings_for(request)
        try:
            signer: SessionSigner | None = signer_for(settings)
        except SessionNotConfigured:
            signer = None

        path = request.url.path
        admin = is_admin_path(path)
        session = self.read(request, signer)
        issue: Session | None = None

        if admin and signer is None:
            log.error("admin requested but the session secret is not configured")
            return Response(
                "admin sign-in is unavailable: the session secret is not configured",
                status_code=503,
                media_type="text/plain",
            )

        if admin and session is None:
            # Every admin page, including the login form, needs a token to post
            # with, so hand out an anonymous session rather than a bare form.
            issue = new_session(int(settings.session_ttl_hours * 3600))
            session = issue

        # Set after the anonymous session exists: the templates read this to fill
        # in each form's csrf field, and they must see the token the cookie carries.
        request.state.session = session

        if admin and path not in PUBLIC_ADMIN_PATHS and (session is None or not session.authed):
            return RedirectResponse("/admin/login", status_code=303)

        if admin and request.method in MUTATING:
            problem = await self._csrf_problem(request, session)
            if problem:
                log.info("csrf rejected %s %s (%s)", request.method, path, problem)
                status = 413 if problem == "body too large" else 403
                return Response("csrf check failed", status_code=status, media_type="text/plain")

        response = await call_next(request)
        if issue is not None:
            set_session_cookie(response, settings, issue)
        return response

    async def _csrf_problem(self, request: Request, session: Session | None) -> str:
        if session is None or not session.csrf:
            return "no session"
        supplied = request.headers.get(CSRF_HEADER, "")
        content_type = request.headers.get("content-type", "")

        if not supplied:
            length = request.headers.get("content-length", "")
            if length and length.isdigit() and int(length) > MAX_CSRF_BODY_BYTES:
                return "body too large"
            # Reading the *body*, not the form, is deliberate: Request.form()
            # consumes the stream and Starlette then hands the downstream route an
            # empty body, while body() is replayed to it.
            body = await request.body()
            if content_type.startswith("application/json"):
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return "unreadable json body"
                if isinstance(payload, dict):
                    supplied = str(payload.get(CSRF_FORM_FIELD, ""))
            else:
                supplied = parse_qs(body.decode("utf-8", "replace")).get(CSRF_FORM_FIELD, [""])[0]

        if not supplied or not hmac.compare_digest(supplied, session.csrf):
            return "token mismatch"
        if content_type.startswith("application/json") and not request.headers.get(AJAX_HEADER):
            # A cross-origin document can post a form with a *guessed* token; it
            # cannot set this header without a CORS handshake we never grant.
            return "json write without X-Requested-With"
        return ""


def csrf_input(request: Request) -> Markup:
    """Hidden field for templates: ``{{ csrf_input(request) }}`` inside a form."""
    session: Session | None = getattr(request.state, "session", None)
    token = session.csrf if session is not None else ""
    return Markup(f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{escape(token)}">')


def client_key(request: Request) -> str:
    """Bucket key for the login throttle.

    ``request.client.host`` is the direct peer, which behind a TLS proxy is the
    proxy — so put the throttle in front of the proxy, or accept that the whole
    LAN shares one bucket. On a LAN deployment that is the right trade: one
    attacker guessing still burns the shared budget quickly.
    """
    return request.client.host if request.client is not None else "unknown"
