"""Fake provider used by unit and end-to-end tests.

Lives in ``tests/`` on purpose: production code must never be able to reach it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from app.providers.base import (
    ChatMessage,
    Completion,
    ModelInfo,
    ProbeResult,
    ProviderError,
)


class FakeProvider:
    """Scripted stand-in for an OpenAI-compatible endpoint."""

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        models: Sequence[ModelInfo] | None = None,
        fail_with: ProviderError | None = None,
        model_returned: str = "fake-model-1",
        delay_s: float = 0.0,
    ) -> None:
        self.responses = list(responses or ['<!DOCTYPE html><html><body>ok</body></html>'])
        self.models = list(
            models
            or [ModelInfo("fake-model-1", "Fake Model One"), ModelInfo("fake-model-2")]
        )
        self.fail_with = fail_with
        self.model_returned = model_returned
        self.delay_s = delay_s
        self.calls: list[dict] = []
        #: Concurrency bookkeeping, so the worker's cap can be asserted.
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> Completion:
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "max_output_tokens": max_output_tokens,
                "temperature": temperature,
            }
        )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if self.fail_with is not None:
                raise self.fail_with
            text = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        finally:
            self.in_flight -= 1
        completion_tokens = len(text) // 4
        return Completion(
            text=text,
            model_returned=self.model_returned,
            finish_reason="stop",
            latency_ms=12,
            usage={
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
                "total_tokens": 10 + completion_tokens,
            },
        )

    async def list_models(self) -> list[ModelInfo]:
        # Real adapters raise for a failed /models call too, so discovery sees the
        # same error type the completion path raises.
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.models)

    async def probe(self) -> ProbeResult:
        if self.fail_with is not None:
            return ProbeResult(ok=False, detail=str(self.fail_with))
        return ProbeResult(ok=True, detail="fake provider reachable", model_count=len(self.models))
