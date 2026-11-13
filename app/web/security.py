"""Security headers for the gallery/admin/API origin (port 8080).

The artifact origin on ``settings.artifact_port`` gets its own, much stricter
header set (M4). Keeping the two apart matters: this origin holds the admin
session cookie, the artifact origin must hold nothing.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings


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
