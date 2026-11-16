"""Gallery pages: what is listed, what is hidden, and how an artifact is embedded.

The important invariants are the ones a screenshot would miss: an unpublished or
hidden build appears nowhere, the artifact is framed from the other origin rather
than inlined, and a download arrives as an attachment.
"""

from __future__ import annotations

from itertools import count


import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.status import BuildStatus
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.prompts import PromptRepo
from app.web.routes_gallery import PAGE_SIZE
from tests.helpers import make_endpoint, make_prompt

ARTIFACT = "<!DOCTYPE html><html><head><title>marker-app</title></head><body><b>MARKER</b></body></html>"
_names = count()


def publish(
    db: Database,
    *,
    title: str | None = None,
    body: str = "build a small app",
    tags: tuple[str, ...] = ("tools",),
    endpoint_name: str | None = None,
    models: tuple[str, ...] = ("model-a",),
    html: str = ARTIFACT,
    status: BuildStatus = BuildStatus.PUBLISHED,
    hidden: bool = False,
    prompt_hidden: bool = False,
    versions: int = 1,
) -> tuple[int, int, int]:
    """One published build: returns (build_id, latest_version_id, model_row_id)."""
    name = endpoint_name or f"ep-{next(_names)}"
    prompt_title = title or f"prompt-{next(_names)}"
    endpoints = EndpointRepo(db)
    endpoint = endpoints.get_by_name(name)
    if endpoint is None:
        endpoint, model_rows = make_endpoint(db, name=name, models=models)
    else:
        model_rows = endpoints.list_models(endpoint.id)
    prompt = PromptRepo(db).create(
        title=prompt_title, body=body, summary=f"summary of {prompt_title}"
    )
    builds = BuildRepo(db)
    build = builds.create(
        prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model_rows[0].id
    )
    version_id = 0
    for _ in range(versions):
        version_id = builds.add_version(
            build.id, html=html, raw_response=html, usage={"prompt_tokens": 3, "completion_tokens": 7}
        ).id
    if status is not BuildStatus.QUEUED:
        for step in (BuildStatus.GENERATING, BuildStatus.VALIDATING, status):
            builds.set_status(build.id, step)
    builds.tag_build(build.id, tags, "manual")
    if hidden:
        builds.set_hidden(build.id, True)
    if prompt_hidden:
        PromptRepo(db).set_hidden(prompt.id, True)
    return build.id, version_id, model_rows[0].id


@pytest.fixture
def seeded(db: Database) -> dict:
    """A predictable little gallery: two models, three tags, four builds."""
    good = publish(db, title="Alpha counter", tags=("tools", "counter"), endpoint_name="shared", models=("m-one", "m-two"))
    beta = publish(db, title="Beta maze", tags=("games", "counter"), endpoint_name="shared", models=("m-two",))
    gamma = publish(db, title="Gamma timer", tags=("tools",), endpoint_name="other")
    hidden = publish(db, title="Hidden thing", tags=("tools",), hidden=True)
    return {"good": good, "beta": beta, "gamma": gamma, "hidden": hidden}


def _links(html: str) -> list[str]:
    return [part.split('"')[0] for part in html.split('href="')[1:]]


