"""The admin panel: what it lets an operator do, and what it refuses.

These run against the real app: the guard middleware, the repositories, the queue
and the pipeline are all production objects. Only the provider adapter is
substituted, through the same registry seam a real backend uses.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.build.orchestrator import Target
from app.config import Settings
from app.domain.status import BuildStatus, JobState
from app.main import create_app
from app.providers import registry
from app.providers.base import ModelInfo, ProviderError
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.prompts import PromptRepo
from tests.fakes import FakeProvider
from tests.helpers import admin_post, sign_in


@pytest.fixture
def panel(client: TestClient) -> TestClient:
    """The seeded app, signed in."""
    sign_in(client)
    return client


def state(client: TestClient):  # noqa: ANN201 - Container
    """The running app's container, so tests read the same rows the app wrote."""
    return client.app.state.container


def notice(response) -> tuple[str, str]:  # noqa: ANN001
    """The flash notice on a 303 as ``(message, kind)``."""
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query.get("msg", [""])[0], query.get("kind", ["ok"])[0]


def make_live_endpoint(client: TestClient, fake: FakeProvider, name: str = "live") -> tuple[int, int]:
    """An endpoint the panel can call, wired to the fake adapter.

    One provider type per fake: the registry is global, so two tests sharing
    ``fake`` would end up probing whichever adapter was registered last.
    """
    provider_type = f"fake-{name}"
    registry.register(provider_type, lambda build: fake)
    endpoints = EndpointRepo(state(client).db)
    endpoint = endpoints.create(name=name, base_url="http://testserver/v1", provider_type=provider_type)
    endpoints.replace_models(endpoint.id, (("m1", "M One"), ("m2", None)))
    models = endpoints.list_models(endpoint.id)
    return endpoint.id, models[0].id


# --- pages -------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/endpoints",
        "/admin/endpoints/new",
        "/admin/prompts",
        "/admin/prompts?show_hidden=1",
        "/admin/builds",
        "/admin/builds?status=published",
        "/admin/builds?status=nonsense",
        "/admin/failures",
        "/admin/guard",
        "/admin/tags",
    ],
)
def test_every_admin_page_renders(panel: TestClient, path: str) -> None:
    response = panel.get(path)
    assert response.status_code == 200, response.text[:400]
    assert "log out" in response.text


def test_dashboard_shows_seeded_state_and_the_worker(panel: TestClient) -> None:
    html = panel.get("/admin").text
    assert "published" in html
    assert "seed" in html  # the seeded endpoint is listed like any other
    assert "queued jobs" in html


def test_pages_link_to_each_other(panel: TestClient) -> None:
    html = panel.get("/admin").text
    for href in (
        'href="/admin/endpoints"',
        'href="/admin/prompts?show_hidden=1"',
        'href="/admin/builds"',
        'href="/admin/failures"',
        'href="/admin/guard"',
        'href="/admin/tags"',
    ):
        assert href in html


# --- creating endpoints ------------------------------------------------------
def test_create_an_endpoint(panel: TestClient) -> None:
    created = admin_post(
        panel,
        "/admin/endpoints",
        {
            "name": "lan-ollama",
            # An IP literal: the guard resolves hostnames before saving, and a
            # test must not depend on DNS.
            "base_url": "http://127.0.0.1:11434/v1/",
            "provider_type": "openai_compat",
            "api_key": "sk-secret-value",
            "tags": "Local, fast",
            "enabled": "1",
            "timeout_s": "45",
            "max_output_tokens": "4000",
            "temperature": "0.4",
        },
    )
    assert created.status_code == 303
    assert "created endpoint lan-ollama" in notice(created)[0]

    endpoint = EndpointRepo(state(panel).db).get_by_name("lan-ollama")
    assert endpoint is not None
    assert endpoint.base_url == "http://127.0.0.1:11434/v1"  # trailing slash trimmed
    assert endpoint.tags == ("fast", "local")  # normalised and sorted
    assert endpoint.timeout_s == 45
    assert endpoint.max_output_tokens == 4000
    assert endpoint.temperature == 0.4
    assert endpoint.api_key_encrypted  # stored, never echoed back

    detail = panel.get(f"/admin/endpoints/{endpoint.id}").text
    assert "sk-secret-value" not in detail
    assert "sk-…" in detail  # masked form only


