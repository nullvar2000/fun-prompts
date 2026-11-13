"""Composition root.

The only module that wires layers together. ``create_app`` receives resolved
:class:`Settings` and returns the gallery/admin/API application served on
``settings.port``; the artifact origin runs as a separate ASGI app on
``settings.artifact_port`` (added in M4) so generated HTML never shares an
origin, cookies, or API surface with the gallery.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app import logsetup
from app.config import Settings, bootstrap_secrets, load_settings
from app.container import build_container
from app.storage.seed import SeedImporter
from app.web.api.routes_builds import router as builds_router
from app.web.artifact_server import create_artifact_app, start_artifact_server
from app.web.templating import templates
from app.web.security import SecurityHeaders

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR.parent / "gallery_static"

__all__ = ["app", "create_app", "main"]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the gallery/admin/API app. Secrets are resolved during startup."""
    settings = settings or load_settings()
    logsetup.configure(settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        resolved = bootstrap_secrets(application.state.settings)
        application.state.settings = resolved
        log = logsetup.get("app.main")
        log.info("configuration resolved:")
        for key, value in resolved.redacted_dump().items():
            log.info("  %-30s %s", key, value)

        container = build_container(resolved)
        application.state.container = container
        application.state.db = container.db

        if resolved.seed_on_start:
            report = SeedImporter(
                container.db,
                prompts_dir=resolved.prompts_dir,
                seed_apps_dir=resolved.seed_apps_dir,
            ).run()
            if report.skipped:
                log.info("seed import already recorded, skipping")
            else:
                log.info(
                    "seeded %s prompts, %s published builds from %s + %s",
                    report.prompts,
                    report.builds,
                    resolved.prompts_dir,
                    resolved.seed_apps_dir,
                )

        container.worker.recover_interrupted()
        await container.worker.start()

        artifact_server = None
        artifact_task = None
        if resolved.run_artifact_server:
            artifact_app = create_artifact_app(resolved, db=container.db)
            artifact_server, artifact_task = await start_artifact_server(artifact_app, resolved)
            log.info(
                "artifact origin listening on %s:%s (serves artifacts only)",
                resolved.host,
                resolved.artifact_port,
            )

        try:
            yield
        finally:
            if artifact_server is not None and artifact_task is not None:
                artifact_server.should_exit = True  # type: ignore[attr-defined]
                await artifact_task
            await container.worker.stop()

    application = FastAPI(
        title="fun-prompts",
        description=(
            "Gallery of zero-shot applications built by AI, and the builder that makes them."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.add_middleware(SecurityHeaders, settings=settings)

    @application.get("/healthz")
    async def healthz() -> dict[str, object]:
        resolved: Settings = application.state.settings
        return {
            "status": "ok",
            "version": application.version,
            "artifact_origin": resolved.artifact_base_url,
        }

    @application.get("/")
    async def home(request: Request):
        resolved: Settings = request.app.state.settings
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"artifact_origin": resolved.artifact_base_url},
        )

    if STATIC_DIR.is_dir():
        application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    application.include_router(builds_router)

    return application


app = create_app()


def main() -> None:
    import uvicorn

    settings = load_settings()
    logsetup.configure(settings.log_level)
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
