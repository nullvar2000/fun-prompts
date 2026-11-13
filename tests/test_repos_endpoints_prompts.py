"""Endpoint/model and prompt repositories."""

from __future__ import annotations

import pytest

from app.domain.status import PromptSource
from app.storage.db import Database
from app.storage.repos.builds import BuildRepo
from app.storage.repos.endpoints import EndpointRepo
from app.storage.repos.prompts import PromptRepo


@pytest.fixture
def endpoints(db: Database) -> EndpointRepo:
    return EndpointRepo(db)


def test_create_read_and_update_roundtrip(endpoints: EndpointRepo) -> None:
    created = endpoints.create(
        name="LAN vllm",
        base_url="http://10.0.0.7:8000/v1",
        api_key_encrypted="gAAAA",
        tags=("fast", "Local"),
        timeout_s=90,
        temperature=0.4,
    )
    assert created.id > 0
    # Tags are normalised (case-folded) on the way in.
    assert created.tags == ("fast", "local")

    fetched = endpoints.get(created.id)
    assert fetched is not None and fetched.base_url == "http://10.0.0.7:8000/v1"
    assert fetched.timeout_s == 90 and fetched.temperature == 0.4

    updated = endpoints.update(created.id, enabled=False, tags=("slow",))
    assert updated is not None
    assert updated.enabled is False
    assert updated.tags == ("slow",)
    # Fields we did not touch stay put.
    assert updated.base_url == "http://10.0.0.7:8000/v1"


def test_names_are_unique(endpoints: EndpointRepo) -> None:
    endpoints.create(name="dup", base_url="http://a")
    with pytest.raises(Exception):
        endpoints.create(name="dup", base_url="http://b")


def test_discovery_keeps_manual_disable_flags(endpoints: EndpointRepo) -> None:
    endpoint = endpoints.create(name="ollama", base_url="http://localhost:11434")
    added, removed = endpoints.replace_models(
        endpoint.id, (("llama3", "Llama 3"), ("qwen", "Qwen"))
    )
    assert (added, removed) == (2, 0)

    models = endpoints.list_models(endpoint.id)
    endpoints.set_model_enabled(models[0].id, False)

    # Re-discovery: the same set, plus one new model, minus one that went away.
    added, removed = endpoints.replace_models(
        endpoint.id, (("llama3", "Llama 3.1"), ("qwen", "Qwen"), ("mistral", None))
    )
    assert (added, removed) == (1, 0)
    relabelled = endpoints.list_models(endpoint.id)
    assert {m.model_id for m in relabelled} == {"llama3", "qwen", "mistral"}
    assert next(m for m in relabelled if m.model_id == "llama3").enabled is False
    assert next(m for m in relabelled if m.model_id == "llama3").display_name == "Llama 3.1"

    enabled_only = endpoints.list_models(endpoint.id, enabled_only=True)
    assert "llama3" not in {m.model_id for m in enabled_only}


def test_enabled_targets_only_offers_both_switches_on(endpoints: EndpointRepo) -> None:
    endpoint = endpoints.create(name="on", base_url="http://a")
    off = endpoints.create(name="off", base_url="http://b", enabled=False)
    endpoints.replace_models(endpoint.id, (("good", "Good"), ("paused", "Paused")))
    endpoints.replace_models(off.id, (("ignored", None),))
    endpoints.set_model_enabled(
        next(m.id for m in endpoints.list_models(endpoint.id) if m.model_id == "paused"), False
    )

    targets = endpoints.enabled_targets()
    assert [(e.name, m.model_id) for e, m in targets] == [("on", "good")]


def test_endpoint_delete_cascades_to_models(endpoints: EndpointRepo) -> None:
    endpoint = endpoints.create(name="doomed", base_url="http://a")
    endpoints.replace_models(endpoint.id, (("m1", None),))
    endpoints.delete(endpoint.id)
    assert endpoints.list_models(endpoint.id) == []
    assert endpoints.get(endpoint.id) is None


def test_prompt_slugs_stay_unique(db: Database, endpoints: EndpointRepo) -> None:
    prompts = PromptRepo(db)
    first = prompts.create(title="Dice Roller", body="roll")
    second = prompts.create(title="Dice Roller", body="roll again")
    assert (first.slug, second.slug) == ("dice-roller", "dice-roller-2")
    assert prompts.get_by_slug("dice-roller-2").id == second.id  # type: ignore[union-attr]


def test_fork_records_lineage_without_copying_builds(db: Database) -> None:
    prompts = PromptRepo(db)
    original = prompts.create(title="Palette", body="colors")
    fork = prompts.fork(original.id, body="colors but darker")
    assert fork is not None
    assert fork.source is PromptSource.FORK
    assert fork.parent_prompt_id == original.id
    assert fork.slug.startswith("palette")
    assert PromptRepo(db).get(fork.id).body == "colors but darker"  # type: ignore[union-attr]


def test_hidden_prompts_are_filtered_out_of_default_lists(db: Database) -> None:
    prompts = PromptRepo(db)
    visible = prompts.create(title="Visible", body="v")
    hidden = prompts.create(title="Hidden", body="h")
    prompts.set_hidden(hidden.id, True)

    assert {p.id for p in prompts.list()} == {visible.id}
    assert {p.id for p in prompts.list(include_hidden=True)} == {visible.id, hidden.id}


def test_deleting_a_prompt_cascades_to_its_builds(db: Database, endpoints: EndpointRepo) -> None:
    prompts = PromptRepo(db)
    builds = BuildRepo(db)
    prompt = prompts.create(title="Doomed", body="x")
    endpoint = endpoints.create(name="e", base_url="http://a")
    endpoints.replace_models(endpoint.id, (("m", None),))
    model = endpoints.list_models(endpoint.id)[0]

    build = builds.create(prompt_id=prompt.id, endpoint_id=endpoint.id, model_row_id=model.id)
    builds.add_version(build.id, html="<html></html>", raw_response="<html></html>")
    prompts.delete(prompt.id)

    assert builds.get(build.id) is None
    assert builds.versions(build.id) == []