def test_create_requires_a_name_and_a_real_url(panel: TestClient) -> None:
    missing = admin_post(panel, "/admin/endpoints", {"name": "", "base_url": "http://127.0.0.1:9004/v1"})
    assert "name is required" in notice(missing)[0]

    scheme = admin_post(panel, "/admin/endpoints", {"name": "nope", "base_url": "ftp://a.test/v1"})
    assert notice(scheme)[1] == "error"
    assert EndpointRepo(state(panel).db).get_by_name("nope") is None


def test_create_refuses_a_host_outside_the_allowlist(settings: Settings) -> None:
    strict = replace(settings, seed_on_start=False, endpoint_allowlist=("127.0.0.1",))
    with TestClient(create_app(strict)) as client:
        sign_in(client)
        refused = admin_post(client, "/admin/endpoints", {"name": "offlist", "base_url": "http://10.0.0.5:11434/v1"})
        message, kind = notice(refused)
        assert kind == "error" and "not in FP_ENDPOINT_ALLOWLIST" in message
        assert EndpointRepo(state(client).db).get_by_name("offlist") is None

        allowed = admin_post(client, "/admin/endpoints", {"name": "onlist", "base_url": "http://127.0.0.1:11434/v1"})
        assert notice(allowed)[1] == "ok"


def test_create_refuses_metadata_addresses(panel: TestClient) -> None:
    refused = admin_post(panel, "/admin/endpoints", {"name": "meta", "base_url": "http://169.254.169.254/v1"})
    assert notice(refused)[1] == "error"
    assert EndpointRepo(state(panel).db).get_by_name("meta") is None


def test_duplicate_endpoint_name_is_reported_not_crashed(panel: TestClient) -> None:
    seeded = EndpointRepo(state(panel).db).get_by_name("seed")
    assert seeded is not None
    clash = admin_post(panel, "/admin/endpoints", {"name": "seed", "base_url": "http://127.0.0.1:9005/v1"})
    assert "already exists" in notice(clash)[0]


def test_environment_ceilings_win_over_the_form(panel: TestClient, settings: Settings) -> None:
    created = admin_post(
        panel,
        "/admin/endpoints",
        {
            "name": "greedy",
            "base_url": "http://127.0.0.1:9001/v1",
            "max_output_tokens": str(settings.max_output_tokens + 1),
            "timeout_s": str(settings.job_timeout_s + 60),
        },
    )
    message, kind = notice(created)
    assert kind == "error" and "clamped" in message
    assert EndpointRepo(state(panel).db).get_by_name("greedy") is None

    # A valid pair inside the ceiling is stored as typed.
    ok = admin_post(
        panel,
        "/admin/endpoints",
        {"name": "polite", "base_url": "http://127.0.0.1:9002/v1", "max_output_tokens": "1024", "timeout_s": "30"},
    )
    assert notice(ok)[1] == "ok"
    endpoint = EndpointRepo(state(panel).db).get_by_name("polite")
    assert endpoint is not None and endpoint.max_output_tokens == 1024


def test_bad_numbers_are_reported(panel: TestClient) -> None:
    for field, value in (("timeout_s", "soon"), ("max_output_tokens", "-5"), ("temperature", "9")):
        response = admin_post(
            panel, "/admin/endpoints", {"name": f"x-{field}", "base_url": "http://127.0.0.1:9003/v1", field: value}
        )
        assert notice(response)[1] == "error", field
        assert EndpointRepo(state(panel).db).get_by_name(f"x-{field}") is None


# --- editing endpoints -------------------------------------------------------
def _make_endpoint(client: TestClient, name: str = "editme", key: str = "sk-abcdef0123456789") -> int:
    repo = EndpointRepo(state(client).db)
    endpoint = repo.create(
        name=name, base_url="http://127.0.0.1:9006/v1", api_key_encrypted=state(client).key_ring.encrypt(key)
    )
    return endpoint.id


