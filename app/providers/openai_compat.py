"""OpenAI-compatible provider adapter.

Covers the shape nearly every local and hosted server speaks:
``POST {base}/chat/completions`` and ``GET {base}/models``. The base URL is
expected to include the API prefix (``http://host:11434/v1``); paths are joined
onto it literally rather than guessed at, because the servers differ and a
silent rewrite is harder to debug than a 404.

Nothing here is retry-aware. Retries, attempts and backoff belong to the job
queue, which is the only place that knows whether a rerun is affordable.
"""

from __future__ import annotations

import time
from typing import Sequence

import httpx

from app import logsetup
from app.providers.base import (
    BadResponse,
    ChatMessage,
    Completion,
    ModelInfo,
    ProbeResult,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.url_guard import UrlNotAllowed, UrlPolicy, validate_endpoint_url

log = logsetup.get("app.providers.openai_compat")

CONNECT_TIMEOUT_S = 10.0
MAX_ERROR_BODY_CHARS = 400
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class OpenAICompatProvider:
    provider_type = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_s: float = 120.0,
        policy: UrlPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.policy = policy or UrlPolicy()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # --- lifecycle ----------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"accept": "application/json", "user-agent": "fun-prompts/0.1"}
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                # A redirect to another host is a re-configuration decision, not
                # something to obey: following it would route around the URL policy.
                follow_redirects=False,
                timeout=httpx.Timeout(self.timeout_s, connect=CONNECT_TIMEOUT_S),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_allowed(self) -> None:
        """Re-checked before every request, not just at construction."""
        await validate_endpoint_url(self.base_url, self.policy)

    # --- the adapter protocol ------------------------------------------------
    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> Completion:
        await self._ensure_allowed()

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_output_tokens,
            "stream": False,
        }
        # Only send temperature when asked: servers disagree on whether an
        # explicit 0.7 is acceptable, and an omitted field is unambiguous.
        if temperature is not None:
            payload["temperature"] = temperature

        started = time.perf_counter()
        response = await self._post("/chat/completions", payload)
        latency_ms = int((time.perf_counter() - started) * 1000)

        data = self._json(response)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BadResponse("response contained no choices")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise BadResponse("malformed choice")
        text = _extract_text(choice.get("message"))
        if text is None:
            raise BadResponse("choice has no assistant message content")

        # ``usage`` may be absent, null, or full of strings depending on the
        # server, so normalise before reading it.
        usage_raw = data.get("usage")
        usage = {
            key: int(value)
            for key, value in (usage_raw.items() if isinstance(usage_raw, dict) else ())
            if isinstance(value, int)
        }
        return Completion(
            text=text,
            model_returned=str(data.get("model") or model),
            finish_reason=str(choice.get("finish_reason") or ""),
            latency_ms=latency_ms,
            usage=usage,
        )

    async def list_models(self) -> list[ModelInfo]:
        await self._ensure_allowed()
        response = await self._post_or_get("/models", method="GET")
        data = self._json(response)
        entries = data.get("data")
        if not isinstance(entries, list):
            raise BadResponse("model list is missing a 'data' array")

        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            model_id = str(entry["id"])
            display = entry.get("name") or entry.get("display_name")
            models.append(
                ModelInfo(model_id=model_id, display_name=str(display) if display else None)
            )
        if not models:
            raise BadResponse("model list was empty")
        return models

    async def probe(self) -> ProbeResult:
        try:
            models = await self.list_models()
        except (ProviderError, UrlNotAllowed) as exc:
            return ProbeResult(ok=False, detail=str(exc))
        return ProbeResult(
            ok=True, detail=f"reachable, {len(models)} models offered", model_count=len(models)
        )

    # --- transport plumbing --------------------------------------------------
    async def _post(self, path: str, payload: dict) -> httpx.Response:
        return await self._post_or_get(path, method="POST", payload=payload)

    async def _post_or_get(
        self, path: str, *, method: str, payload: dict | None = None
    ) -> httpx.Response:
        client = self._http()
        try:
            response = await client.request(
                method, path, json=payload if payload is not None else None
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{method} {path} exceeded {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{method} {path}: {exc}") from exc

        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location", "?")
            raise ProviderUnavailable(
                f"{method} {path} redirected to {location}; redirects are not followed"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"{method} {path} -> HTTP {response.status_code}: {_clip(response.text)}"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise BadResponse(f"response was not JSON: {_clip(response.text)}") from exc
        if not isinstance(data, dict):
            raise BadResponse("response JSON was not an object")
        return data


def _extract_text(message: object) -> str | None:
    """Return assistant text, tolerating content-parts responses."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        joined = "".join(parts)
        return joined or None
    return None


def _clip(body: str) -> str:
    body = (body or "").strip().replace("\n", " ")
    return body if len(body) <= MAX_ERROR_BODY_CHARS else body[:MAX_ERROR_BODY_CHARS] + "…"
