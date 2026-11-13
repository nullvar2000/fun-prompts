"""Small helpers that build valid rows and drive the admin session in tests."""

from __future__ import annotations

import re
from itertools import count

from fastapi.testclient import TestClient

from app.domain.models import Build, Endpoint, ModelTarget, Prompt
from app.domain.status import BuildStatus
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.prompts import PromptRepo
from app.web.auth import CSRF_FORM_FIELD

CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]*)"')
_names = count()


def make_endpoint(db: Database, name: str = "test-endpoint", models: tuple[str, ...] = ("model-a",)) -> tuple[Endpoint, list[ModelTarget]]:
    repo = EndpointRepo(db)
    endpoint = repo.create(name=name, base_url="http://example.invalid/v1")
    repo.replace_models(endpoint.id, tuple((m, m.upper()) for m in models))
    return endpoint, repo.list_models(endpoint.id)


def make_prompt(db: Database, title: str = "Test Prompt", body: str = "build a thing") -> Prompt:
    return PromptRepo(db).create(title=title, body=body)


def sibling_builds(
    db: Database,
    *,
    count: int = 2,
    body: str = "a dice roller with a history list",
    title: str | None = None,
    status: BuildStatus = BuildStatus.PUBLISHED,
) -> tuple[Prompt, list[Build]]:
    """One prompt built ``count`` times by one endpoint's ``count`` models.

    A comparison's worth of builds: the input is identical, only the pair that
    answered differs.
    """
    name = f"sibling-{next(_names)}"
    endpoint, models = make_endpoint(db, name=name, models=tuple(f"m{i}" for i in range(count)))
    prompt = PromptRepo(db).create(title=title or name, body=body)
    builds = BuildRepo(db)
    created: list[Build] = []
    for model in models:
        build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model.id)
        if status is not BuildStatus.QUEUED:
            builds.add_version(
                build.id,
                html="<!DOCTYPE html><html><head><title>app</title></head><body><b>MARK</b></body></html>",
                raw_response="canned",
            )
            for step in (BuildStatus.GENERATING, BuildStatus.VALIDATING, status):
                builds.set_status(build.id, step)
        created.append(build)
    return prompt, created


# --- admin session -----------------------------------------------------------
def csrf_token_on(client: TestClient, path: str = "/admin/login") -> str:
    """The CSRF token the page at ``path`` would submit.

    Read out of the rendered hidden field, so the test breaks if the template
    stops including it — which is the failure mode that matters.
    """
    match = CSRF_FIELD_RE.search(client.get(path).text)
    assert match is not None, f"no {CSRF_FORM_FIELD} field on {path}"
    return match.group(1)


def sign_in(client: TestClient, password: str = "test-admin-password") -> str:
    """Log the client's cookie jar in and return the (now authorised) csrf token."""
    token = csrf_token_on(client)
    response = client.post(
        "/admin/login", data={"password": password, CSRF_FORM_FIELD: token}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return csrf_token_on(client, "/admin")


def admin_post(client: TestClient, path: str, data: dict[str, str], token: str | None = None):  # noqa: ANN201
    """POST a form to /admin with a valid csrf token unless one is passed in."""
    payload = dict(data)
    payload.setdefault(CSRF_FORM_FIELD, token if token is not None else csrf_token_on(client, "/admin"))
    return client.post(path, data=payload, follow_redirects=False)