def test_blank_key_field_keeps_the_stored_key(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel)
    before = state(panel).endpoints.get(endpoint_id)
    assert before is not None and before.api_key_encrypted

    saved = admin_post(panel, f"/admin/endpoints/{endpoint_id}", {"name": "renamed", "base_url": "", "api_key": ""})
    assert notice(saved)[1] == "ok"
    after = state(panel).endpoints.get(endpoint_id)
    assert after is not None
    assert after.name == "renamed"
    assert after.api_key_encrypted == before.api_key_encrypted
    assert state(panel).key_ring.decrypt(after.api_key_encrypted) == "sk-abcdef0123456789"


def test_pasting_back_the_masked_value_does_not_store_the_mask(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel)
    before = state(panel).endpoints.get(endpoint_id)
    admin_post(panel, f"/admin/endpoints/{endpoint_id}", {"name": "x", "api_key": "sk-…6789"})
    after = state(panel).endpoints.get(endpoint_id)
    assert after is not None and before is not None
    assert after.api_key_encrypted == before.api_key_encrypted


def test_clear_key_removes_it(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel)
    admin_post(panel, f"/admin/endpoints/{endpoint_id}", {"name": "x", "clear_key": "1"})
    after = state(panel).endpoints.get(endpoint_id)
    assert after is not None and after.api_key_encrypted == ""
    assert "leave blank to keep" not in panel.get(f"/admin/endpoints/{endpoint_id}").text


def test_blank_limits_clear_the_override(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel)
    admin_post(panel, f"/admin/endpoints/{endpoint_id}", {"name": "x", "max_output_tokens": "2048", "timeout_s": "30"})
    mid = state(panel).endpoints.get(endpoint_id)
    assert mid is not None and mid.max_output_tokens == 2048

    admin_post(panel, f"/admin/endpoints/{endpoint_id}", {"name": "x", "max_output_tokens": "", "timeout_s": ""})
    cleared = state(panel).endpoints.get(endpoint_id)
    assert cleared is not None
    assert cleared.max_output_tokens is None and cleared.timeout_s is None


def test_enabled_toggle(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel)
    admin_post(panel, f"/admin/endpoints/{endpoint_id}/enabled", {"enabled": "0"})
    disabled = state(panel).endpoints.get(endpoint_id)
    assert disabled is not None and disabled.enabled is False
    admin_post(panel, f"/admin/endpoints/{endpoint_id}/enabled", {"enabled": "1"})
    assert state(panel).endpoints.get(endpoint_id).enabled is True


def test_delete_needs_the_name_typed(panel: TestClient) -> None:
    endpoint_id = _make_endpoint(panel, name="todelete")
    wrong = admin_post(panel, f"/admin/endpoints/{endpoint_id}/delete", {"confirm": "something else"})
    assert notice(wrong)[1] == "error"
    assert state(panel).endpoints.get(endpoint_id) is not None

    done = admin_post(panel, f"/admin/endpoints/{endpoint_id}/delete", {"confirm": "todelete"})
    assert "deleted todelete" in notice(done)[0]
    assert state(panel).endpoints.get(endpoint_id) is None
    assert panel.get(f"/admin/endpoints/{endpoint_id}").status_code == 404


def test_the_seed_endpoint_cannot_be_probed(panel: TestClient) -> None:
    seeded = EndpointRepo(state(panel).db).get_by_name("seed")
    assert seeded is not None
    response = admin_post(panel, f"/admin/endpoints/{seeded.id}/probe", {})
    message, kind = notice(response)
    assert kind == "error" and "cannot be probed" in message
    # ...but renaming it is still allowed: its placeholder URL stays a label.
    saved = admin_post(panel, f"/admin/endpoints/{seeded.id}", {"name": "seed", "base_url": "none://seed"})
    assert notice(saved)[1] == "ok"


# --- discovery and probing ---------------------------------------------------
def test_discovery_replaces_the_model_list(panel: TestClient) -> None:
    fake = FakeProvider(models=[ModelInfo("alpha", "Alpha"), ModelInfo("beta")])
    endpoint_id, _ = make_live_endpoint(panel, fake, name="discoverable")
    panel.app.state.container.endpoints.replace_models(endpoint_id, (("stale", None),))

    discovered = admin_post(panel, f"/admin/endpoints/{endpoint_id}/discover", {})
    message, kind = notice(discovered)
    assert kind == "ok" and "discovered 2 models" in message

    models = state(panel).endpoints.list_models(endpoint_id)
    assert [model.model_id for model in models] == ["alpha", "beta"]
    assert models[0].display_name == "Alpha"