# --- home ---------------------------------------------------------------------
def test_home_shows_three_random_published_apps(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    # The seed import publishes exactly three apps.
    assert page.text.count('<article class="card">') == 3
    assert "random" in page.text


def test_home_links_are_safe_relative_paths(client: TestClient) -> None:
    page = client.get("/")
    assert not [link for link in _links(page.text) if link.startswith("http://")]
    assert "artifact_origin" not in page.text  # only the meta tag carries the origin
    assert '<meta name="artifact-origin"' in page.text


def test_home_counts_and_empty_state(bare_client: TestClient, db: Database) -> None:
    empty = bare_client.get("/")
    assert empty.status_code == 200 and "Nothing published yet" in empty.text

    publish(db, title="Something new")
    filled = bare_client.get("/")
    assert "1 published app" in filled.text
    assert "1 prompt" in filled.text
    assert "Something new" in filled.text


# --- browse -------------------------------------------------------------------
def test_gallery_lists_every_visible_build(bare_client: TestClient, seeded: dict) -> None:
    page = bare_client.get("/gallery")
    assert page.status_code == 200
    assert "3 published apps" in page.text  # the fourth is hidden
    for title in ("Alpha counter", "Beta maze", "Gamma timer"):
        assert title in page.text
    assert "Hidden thing" not in page.text


def test_tag_filters_combine_with_and(bare_client: TestClient, seeded: dict) -> None:
    tools = bare_client.get("/gallery?tag=tools").text
    assert "Alpha counter" in tools and "Gamma timer" in tools and "Beta maze" not in tools

    both = bare_client.get("/gallery?tag=tools&tag=counter").text
    assert "Alpha counter" in both and "Gamma timer" not in both

    toggle = bare_client.get("/gallery?tag=tools").text
    assert "/gallery?tag=tools&amp;tag=counter" in toggle or "tag=counter" in toggle


def test_model_and_search_filters(bare_client: TestClient, seeded: dict) -> None:
    model_row_id = seeded["beta"][2]
    page = bare_client.get(f"/gallery?model={model_row_id}").text
    assert "Beta maze" in page and "Gamma timer" not in page

    search = bare_client.get("/gallery?q=maze").text
    assert "Beta maze" in search and "Alpha counter" not in search

    by_tag_text = bare_client.get("/gallery?q=games").text
    assert "Beta maze" in by_tag_text  # tags are searchable too


def test_sorting_and_pagination(bare_client: TestClient, db: Database) -> None:
    # Zero-padded so one title is never a substring of another assertion.
    titles = [f"paged app {n:02d}" for n in range(PAGE_SIZE + 2)]
    for title in titles:
        publish(db, title=title)

    first = bare_client.get("/gallery").text
    second = bare_client.get("/gallery?page=2").text
    assert f"{PAGE_SIZE + 2} published apps" in first
    assert sum(title in first for title in titles) == PAGE_SIZE
    assert sum(title in second for title in titles) == 2
    assert "page 2 of 2" in second

    by_title = bare_client.get("/gallery?sort=title").text
    assert by_title.index("paged app 00") < by_title.index("paged app 01")


def test_a_hidden_prompt_takes_its_builds_out_of_listing(bare_client: TestClient, db: Database) -> None:
    build_id, _, _ = publish(db, title="Embodied", prompt_hidden=True)
    page = bare_client.get("/gallery").text
    assert "Embodied" not in page
    assert bare_client.get(f"/build/{build_id}").status_code in {200, 404}


# --- build detail -------------------------------------------------------------
def test_the_artifact_is_framed_from_the_other_origin_never_inlined(
    bare_client: TestClient, db: Database, settings: Settings
) -> None:
    build_id, version_id, _ = publish(db, title="Framed app")
    page = bare_client.get(f"/build/{build_id}")
    assert page.status_code == 200
    assert "MARKER" not in page.text
    assert f'src="{settings.artifact_base_url}/artifact/{version_id}"' in page.text
    assert 'sandbox="allow-scripts"' in page.text
    assert "allow-same-origin" not in page.text
    assert f"frame-src {settings.artifact_base_url}" in page.headers["content-security-policy"]


def test_build_page_shows_the_llm_provenance(bare_client: TestClient, db: Database) -> None:
    build_id, _, model_row_id = publish(
        db, title="Provenance", endpoint_name="lan-ollama", models=("qwen-7b",)
    )
    page = bare_client.get(f"/build/{build_id}").text
    assert "lan-ollama" in page
    assert "qwen-7b" in page
    assert "7" in page  # completion tokens
    assert "as-is" in page  # extraction source
    assert "/download/" in page


def test_versions_are_linkable_and_marked(bare_client: TestClient, db: Database) -> None:
    build_id, latest, _ = publish(db, title="Refined app", versions=3)
    page = bare_client.get(f"/build/{build_id}").text
    assert "v3" in page and "v1" in page
    assert f"/build/{build_id}?version={latest}" in page
    assert "shown" in page

    single = bare_client.get(f"/build/{build_id}?version={latest}")
    assert single.status_code == 200
    # A version from another build is ignored rather than framed.
    other_build, other_version, _ = publish(db, title="Other app")
    assert f"/artifact/{other_version}" not in bare_client.get(
        f"/build/{build_id}?version={other_version}"
    ).text


@pytest.mark.parametrize(
    "status,label",
    [
        (BuildStatus.QUEUED, "queued"),
        (BuildStatus.GENERATING, "generating"),
        (BuildStatus.REJECTED, "rejected"),
        (BuildStatus.FAILED, "failed"),
        (BuildStatus.CANCELLED, "cancelled"),
    ],
)
def test_an_unpublished_build_explains_itself_without_an_iframe(
    bare_client: TestClient, db: Database, status: BuildStatus, label: str
) -> None:
    build_id, _, _ = publish(db, title="Not published", status=status)
    page = bare_client.get(f"/build/{build_id}")
    assert page.status_code == 200
    assert label in page.text
    assert "<iframe" not in page.text
    if status in {BuildStatus.QUEUED, BuildStatus.GENERATING}:
        assert f'hx-get="/build/{build_id}/status"' in page.text  # it keeps polling
    else:
        assert "hx-get" not in page.text  # terminal, nothing more to ask


def test_the_status_fragment_tracks_the_build(bare_client: TestClient, db: Database) -> None:
    build_id, _, _ = publish(db, title="In flight", status=BuildStatus.GENERATING)
    fragment = bare_client.get(f"/build/{build_id}/status")
    assert fragment.status_code == 200
    assert "generating" in fragment.text and "<iframe" not in fragment.text

    builds = BuildRepo(db)
    builds.set_status(build_id, BuildStatus.VALIDATING)
    builds.set_status(build_id, BuildStatus.PUBLISHED)
    settled = bare_client.get(f"/build/{build_id}/status")
    assert "<iframe" in settled.text and "hx-get" not in settled.text


def test_failure_history_is_summarised_not_dumped(bare_client: TestClient, db: Database) -> None:
    build_id, _, _ = publish(db, title="Rejected app", status=BuildStatus.REJECTED)
    BuildRepo(db).record_failure(
        build_id,
        job_id=None,
        stage="validate",
        error_text="artifact rejected: external-url",
        violations=("external-url",),
        raw_response="SECRET-MODEL-CHATTER",
    )
    page = bare_client.get(f"/build/{build_id}").text
    assert "1 attempt recorded: validate" in page
    assert "SECRET-MODEL-CHATTER" not in page


def test_a_truncated_rejection_says_it_ran_out_of_tokens(bare_client: TestClient, db: Database) -> None:
    build_id, _, _ = publish(db, title="Cut short", status=BuildStatus.REJECTED)
    for _ in range(2):
        BuildRepo(db).record_failure(
            build_id,
            job_id=None,
            stage="truncated",
            error_text="answer cut off at the 8000-token ceiling",
            violations=("missing-or-duplicate-html-close",),
            raw_response="SECRET-CHATTER",
        )
    page = bare_client.get(f"/build/{build_id}").text
    assert "ran out of tokens before the document finished" in page
    assert "2 attempts recorded: truncated, truncated" in page
    assert "SECRET-CHATTER" not in page


def test_hidden_builds_are_not_reachable(bare_client: TestClient, seeded: dict) -> None:
    build_id = seeded["hidden"][0]
    assert bare_client.get(f"/build/{build_id}").status_code == 404
    assert bare_client.get("/gallery?tag=tools").text.count("Hidden thing") == 0


def test_unknown_ids_are_404(bare_client: TestClient) -> None:
    assert bare_client.get("/build/99999").status_code == 404
    assert bare_client.get("/build/99999/status").status_code == 404
    assert bare_client.get("/prompt/nope").status_code == 404
    assert bare_client.get("/download/99999").status_code == 404


# --- prompt detail ------------------------------------------------------------
def test_prompt_page_lists_every_public_build_of_the_prompt(
    bare_client: TestClient, db: Database
) -> None:
    prompt = make_prompt(db, title="Shared prompt", body="a prompt used twice")
    builds = BuildRepo(db)
    endpoint, models = make_endpoint(db, name="twice", models=("m-a", "m-b"))
    ids = []
    for model in models:
        build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model.id)
        builds.add_version(build.id, html=ARTIFACT, raw_response=ARTIFACT)
        for step in (BuildStatus.GENERATING, BuildStatus.VALIDATING, BuildStatus.PUBLISHED):
            builds.set_status(build.id, step)
        ids.append(build.id)

    page = bare_client.get(f"/prompt/{prompt.slug}")
    assert page.status_code == 200
    assert "a prompt used twice" in page.text
    assert "2 published builds" in page.text
    assert "m-a" in page.text and "m-b" in page.text


