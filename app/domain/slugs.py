"""Slug rules: pure text transforms owned by the domain."""

from __future__ import annotations

import re

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "prompt") -> str:
    slug = _NON_SLUG.sub("-", text.lower()).strip("-")
    return slug or fallback


def unique_slug(base: str, taken: set[str]) -> str:
    """``dice-roller`` -> ``dice-roller-2`` -> ``dice-roller-3`` …"""
    if base not in taken:
        return base
    counter = 2
    while f"{base}-{counter}" in taken:
        counter += 1
    return f"{base}-{counter}"
