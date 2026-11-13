"""Endpoint URL policy: allowlist plus always-refused targets."""

from __future__ import annotations

import pytest

from app.providers.url_guard import UrlNotAllowed, UrlPolicy, parse_endpoint_url, validate_endpoint_url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host/v1",
        "file:///etc/passwd",
        "http://",
        "",
        "http://user:pass@host/v1",
    ],
)
def test_unusable_or_trickery_urls_are_rejected(url: str) -> None:
    with pytest.raises(UrlNotAllowed):
        parse_endpoint_url(url)


def test_parsing_keeps_host_port_and_path() -> None:
    parsed = parse_endpoint_url("http://ollama.home.arpa:11434/v1/")
    assert parsed.host == "ollama.home.arpa"
    assert parsed.port == 11434
    assert parsed.host_port == "ollama.home.arpa:11434"
    assert parsed.path == "/v1/"


async def test_allowlist_accepts_host_and_host_port_forms() -> None:
    by_host = UrlPolicy(allowlist=("ollama.home.arpa",), resolve_dns=False)
    assert await validate_endpoint_url("http://ollama.home.arpa:11434/v1", by_host)

    by_host_port = UrlPolicy(allowlist=("ollama.home.arpa:11434",), resolve_dns=False)
    assert await validate_endpoint_url("http://ollama.home.arpa:11434/v1", by_host_port)

    with pytest.raises(UrlNotAllowed, match="not in FP_ENDPOINT_ALLOWLIST"):
        await validate_endpoint_url(
            "http://elsewhere:11434/v1", UrlPolicy(allowlist=("ollama.home.arpa",), resolve_dns=False)
        )


async def test_a_different_port_on_an_allowed_host_is_a_distinct_choice() -> None:
    policy = UrlPolicy(allowlist=("ollama.home.arpa:11434",), resolve_dns=False)
    with pytest.raises(UrlNotAllowed):
        await validate_endpoint_url("http://ollama.home.arpa:9999/v1", policy)


@pytest.mark.parametrize("host", ["169.254.169.254", "169.254.1.1", "[fe80::1]"])
async def test_link_local_and_metadata_addresses_are_always_refused(host: str) -> None:
    # Even an explicit allowlist entry does not unlock these.
    bare = host.strip("[]")
    policy = UrlPolicy(allowlist=(bare,), resolve_dns=False)
    with pytest.raises(UrlNotAllowed, match="blocked address"):
        await validate_endpoint_url(f"http://{host}/v1", policy)


async def test_a_malformed_url_is_a_policy_refusal_not_a_crash() -> None:
    """A bare IPv6 literal needs brackets; that must not escape as ValueError."""
    with pytest.raises(UrlNotAllowed, match="malformed"):
        parse_endpoint_url("http://fe80::1/v1")


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://127.0.0.1:11434/v1", "127.0.0.1"),
        ("http://10.0.0.7:11434/v1", "10.0.0.7"),
        ("http://192.168.1.20/v1", "192.168.1.20"),
        ("http://[::1]:11434/v1", "::1"),
    ],
)
async def test_private_lan_addresses_are_allowed_by_default(url: str, host: str) -> None:
    """The whole point: the intended endpoints live on the LAN."""
    parsed = await validate_endpoint_url(url, UrlPolicy(resolve_dns=False))
    assert parsed.host == host


async def test_dns_resolution_feeds_the_same_block_list() -> None:
    parsed = await validate_endpoint_url("http://localhost:11434/v1", UrlPolicy())
    assert parsed.ips, "localhost should resolve to at least one address"


async def test_an_unresolvable_host_fails_before_a_request() -> None:
    with pytest.raises(UrlNotAllowed, match="does not resolve"):
        await validate_endpoint_url("http://definitely-not-a-host.invalid/v1", UrlPolicy())
