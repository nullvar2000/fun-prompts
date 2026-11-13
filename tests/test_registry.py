"""The registry is the plugin seam: type lookup, limit clamping, registration."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.domain.models import Endpoint
from app.providers.base import Completion, ModelInfo, ProbeResult
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.registry import (
    UnknownProvider,
    create,
    known_types,
    models_to_rows,
    policy_for,
    register,
    resolve_limits,
)


def endpoint(provider_type: str = "openai_compat", **overrides) -> Endpoint:  # noqa: ANN003
    values = {
        "id": 1,
        "name": "lan",
        "base_url": "http://10.0.0.7:11434/v1",
        "provider_type": provider_type,
    }
    values.update(overrides)
    return Endpoint(**values)


def test_the_default_registry_speaks_openai_compat() -> None:
    adapter = create(endpoint(), api_key="k", settings=Settings())
    assert isinstance(adapter, OpenAICompatProvider)
    assert "openai_compat" in known_types()


def test_an_unregistered_type_lists_what_is_available() -> None:
    with pytest.raises(UnknownProvider, match="known types:"):
        create(endpoint("anthropic"), api_key="k", settings=Settings())


def test_endpoint_limits_are_clamped_by_the_environment_not_just_defaulted() -> None:
    settings = Settings(job_timeout_s=120, max_output_tokens=8000)

    unset = resolve_limits(endpoint(), settings)
    assert unset.timeout_s == 120.0 and unset.max_output_tokens == 8000

    greedy = resolve_limits(
        endpoint(timeout_s=3600, max_output_tokens=999_999), settings
    )
    assert greedy.timeout_s == 120.0
    assert greedy.max_output_tokens == 8000

    modest = resolve_limits(endpoint(timeout_s=30, max_output_tokens=500), settings)
    assert modest.timeout_s == 30.0 and modest.max_output_tokens == 500


async def test_a_new_backend_only_needs_one_registered_factory() -> None:
    class LocalAdapter:
        provider_type = "custom"

        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        async def complete(self, **kwargs) -> Completion:  # noqa: ANN003, ANN002
            return Completion(text="ok", model_returned="custom", finish_reason="stop", latency_ms=1)

        async def list_models(self) -> list[ModelInfo]:
            return [ModelInfo("custom")]

        async def probe(self) -> ProbeResult:
            return ProbeResult(ok=True, detail="custom adapter")

    register("custom", lambda build: LocalAdapter(build.endpoint.base_url))

    adapter = create(endpoint("custom"), api_key="", settings=Settings())
    assert isinstance(adapter, LocalAdapter)
    assert adapter.base_url == "http://10.0.0.7:11434/v1"
    assert (await adapter.complete()).text == "ok"
    assert "custom" in known_types()


def test_url_policy_is_shared_with_the_admin_save_path() -> None:
    settings = Settings(endpoint_allowlist=["ollama.home.arpa", "10.0.0.7:11434"])
    policy = policy_for(settings)
    assert policy.allowlist == ("ollama.home.arpa", "10.0.0.7:11434")
    assert policy.block_metadata is True


def test_discovered_models_become_storable_rows() -> None:
    rows = models_to_rows([ModelInfo("llama3", "Llama 3"), ModelInfo("qwen")])
    assert rows == (("llama3", "Llama 3"), ("qwen", None))
