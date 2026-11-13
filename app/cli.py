"""Operator CLI.

Endpoints are created here rather than through an unauthenticated HTTP route:
until the admin panel exists (M5) there is no authenticated write surface, and
a temporary unauthenticated one is the kind of thing that survives an
implementation by accident.

    python -m app.cli endpoints add lan http://127.0.0.1:8123/v1 --model fake-1
    python -m app.cli endpoints discover lan
    python -m app.cli status
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.config import load_settings
from app.container import build_container
from app.domain.errors import ValidationError
from app.providers import registry
from app.providers.registry import models_to_rows, policy_for
from app.providers.url_guard import UrlNotAllowed, validate_endpoint_url


def _endpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("base_url")
    parser.add_argument("--model", action="append", default=[], help="seed a model row (repeatable)")
    parser.add_argument("--tag", action="append", default=[], help="tag inherited by builds (repeatable)")
    parser.add_argument("--api-key", default=None, help="omit to be prompted, or set FP_API_KEY")
    parser.add_argument(
        "--provider",
        default="openai_compat",
        help=f"provider type (known: {', '.join(registry.known_types())})",
    )
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=None)


def cmd_endpoints_add(args: argparse.Namespace) -> int:
    container = build_container(load_settings())
    key = args.api_key if args.api_key is not None else os.environ.get("FP_API_KEY")
    if key is None and sys.stdin.isatty():
        key = getpass.getpass("API key (empty for none): ")
    key = key or ""

    try:
        asyncio.run(validate_endpoint_url(args.base_url, policy_for(container.settings)))
    except UrlNotAllowed as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    try:
        endpoint = container.endpoints.create(
            name=args.name,
            base_url=args.base_url.rstrip("/"),
            provider_type=args.provider,
            api_key_encrypted=container.key_ring.encrypt(key) if key else "",
            tags=tuple(args.tag),
            timeout_s=args.timeout,
            max_output_tokens=args.max_tokens,
            temperature=args.temperature,
        )
    except ValidationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.model:
        added, _ = container.endpoints.replace_models(
            endpoint.id, tuple((m, None) for m in args.model)
        )
        print(f"added {added} model row(s)")
    print(f"endpoint {endpoint.id}: {endpoint.name} -> {endpoint.base_url}")
    return 0


def cmd_endpoints_list(args: argparse.Namespace) -> int:
    container = build_container(load_settings())
    endpoints = container.endpoints.list()
    if not endpoints:
        print("no endpoints configured")
        return 0
    for endpoint in endpoints:
        models = container.endpoints.list_models(endpoint.id)
        key_state = "key set" if endpoint.api_key_encrypted else "no key"
        print(
            f"{endpoint.id:>3}  {'on ' if endpoint.enabled else 'off'}  "
            f"{endpoint.name:<18} {endpoint.base_url:<40} {key_state}"
        )
        for model in models:
            print(f"       model {model.id:>3}  {model.model_id:<28} {'enabled' if model.enabled else 'disabled'}")
    return 0


async def _use_provider(provider, action):  # noqa: ANN001
    """One event loop for the call *and* the teardown.

    Two separate asyncio.run() calls would close the loop before the transport
    finished shutting down, which raises "Event loop is closed" during cleanup.
    """
    try:
        return await action(provider)
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()


def cmd_endpoints_discover(args: argparse.Namespace) -> int:
    container = build_container(load_settings())
    endpoint = container.endpoints.get_by_name(args.name)
    if endpoint is None:
        print(f"no endpoint named {args.name}", file=sys.stderr)
        return 2
    provider = container.provider_for(endpoint)
    try:
        models = asyncio.run(_use_provider(provider, lambda p: p.list_models()))
    except Exception as exc:
        print(f"{endpoint.name}: discovery failed — {exc}", file=sys.stderr)
        return 1
    added, removed = container.endpoints.replace_models(endpoint.id, models_to_rows(models))
    print(f"{endpoint.name}: {len(models)} models ({added} added, {removed} removed)")
    for model in models:
        print(f"  {model.model_id:<32} {model.label}")
    return 0


def cmd_endpoints_probe(args: argparse.Namespace) -> int:
    container = build_container(load_settings())
    endpoint = container.endpoints.get_by_name(args.name)
    if endpoint is None:
        print(f"no endpoint named {args.name}", file=sys.stderr)
        return 2
    provider = container.provider_for(endpoint)
    result = asyncio.run(_use_provider(provider, lambda p: p.probe()))
    print(f"{endpoint.name}: {'ok' if result.ok else 'FAILED'} — {result.detail}")
    return 0 if result.ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    container = build_container(load_settings())
    jobs = container.jobs.recent(10)
    counts: dict[str, int] = {}
    for job in container.jobs.recent(500):
        counts[str(job.state)] = counts.get(str(job.state), 0) + 1
    print(f"db: {container.db.path}")
    print(f"pending/running/cancelling: {container.jobs.pending_count()}")
    print(f"recent job states: {counts or 'none'}")
    print(f"guard blocks/flags: {container.guard_log.count()}")
    for job in jobs[:5]:
        print(f"  job {job.id} {job.kind} {job.state} attempts={job.attempts} {job.error[:60]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="fun-prompts operator tools")
    sub = parser.add_subparsers(dest="command", required=True)

    endpoints = sub.add_parser("endpoints", help="manage LLM endpoints")
    endpoints_sub = endpoints.add_subparsers(dest="action", required=True)

    add = endpoints_sub.add_parser("add", help="create an endpoint")
    _endpoint_args(add)
    add.set_defaults(func=cmd_endpoints_add)

    listing = endpoints_sub.add_parser("list", help="list endpoints and models")
    listing.set_defaults(func=cmd_endpoints_list)

    discover = endpoints_sub.add_parser("discover", help="fetch and store the model list")
    discover.add_argument("name")
    discover.set_defaults(func=cmd_endpoints_discover)

    probe = endpoints_sub.add_parser("probe", help="check reachability")
    probe.add_argument("name")
    probe.set_defaults(func=cmd_endpoints_probe)

    status = sub.add_parser("status", help="queue and guard summary")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
