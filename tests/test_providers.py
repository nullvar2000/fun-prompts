"""The provider boundary and the fake used by later milestones."""

from __future__ import annotations

import pytest

from app.providers.base import (
    ChatMessage,
    ModelInfo,
    ProviderAdapter,
    ProviderTimeout,
)
from tests.fakes import FakeProvider


def test_fake_provider_satisfies_the_adapter_protocol() -> None:
    assert isinstance(FakeProvider(), ProviderAdapter)


async def test_complete_records_the_call_and_returns_usage() -> None:
    provider = FakeProvider(responses=["<!DOCTYPE html><html>ok</html>"])
    completion = await provider.complete(
        model="fake-model-1",
        messages=[ChatMessage(role="user", content="build a dice roller")],
        max_output_tokens=4000,
        temperature=0.2,
    )
    assert completion.text.startswith("<!DOCTYPE html>")
    assert completion.finish_reason == "stop"
    assert completion.usage["completion_tokens"] > 0
    assert provider.calls[0]["model"] == "fake-model-1"
    assert provider.calls[0]["max_output_tokens"] == 4000


async def test_scripted_failure_surfaces_as_a_provider_error() -> None:
    provider = FakeProvider(fail_with=ProviderTimeout("upstream took too long"))
    with pytest.raises(ProviderTimeout):
        await provider.complete(
            model="fake-model-1",
            messages=[ChatMessage(role="user", content="x")],
            max_output_tokens=100,
        )
    probe = await provider.probe()
    assert probe.ok is False
    assert "too long" in probe.detail


async def test_model_discovery_labels_are_selectable() -> None:
    provider = FakeProvider(models=[ModelInfo("gpt-x"), ModelInfo("gpt-y", "GPT Y")])
    models = await provider.list_models()
    assert [m.model_id for m in models] == ["gpt-x", "gpt-y"]
    assert models[0].label == "gpt-x"
    assert models[1].label == "GPT Y"
