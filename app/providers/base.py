"""Provider adapter boundary.

Everything above this layer talks to the ``ProviderAdapter`` protocol, never to
an HTTP endpoint directly. Adding a backend that is not OpenAI-compatible means
dropping one module in ``app/providers/`` and registering its type — no edits to
the build pipeline, storage or web layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


class ProviderError(RuntimeError):
    """Base class for every provider-side failure."""


class ProviderUnavailable(ProviderError):
    """Endpoint could not be reached, or rejected the request outright."""


class ProviderTimeout(ProviderError):
    """The request exceeded the configured deadline."""


class BadResponse(ProviderError):
    """The endpoint answered, but not with something we can parse."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True)
class ModelInfo:
    """One selectable model behind an endpoint (a target = endpoint + model)."""

    model_id: str
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.model_id


@dataclass(frozen=True)
class Completion:
    text: str
    model_returned: str
    finish_reason: str
    latency_ms: int
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    model_count: int = 0


@runtime_checkable
class ProviderAdapter(Protocol):
    """What the build pipeline needs from any backend."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> Completion: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def probe(self) -> ProbeResult: ...
