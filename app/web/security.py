"""Security headers for the gallery/admin/API origin (port 8080).

The artifact origin on ``settings.artifact_port`` gets its own, much stricter
header set (M4). Keeping the two apart matters: this origin holds the admin
session cookie, the artifact origin must hold nothing.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings

#: Methods that change something, and so have an origin to answer for.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def artifact_origin(settings: Settings) -> str:
    """``http://host:port`` form used inside CSP ``frame-src``."""
    return settings.artifact_base_url.rstrip("/")


def gallery_origin(settings: Settings) -> str:
    """The origin the gallery is browsed at, which frame-ancestors must name.

    Set explicitly with ``FP_GALLERY_BASE_URL``. Otherwise it is derived from
    ``FP_ARTIFACT_BASE_URL`` — already the hostname users browse to — with
    ``FP_PORT`` swapped in. Splitting on the last colon keeps IPv6 literals
    (``[::1]:8081``) intact.
    """
    if settings.gallery_base_url:
        return settings.gallery_base_url.rstrip("/")
    base = settings.artifact_base_url.rstrip("/")
    parsed = urlsplit(base)
    host_part = parsed.netloc
    if parsed.port is not None:
        host_part = host_part.rsplit(":", 1)[0]
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host_part}:{settings.port}"


def gallery_origins(settings: Settings) -> tuple[str, ...]:
    """Every origin allowed to embed an artifact."""
    return (gallery_origin(settings), *settings.extra_frame_ancestors)


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` with a default port dropped, or None.

    The host part is taken from the netloc rather than from ``hostname``,
    because ``hostname`` un-quotes an IPv6 literal and drops the brackets, and
    ``http://[::1]:8081`` has to compare equal to itself.
    """
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        port = parts.port
        # Drop any userinfo, then the port, keeping brackets intact.
        host = parts.netloc.rsplit("@", 1)[-1]
    except ValueError:
        # A header no browser would send: bad port, unbalanced brackets.
        return None
    if port is not None:
        suffix = f":{port}"
        if host.endswith(suffix):
            host = host[: -len(suffix)]
    host = host.lower()
    if not scheme or not host:
        return None
    default = 443 if scheme == "https" else 80
    if port is None or port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def write_origins(request: Request, settings: Settings) -> set[str]:
    """Origins whose writes this app will act on.

    The host the request actually arrived at is included because users reach the
    gallery by whatever name and port the container is published on, which is
    not always what ``FP_GALLERY_BASE_URL`` says. The artifact origin is
    deliberately *not* included: a generated app must not be able to submit
    prompts, revise builds or cancel jobs.
    """
    origins = {origin_of(str(request.base_url))}
    origins.add(origin_of(gallery_origin(settings)))
    return {origin for origin in origins if origin}


def same_origin_write(request: Request, settings: Settings) -> None:
    """Refuse a write that a browser says came from another origin.

    The gallery has no session, so there is no CSRF token to check for the
    public write routes. What it can check is the origin a browser attaches to
    the request: every modern browser sends ``Origin`` on a POST, and this
    origin's ``Referrer-Policy: no-referrer`` means ``Referer`` is normally
    absent, so ``Origin`` is the header that decides.

    A request that carries neither header is not a browser cross-site request —
    it is curl, a health check or a test client — so it is allowed. The point is
    to stop a page on someone else's origin from pushing the builder around, not
    to authenticate callers.
    """
    if request.method not in UNSAFE_METHODS:
        return
    header = request.headers.get("origin") or request.headers.get("referer")
    if not header:
        return
    source = origin_of(header)
    if source is None or source not in write_origins(request, settings):
        raise HTTPException(403, "this form came from another origin")


def build_csp(settings: Settings) -> str:
    frame_ancestors = " ".join(["'none'", *settings.extra_frame_ancestors])
    return "; ".join(
        [
            "default-src 'none'",
            # Everything the gallery serves is local, including htmx.min.js.
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            # Generated apps are embedded from the separate artifact origin only.
            f"frame-src {artifact_origin(settings)}",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            f"frame-ancestors {frame_ancestors}",
        ]
    )


class SecurityHeaders(BaseHTTPMiddleware):
    """Baseline hardening for every response on the app/API origin."""

    def __init__(self, app, settings: Settings) -> None:  # noqa: ANN401
        super().__init__(app)
        self.settings = settings
        self.csp = build_csp(settings)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", self.csp)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