def test_discovery_failure_is_reported_not_swallowed(panel: TestClient) -> None:
    fake = FakeProvider(fail_with=ProviderError("401 from /models"))
    endpoint_id, _ = make_live_endpoint(panel, fake, name="refusing")
    response = admin_post(panel, f"/admin/endpoints/{endpoint_id}/discover", {})
    message, kind = notice(response)
    assert kind == "error" and "discovery failed: 401 from /models" in message


def test_probe_reports_reachability(panel: TestClient) -> None:
    good, bad = FakeProvider(), FakeProvider(fail_with=ProviderError("connection refused"))
    good_id, _ = make_live_endpoint(panel, good, name="reachable")
    bad_id, _ = make_live_endpoint(panel, bad, name="down")

    ok_message, ok_kind = notice(admin_post(panel, f"/admin/endpoints/{good_id}/probe", {}))
    assert ok_kind == "ok" and "reachable:" in ok_message

    bad_message, bad_kind = notice(admin_post(panel, f"/admin/endpoints/{bad_id}/probe", {}))
    assert bad_kind == "error" and "NOT reachable" in bad_message


def test_model_toggle_and_ownership_check(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="toggleable")
    admin_post(panel, f"/admin/endpoints/{endpoint_id}/models/{model_id}/enabled", {"enabled": "0"})
    assert state(panel).endpoints.get_model(model_id).enabled is False
    admin_post(panel, f"/admin/endpoints/{endpoint_id}/models/{model_id}/enabled", {"enabled": "1"})
    assert state(panel).endpoints.get_model(model_id).enabled is True

    other = EndpointRepo(state(panel).db).create(name="other", base_url="http://127.0.0.1:9007/v1")
    wrong = admin_post(panel, f"/admin/endpoints/{other.id}/models/{model_id}/enabled", {"enabled": "0"})
    assert wrong.status_code == 404


# --- prompts -----------------------------------------------------------------
def test_prompt_edit_hide_fork_and_delete(panel: TestClient, settings: Settings) -> None:
    prompts = PromptRepo(state(panel).db)
    prompt = prompts.create(title="Original", body="make a thing", summary="")
    prompt_id = prompt.id

    edited = admin_post(panel, f"/admin/prompts/{prompt_id}", {"title": "Renamed", "body": "make a better thing"})
    assert "saved prompt" in notice(edited)[0]
    renamed = prompts.get(prompt_id)
    assert renamed is not None and renamed.title == "Renamed"

    too_long = admin_post(panel, f"/admin/prompts/{prompt_id}", {"body": "x" * (settings.max_prompt_chars + 1)})
    assert "over the limit" in notice(too_long)[0]

    empty = admin_post(panel, f"/admin/prompts/{prompt_id}", {"body": "   "})
    assert "cannot be empty" in notice(empty)[0]

    admin_post(panel, f"/admin/prompts/{prompt_id}/hidden", {"hidden": "1"})
    assert prompts.get(prompt_id).hidden is True
    assert prompt.slug not in panel.get("/admin/prompts").text
    assert prompt.slug in panel.get("/admin/prompts?show_hidden=1").text

    admin_post(panel, f"/admin/prompts/{prompt_id}/hidden", {"hidden": "0"})
    assert prompts.get(prompt_id).hidden is False

    forked = admin_post(panel, f"/admin/prompts/{prompt_id}/fork", {"title": "Derived"})
    assert "forked" in notice(forked)[0]
    copies = [item for item in prompts.list(include_hidden=True) if item.parent_prompt_id == prompt_id]
    assert len(copies) == 1 and copies[0].title == "Derived"

    refused = admin_post(panel, f"/admin/prompts/{prompt_id}/delete", {"confirm": "no"})
    assert notice(refused)[1] == "error"
    done = admin_post(panel, f"/admin/prompts/{prompt_id}/delete", {"confirm": "delete"})
    assert "deleted" in notice(done)[0]
    assert prompts.get(prompt_id) is None


