"""Small helpers that build valid rows for repository tests."""

from __future__ import annotations

from app.domain.models import Endpoint, ModelTarget, Prompt
from app.storage.db import Database
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.prompts import PromptRepo


def make_endpoint(db: Database, name: str = "test-endpoint", models: tuple[str, ...] = ("model-a",)) -> tuple[Endpoint, list[ModelTarget]]:
    repo = EndpointRepo(db)
    endpoint = repo.create(name=name, base_url="http://example.invalid/v1")
    repo.replace_models(endpoint.id, tuple((m, m.upper()) for m in models))
    return endpoint, repo.list_models(endpoint.id)


def make_prompt(db: Database, title: str = "Test Prompt", body: str = "build a thing") -> Prompt:
    return PromptRepo(db).create(title=title, body=body)
