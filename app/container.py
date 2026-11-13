"""Dependency wiring shared by the web app, the worker and the CLI.

``main.py`` is still the composition root, but this module builds the object
graph once so the CLI and the app cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.build.orchestrator import BuildPipeline
from app.config import Settings
from app.domain.models import Endpoint
from app.providers import registry
from app.providers.base import ProviderAdapter
from app.secrets import KeyRing
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.guard import GuardLogRepo
from app.storage.repos.jobs import JobRepo
from app.storage.repos.prompts import PromptRepo
from app.worker.queue import BuildWorker


@dataclass
class Container:
    settings: Settings
    db: Database
    key_ring: KeyRing
    endpoints: EndpointRepo
    prompts: PromptRepo
    builds: BuildRepo
    jobs: JobRepo
    guard_log: GuardLogRepo
    pipeline: BuildPipeline
    worker: BuildWorker
    provider_for: Callable[[Endpoint], ProviderAdapter]


def build_container(settings: Settings) -> Container:
    db = Database(settings.db_path, busy_timeout_ms=settings.db_idle_timeout_ms)
    db.migrate()
    key_ring = KeyRing.load_or_create(settings.key_file)

    endpoints = EndpointRepo(db)
    prompts = PromptRepo(db)
    builds = BuildRepo(db)
    jobs = JobRepo(db)
    guard_log = GuardLogRepo(db)

    def provider_for(endpoint: Endpoint) -> ProviderAdapter:
        return registry.create(
            endpoint,
            api_key=key_ring.decrypt(endpoint.api_key_encrypted),
            settings=settings,
        )

    pipeline = BuildPipeline(db, settings, provider_for=provider_for)
    worker = BuildWorker(jobs=jobs, builds=builds, pipeline=pipeline, settings=settings)

    return Container(
        settings=settings,
        db=db,
        key_ring=key_ring,
        endpoints=endpoints,
        prompts=prompts,
        builds=builds,
        jobs=jobs,
        guard_log=guard_log,
        pipeline=pipeline,
        worker=worker,
        provider_for=provider_for,
    )
