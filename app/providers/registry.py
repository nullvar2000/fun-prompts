"""Provider registry: mapping ``provider_type`` to an adapter instance.

This is the plugin seam. Supporting a backend with a different wire format means
writing one adapter and registering it here — the build pipeline, queue and web
layers only ever see the ``ProviderAdapter`` protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.config import Settings
from app.domain.models import Endpoint
from app.providers.base import ModelInfo, ProviderAdapter
from app.providers.openai_compat import OpenAICompatProvider
from app.providers.url_guard import UrlPolicy

ProviderFactory = Callable[["ProviderBuild"], ProviderAdapter]


class UnknownProvider(RuntimeError):
    """The endpoint names a provider type nothing is registered for."""


@dataclass(frozen=True)
class Limits:
    """Effective transport limits for one endpoint."""

    timeout_s: float
    max_output_tokens: int


@dataclass(frozen=True)
class ProviderBuild:
    endpoint: Endpoint
    api_key: str
    settings: Settings
    transport: httpx.AsyncBaseTransport | None = None


def policy_for(settings: Settings) -> UrlPolicy:
    """The URL policy an admin save should validate against too."""
    return UrlPolicy(
        allowlist=tuple(settings.endpoint_allowlist),
        block_metadata=settings.block_metadata_ips,
    )


def resolve_limits(endpoint: Endpoint, settings: Settings) -> Limits:
    """Endpoint values, clamped by the global config.

    The environment is a ceiling, not just a default: an endpoint asking for a
    longer timeout or a bigger completion than the deployment allows is clamped
    rather than honoured.
    """
    requested_timeout = endpoint.timeout_s or settings.job_timeout_s
    requested_tokens = endpoint.max_output_tokens or settings.max_output_tokens
    return Limits(
        timeout_s=float(min(requested_timeout, settings.job_timeout_s)),
        max_output_tokens=min(requested_tokens, settings.max_output_tokens),
    )


def _build_openai_compat(build: ProviderBuild) -> ProviderAdapter:
    limits = resolve_limits(build.endpoint, build.settings)
    return OpenAICompatProvider(
        base_url=build.endpoint.base_url,
        api_key=build.api_key,
        timeout_s=limits.timeout_s,
        policy=policy_for(build.settings),
        transport=build.transport,
    )


_REGISTRY: dict[str, ProviderFactory] = {"openai_compat": _build_openai_compat}


def register(provider_type: str, factory: ProviderFactory) -> None:
    """Add a provider type. Adapters self-register at import time if desired."""
    _REGISTRY[provider_type] = factory


def known_types() -> Sequence[str]:
    return tuple(sorted(_REGISTRY))


def create(
    endpoint: Endpoint,
    *,
    api_key: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderAdapter:
    factory = _REGISTRY.get(endpoint.provider_type)
    if factory is None:
        raise UnknownProvider(
            f"no provider registered for type {endpoint.provider_type!r}; "
            f"known types: {', '.join(known_types())}"
        )
    return factory(ProviderBuild(endpoint=endpoint, api_key=api_key, settings=settings, transport=transport))


def models_to_rows(models: Sequence[ModelInfo]) -> tuple[tuple[str, str | None], ...]:
    """Adapter output → the shape the model repository stores."""
    return tuple((model.model_id, model.display_name) for model in models)