def test_a_hidden_prompt_page_is_gone(bare_client: TestClient, db: Database) -> None:
    publish(db, title="Buried", prompt_hidden=True)
    prompt = PromptRepo(db).list(include_hidden=True)[-1]
    assert bare_client.get(f"/prompt/{prompt.slug}").status_code == 404


# --- downloads ----------------------------------------------------------------
def test_downloads_arrive_as_attachments(bare_client: TestClient, db: Database) -> None:
    _, version_id, _ = publish(db, title="Download me")
    response = bare_client.get(f"/download/{version_id}")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="download-me-v1.html"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.text == ARTIFACT
    assert "content-security-policy" in response.headers


def test_download_by_build_resolves_the_latest_version(
    bare_client: TestClient, db: Database
) -> None:
    build_id, latest, _ = publish(db, title="Latest please", versions=2)
    by_build = bare_client.get(f"/download/build/{build_id}")
    direct = bare_client.get(f"/download/{latest}")
    assert by_build.status_code == 200
    assert by_build.text == direct.text
    assert "v2" in by_build.headers["content-disposition"]


def test_an_unpublished_version_cannot_be_downloaded(bare_client: TestClient, db: Database) -> None:
    build_id, version_id, _ = publish(db, title="Private build", status=BuildStatus.REJECTED)
    assert bare_client.get(f"/download/{version_id}").status_code == 404
    assert bare_client.get(f"/download/build/{build_id}").status_code == 404
    hidden_build, hidden_version, _ = publish(db, title="Tucked away", hidden=True)
    assert bare_client.get(f"/download/{hidden_version}").status_code == 404