def test_prompt_rerun_queues_against_the_chosen_targets(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="rerun-target")
    prompt = PromptRepo(state(panel).db).create(title="Rerun me", body="build a calculator")

    queued = admin_post(panel, f"/admin/prompts/{prompt.id}/run", {"targets": f"{endpoint_id}:{model_id}"})
    message, kind = notice(queued)
    assert kind == "ok" and "queued 1 build(s)" in message

    builds = state(panel).builds.for_prompt(prompt.id)
    assert len(builds) == 1 and builds[0].status is BuildStatus.QUEUED

    none_chosen = admin_post(panel, f"/admin/prompts/{prompt.id}/run", {})
    assert "choose at least one" in notice(none_chosen)[0]

    disabled = admin_post(panel, f"/admin/prompts/{prompt.id}/run", {"targets": "999999:999999"})
    assert notice(disabled)[1] == "error"


def test_prompt_page_lists_targets_only_from_enabled_endpoints(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="pickable")
    endpoints = state(panel).endpoints
    for model in endpoints.list_models(endpoint_id):
        endpoints.set_model_enabled(model.id, False)
    PromptRepo(state(panel).db).create(title="Listed", body="a prompt")

    assert "pickable" not in panel.get("/admin/prompts?show_hidden=1").text
    endpoints.set_model_enabled(model_id, True)
    html = panel.get("/admin/prompts?show_hidden=1").text
    assert "pickable · M One" in html
    assert "pickable · m2" not in html


# --- builds ------------------------------------------------------------------
def _published_build(client: TestClient) -> int:
    published = state(client).builds.list(statuses=(BuildStatus.PUBLISHED,), limit=1)
    assert published
    return published[0].id


def test_build_hide_and_unhide(panel: TestClient) -> None:
    build_id = _published_build(panel)
    admin_post(panel, f"/admin/builds/{build_id}/hidden", {"hidden": "1"})
    assert state(panel).builds.get(build_id).hidden is True
    assert f"build {build_id} hidden" in notice(admin_post(panel, f"/admin/builds/{build_id}/hidden", {"hidden": "1"}))[0]
    admin_post(panel, f"/admin/builds/{build_id}/hidden", {"hidden": "0"})
    assert state(panel).builds.get(build_id).hidden is False
    # Hiding also takes it out of the public gallery.
    admin_post(panel, f"/admin/builds/{build_id}/hidden", {"hidden": "1"})
    assert f"/build/{build_id}" not in panel.get("/gallery").text
    admin_post(panel, f"/admin/builds/{build_id}/hidden", {"hidden": "0"})
    assert f"/build/{build_id}" in panel.get("/gallery").text


def test_build_delete_needs_the_word(panel: TestClient) -> None:
    build_id = _published_build(panel)
    assert notice(admin_post(panel, f"/admin/builds/{build_id}/delete", {"confirm": "yes"}))[1] == "error"
    assert state(panel).builds.get(build_id) is not None
    admin_post(panel, f"/admin/builds/{build_id}/delete", {"confirm": "delete"})
    assert state(panel).builds.get(build_id) is None


def test_published_builds_are_not_regenerated_by_retry(panel: TestClient) -> None:
    build_id = _published_build(panel)
    message, kind = notice(admin_post(panel, f"/admin/builds/{build_id}/retry", {}))
    assert kind == "error" and "refine adds a version" in message
    assert state(panel).builds.get(build_id).status is BuildStatus.PUBLISHED


def test_failed_build_can_be_requeued(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="retryable")
    prompt = PromptRepo(state(panel).db).create(title="Retry me", body="build a clock")
    submitted = state(panel).pipeline.rerun(prompt.id, [Target(endpoint_id, model_id)])
    build = submitted.builds[0]
    state(panel).builds.set_status(build.id, BuildStatus.FAILED)

    requeued = admin_post(panel, f"/admin/builds/{build.id}/retry", {})
    message, kind = notice(requeued)
    assert kind == "ok" and "requeued build" in message
    assert state(panel).builds.get(build.id).status is BuildStatus.QUEUED
    assert any(job.state is JobState.QUEUED for job in state(panel).jobs.for_build(build.id))


