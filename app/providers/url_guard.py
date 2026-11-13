"""Endpoint URL policy.

An admin can point the app at an arbitrary base URL, which is a server-side
request forgery risk: the app runs inside the network, so a URL can reach
things the outside world cannot.

The naive rule — refuse every private address — would break the actual use
case, since the intended endpoints are Ollama/vLLM boxes on the LAN. So the
policy is an explicit host allowlist plus a small set of always-refused
targets. Redirects are never followed: a legitimate endpoint that answers
from a different host is a re-configuration decision, not something to obey.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# Always refused, whatever the allowlist says: link-local and cloud metadata
# endpoints are the classic targets for a request forged from inside a network.
ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)

ALLOWED_SCHEMES = ("http", "https")


class UrlNotAllowed(RuntimeError):
    """The URL was rejected before any request was made."""


@dataclass(frozen=True)
class UrlPolicy:
    #: ``host`` or ``host:port`` entries. Empty means "any host that is not
    #: explicitly blocked", which is the friendlier default for a LAN tool.
    allowlist: tuple[str, ...] = ()
    block_metadata: bool = True
    resolve_dns: bool = True


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str
    port: int | None
    path: str
    ips: tuple[str, ...] = field(default_factory=tuple)

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


def parse_endpoint_url(url: str) -> ParsedUrl:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        # e.g. a bare IPv6 literal with no brackets: refuse it as policy rather
        # than letting a parsing error escape as an unhandled exception.
        raise UrlNotAllowed(f"endpoint URL is malformed: {exc}") from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"scheme {parts.scheme or '(none)'} is not allowed; use http or https")
    if not parts.hostname:
        raise UrlNotAllowed("endpoint URL must include a host")
    if parts.username or parts.password:
        raise UrlNotAllowed("endpoint URL must not embed credentials")
    return ParsedUrl(
        scheme=parts.scheme,
        host=parts.hostname,
        port=port,
        path=parts.path or "/",
    )


def _matches_allowlist(parsed: ParsedUrl, allowlist: tuple[str, ...]) -> bool:
    wanted = {entry.strip().lower() for entry in allowlist if entry.strip()}
    return parsed.host.lower() in wanted or parsed.host_port.lower() in wanted


def _blocked_ip(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable is treated as unsafe
    return any(address in network for network in ALWAYS_BLOCKED_NETWORKS)


def _resolve(host: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UrlNotAllowed(f"{host} does not resolve: {exc}") from exc
    return tuple(sorted({info[4][0] for info in infos}))


async def validate_endpoint_url(url: str, policy: UrlPolicy) -> ParsedUrl:
    """Check a base URL against policy *before* anything is fetched."""
    parsed = parse_endpoint_url(url)

    if policy.allowlist and not _matches_allowlist(parsed, policy.allowlist):
        raise UrlNotAllowed(
            f"{parsed.host_port} is not in FP_ENDPOINT_ALLOWLIST "
            f"({', '.join(sorted(policy.allowlist))})"
        )

    literal_ip = _as_ip(parsed.host)
    if policy.block_metadata:
        if literal_ip is not None and _blocked_ip(literal_ip):
            raise UrlNotAllowed(f"{parsed.host} is a blocked address (link-local/metadata)")

    if policy.resolve_dns and literal_ip is None:
        ips = await asyncio.to_thread(_resolve, parsed.host)
        if policy.block_metadata:
            hit = next((ip for ip in ips if _blocked_ip(ip)), None)
            if hit:
                raise UrlNotAllowed(
                    f"{parsed.host} resolves to the blocked address {hit}"
                )
        return ParsedUrl(
            scheme=parsed.scheme, host=parsed.host, port=parsed.port, path=parsed.path, ips=ips
        )

    return parsed


def _as_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None
