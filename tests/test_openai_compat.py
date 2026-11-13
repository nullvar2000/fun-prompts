"""The OpenAI-compatible adapter, driven by a scripted ASGI stand-in."""

from __future__ import annotations

import httpx
import pytest

from app.providers.base import (
    BadResponse,
    ChatMessage,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.url_guard import UrlNotAllowed, UrlPolicy
from tests.fake_openai_server import FakeOpenAIServer

BASE = "http://testserver/v1"
LOCAL_POLICY = UrlPolicy(resolve_dns=False)


def provider(server: FakeOpenAIServer, **kwargs) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=kwargs.pop("base_url", BASE),
        transport=httpx.ASGITransport(app=server),
        policy=kwargs.pop("policy", LOCAL_POLICY),
        **kwargs,
    )


async def test_completion_carries_text_usage_and_latency() -> None:
    server = FakeOpenAIServer(completion="<!DOCTYPE html><html>hi</html>")
    completion = await provider(server).complete(
        model="fake-1",
        messages=[ChatMessage(role="user", content="make a thing")],
        max_output_tokens=1000,
    )
    assert completion.text == "<!DOCTYPE html><html>hi</html>"
    assert completion.model_returned == "fake-1"
    assert completion.finish_reason == "stop"
    assert completion.usage["total_tokens"] == 33
    assert completion.latency_ms >= 0


async def test_request_shape_is_the_openai_wire_format() -> None:
    server = FakeOpenAIServer()
    await provider(server).complete(
        model="fake-1",
        messages=[
            ChatMessage(role="system", content="build it"),
            ChatMessage(role="user", content="a dice roller"),
        ],
        max_output_tokens=512,
        temperature=0.3,
    )
    request = server.requests[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/v1/chat/completions"
    body = request["body"]
    assert body["model"] == "fake-1"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.3
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


async def test_temperature_is_omitted_unless_requested() -> None:
    server = FakeOpenAIServer()
    await provider(server).complete(
        model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
    )
    assert "temperature" not in server.requests[-1]["body"]


async def test_bearer_token_is_sent_only_when_a_key_exists() -> None:
    keyed = FakeOpenAIServer(require_bearer="secret-key")
    completion = await provider(keyed, api_key="secret-key").complete(
        model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
    )
    assert completion.text

    unkeyed = FakeOpenAIServer()
    await provider(unkeyed).complete(
        model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
    )
    assert "authorization" not in unkeyed.requests[-1]["headers"]


async def test_wrong_key_surfaces_the_status_and_a_clipped_body() -> None:
    server = FakeOpenAIServer(require_bearer="expected")
    with pytest.raises(ProviderUnavailable) as exc:
        await provider(server, api_key="wrong").complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )
    assert "401" in str(exc.value)


async def test_a_timeout_becomes_a_provider_timeout_not_a_hang() -> None:
    # httpx's ASGITransport runs the app inline and does not enforce read
    # timeouts, so the deadline path is exercised at the transport level. What
    # matters is the mapping: a timeout must surface as ProviderTimeout.
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    adapter = OpenAICompatProvider(
        base_url=BASE,
        timeout_s=0.05,
        policy=LOCAL_POLICY,
        transport=httpx.MockTransport(slow),
    )
    with pytest.raises(ProviderTimeout, match="exceeded"):
        await adapter.complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )


async def test_an_unreachable_endpoint_is_reported_as_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = OpenAICompatProvider(
        base_url=BASE, policy=LOCAL_POLICY, transport=httpx.MockTransport(refuse)
    )
    with pytest.raises(ProviderUnavailable, match="connection refused"):
        await adapter.list_models()

    probe = await adapter.probe()
    assert probe.ok is False and "connection refused" in probe.detail


async def test_redirects_are_refused_not_followed() -> None:
    server = FakeOpenAIServer(redirect_to="http://elsewhere.invalid/v1/chat/completions")
    with pytest.raises(ProviderUnavailable, match="redirects are not followed"):
        await provider(server).complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )


async def test_non_json_body_is_a_bad_response() -> None:
    server = FakeOpenAIServer(raw_body="<html>gateway blew up</html>")
    with pytest.raises(BadResponse, match="not JSON"):
        await provider(server).complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )


async def test_missing_choices_or_content_are_bad_responses() -> None:
    empty = FakeOpenAIServer(raw_body='{"choices": [], "model": "fake-1"}')
    with pytest.raises(BadResponse, match="no choices"):
        await provider(empty).complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )

    no_content = FakeOpenAIServer(raw_body='{"choices": [{"message": {"role": "assistant"}}]}')
    with pytest.raises(BadResponse, match="no assistant message content"):
        await provider(no_content).complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )


async def test_content_parts_lists_are_joined() -> None:
    server = FakeOpenAIServer(
        raw_body=(
            '{"choices": [{"finish_reason": "stop", "message": {"role": "assistant", '
            '"content": [{"type": "text", "text": "<html>"}, {"type": "text", "text": "</html>"}]}}]}'
        )
    )
    completion = await provider(server).complete(
        model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
    )
    assert completion.text == "<html></html>"
    # A usage block we did not send is tolerated, and non-integers are dropped.
    assert completion.usage == {}


async def test_model_discovery_maps_ids_and_names() -> None:
    server = FakeOpenAIServer(models=({"id": "llama3", "name": "Llama 3"}, {"id": "qwen"}))
    models = await provider(server).list_models()
    assert [(m.model_id, m.label) for m in models] == [("llama3", "Llama 3"), ("qwen", "qwen")]
    assert server.requests[-1]["method"] == "GET"
    assert server.requests[-1]["path"] == "/v1/models"


async def test_model_list_with_wrong_shape_is_a_bad_response() -> None:
    server = FakeOpenAIServer(raw_body='{"models": ["nope"]}')
    with pytest.raises(BadResponse, match="data"):
        await provider(server).list_models()


async def test_probe_reports_reachable_and_unreachable() -> None:
    ok = await provider(FakeOpenAIServer()).probe()
    assert ok.ok and ok.model_count == 2 and "2 models" in ok.detail

    broken = await provider(FakeOpenAIServer(status=503)).probe()
    assert broken.ok is False and "503" in broken.detail


async def test_url_policy_is_enforced_before_any_request() -> None:
    server = FakeOpenAIServer()
    adapter = provider(
        server,
        base_url="http://169.254.169.254/latest/v1",
        policy=UrlPolicy(resolve_dns=False),
    )
    with pytest.raises(UrlNotAllowed, match="blocked address"):
        await adapter.complete(
            model="fake-1", messages=[ChatMessage(role="user", content="x")], max_output_tokens=10
        )
    # The guard ran first: the fake server never saw a request.
    assert server.requests == []


async def test_allowlist_rejects_an_unlisted_host() -> None:
    server = FakeOpenAIServer()
    adapter = provider(
        server, base_url="http://10.0.0.9:11434/v1", policy=UrlPolicy(allowlist=("ollama.home",))
    )
    with pytest.raises(UrlNotAllowed, match="not in FP_ENDPOINT_ALLOWLIST"):
        await adapter.list_models()


async def test_aclose_is_idempotent() -> None:
    adapter = provider(FakeOpenAIServer())
    await adapter.list_models()
    await adapter.aclose()
    await adapter.aclose()
