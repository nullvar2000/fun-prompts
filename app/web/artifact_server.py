"""The artifact origin (``settings.artifact_port``, :8081 by default).

This ASGI app serves one thing: stored artifact bytes at
``/artifact/{version_id}``. No cookies, no sessions, no API, no templates, no
static files — so a generated page that misbehaves finds nothing to read and
nothing to call. That is the point of the second origin: the gallery holds the
admin session, this origin holds nothing.

The header set is deliberately different from the gallery origin's
(:mod:`app.web.security`):

* ``script-src 'unsafe-inline'`` — unavoidable, a single-file artifact has to
  carry its own script. Everything else is switched off, so ``default-src
  'none'`` plus ``connect-src 'none'`` means an inline script can paint pixels
  but cannot reach a network.
* ``frame-ancestors`` names the gallery origin exactly (port included), which is
  why ``X-Frame-Options`` is *not* set here: browsers disagree about whether
  ``SAMEORIGIN`` ignores ports, and CSP does not.

Visibility is decided by the database, not by obscurity: an artifact is served
only while its build is ``published`` and neither the build nor its prompt is
hidden, so unpublishing takes effect on the next request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app import logsetup
from app.config import Settings
from app.domain.models import BuildVersion
from app.domain.status import BuildStatus
from app.storage.db import Database
from app.web.security import gallery_origins

log = logsetup.get("app.artifact")

#: Everything not listed is covered by ``default-src 'none'``.
_ARTIFACT_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        # The validator allows data: URIs, so the CSP has to as well — inline
        # images and fonts are how a self-contained page stays self-contained.
        "img-src data:",
        "font-src data:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    ]
)

_NO_STORE = "private, no-cache"


def artifact_csp(settings: Settings) -> str:
    ancestors = " ".join(gallery_origins(settings))
    return f"{_ARTIFACT_CSP}; frame-ancestors {ancestors}"


def artifact_headers(settings: Settings) -> dict[str, str]:
    """Headers for artifact bytes. Also used by the download route on :8080."""
    return {
        "Content-Security-Policy": artifact_csp(settings),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "X-Robots-Tag": "noindex, nofollow",
        # An artifact can be unpublished between views, so nothing here may be
        # cached in a way that outlives the row.
        "Cache-Control": _NO_STORE,
    }


def create_artifact_app(
    settings: Settings,
    *,
    db: Database | None = None,
    version_lookup: Callable[[int], tuple[BuildVersion | None, bool]] | None = None,
) -> Starlette:
    """Build the artifact origin.

    ``version_lookup`` returns ``(version, visible)`` for a version id, with
    ``None`` when there is no such version. The default reads SQLite; tests pass
    their own to exercise the gate without a database.
    """
    get_version = version_lookup
    database: Database | None = db

    if get_version is None:
        lookup_db = database or Database(
            settings.db_path, busy_timeout_ms=settings.db_idle_timeout_ms
        )
        database = lookup_db

        def lookup(version_id: int) -> tuple[BuildVersion | None, bool]:
            row = lookup_db.query_one(
                """
                SELECT v.*, b.status AS build_status, b.hidden AS build_hidden,
                       p.hidden AS prompt_hidden
                FROM build_versions v
                JOIN builds b ON b.id = v.build_id
                JOIN prompts p ON p.id = b.prompt_id
                WHERE v.id = ?
                """,
                (version_id,),
            )
            if row is None:
                return None, False
            visible = (
                str(row["build_status"]) == str(BuildStatus.PUBLISHED)
                and not row["build_hidden"]
                and not row["prompt_hidden"]
            )
            return BuildVersion.from_row(row), visible

        get_version = lookup

    async def artifact(request: Request) -> Response:
        version_id = int(request.path_params["version_id"])
        version, visible = get_version(version_id)
        headers = artifact_headers(settings)

        if not visible or version is None or not version.html:
            # Plain text, not HTML: this origin serves no markup of its own.
            return PlainTextResponse("artifact is not available", status_code=404, headers=headers)

        body = version.html.encode("utf-8")
        if len(body) > settings.max_artifact_bytes:
            # FP_MAX_ARTIFACT_KB is a ceiling, not just a default: a row written
            # under a larger cap stays unserved after the cap is lowered.
            log.warning(
                "version %s is %s bytes, over the %s byte ceiling",
                version_id,
                len(body),
                settings.max_artifact_bytes,
            )
            return PlainTextResponse("artifact exceeds the size ceiling", status_code=413, headers=headers)

        return Response(
            content=body,
            media_type="text/html; charset=utf-8",
            headers={**headers, "Content-Length": str(len(body))},
        )

    async def healthz(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"Cache-Control": _NO_STORE})

    async def not_found(request: Request) -> PlainTextResponse:
        return PlainTextResponse("not found", status_code=404, headers=artifact_headers(settings))

    app = Starlette(
        routes=[
            Route("/artifact/{version_id:int}", artifact, methods=["GET", "HEAD"]),
            Route("/healthz", healthz, methods=["GET", "HEAD"]),
            Route("/{path:path}", not_found, methods=["GET", "HEAD"]),
        ]
    )
    app.state.settings = settings
    app.state.db = database
    return app


async def start_artifact_server(
    app: Starlette, settings: Settings
) -> tuple[object, asyncio.Task]:
    """Run the artifact origin alongside the gallery in one process.

    The gallery's uvicorn owns process signals: a second ``Server`` installing
    its own handlers would take SIGTERM for itself and leave the other one
    running, so those handlers are switched off here and shutdown is driven by
    the caller setting ``should_exit``.
    """
    import uvicorn

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.artifact_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve(), name="artifact-server")
    return server, task