def test_retry_refuses_a_disabled_endpoint(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="off-endpoint")
    prompt = PromptRepo(state(panel).db).create(title="Nope", body="build a thing")
    build = state(panel).pipeline.rerun(prompt.id, [Target(endpoint_id, model_id)]).builds[0]
    state(panel).builds.set_status(build.id, BuildStatus.FAILED)
    state(panel).endpoints.update(endpoint_id, enabled=False)

    message, kind = notice(admin_post(panel, f"/admin/builds/{build.id}/retry", {}))
    assert kind == "error" and "endpoint is gone or disabled" in message


def test_cancel_reports_when_nothing_is_running(panel: TestClient) -> None:
    build_id = _published_build(panel)
    message, kind = notice(admin_post(panel, f"/admin/builds/{build_id}/cancel", {}))
    assert kind == "error" and "nothing running" in message


def test_manual_tags_can_be_added_and_removed(panel: TestClient) -> None:
    build_id = _published_build(panel)
    added = admin_post(panel, f"/admin/builds/{build_id}/tags", {"tag": "  Favourite  "})
    assert "tagged build" in notice(added)[0]
    assert "favourite" in state(panel).builds.get(build_id).tags

    blank = admin_post(panel, f"/admin/builds/{build_id}/tags", {"tag": "  "})
    assert notice(blank)[1] == "error"

    admin_post(panel, f"/admin/builds/{build_id}/tags/remove", {"tag": "favourite"})
    assert "favourite" not in state(panel).builds.get(build_id).tags


def test_builds_page_names_the_endpoint_and_model(panel: TestClient) -> None:
    fake = FakeProvider()
    endpoint_id, model_id = make_live_endpoint(panel, fake, name="named-endpoint")
    prompt = PromptRepo(state(panel).db).create(title="Named", body="build a thing")
    build = state(panel).pipeline.rerun(prompt.id, [Target(endpoint_id, model_id)]).builds[0]
    html = panel.get("/admin/builds").text
    assert "named-endpoint" in html
    assert "M One" in html or state(panel).endpoints.get_model(model_id).model_id in html
    assert f"/build/{build.id}" in html


# --- logs --------------------------------------------------------------------
def test_failures_page_shows_the_raw_response_as_text(panel: TestClient) -> None:
    build_id = _published_build(panel)
    payload = "<script>alert('from the endpoint')</script>"
    state(panel).builds.record_failure(
        build_id, job_id=None, stage="validate", error_text="no html found", violations=("missing-doctype",), raw_response=payload
    )
    html = panel.get("/admin/failures").text
    assert "&lt;script&gt;alert" in html
    assert "<script>alert('from the endpoint')" not in html
    assert "missing-doctype" in html


def test_failures_are_scoped_to_one_endpoint_on_its_page(panel: TestClient) -> None:
    build_id = _published_build(panel)
    state(panel).builds.record_failure(build_id, job_id=None, stage="provider", error_text="boom on seed")
    seeded = EndpointRepo(state(panel).db).get_by_name("seed")
    other = EndpointRepo(state(panel).db).create(name="clean", base_url="http://127.0.0.1:9008/v1")
    assert "boom on seed" in panel.get(f"/admin/endpoints/{seeded.id}").text
    assert "boom on seed" not in panel.get(f"/admin/endpoints/{other.id}").text


def test_guard_page_lists_detector_entries(panel: TestClient) -> None:
    state(panel).guard_log.log(matched_rule="override-marker", snippet="ignore previous instructions", action="block")
    html = panel.get("/admin/guard").text
    assert "override-marker" in html
    assert "ignore previous instructions" in html
    assert "block" in html


def test_tags_page_shows_where_a_tag_came_from(panel: TestClient) -> None:
    build_id = _published_build(panel)
    state(panel).builds.tag_build(build_id, ("handpicked",), "manual")
    html = panel.get("/admin/tags").text
    assert "handpicked" in html
    assert "manual" in html
    assert "seed" in html and "endpoint" in html
