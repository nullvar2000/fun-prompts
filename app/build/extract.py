"""Recover the HTML document from whatever the model actually returned.

Even with "output only HTML" in the system prompt, real servers wrap the answer
in prose, add code fences, forget to close a fence, or trail a comment after
``</html>``. Extraction is deliberately forgiving about *wrapping* and strict
about *content* — the validator decides whether what is left is acceptable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FENCE = re.compile(r"(?P<marker>```|~~~)[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n(?P<body>.*?)(?P=marker)", re.DOTALL)
_OPEN_FENCE = re.compile(r"```[ \t]*(?:html|xml|)?[ \t]*\n(?P<body>.*)$", re.DOTALL | re.IGNORECASE)
_DOCTYPE = re.compile(r"<!doctype html", re.IGNORECASE)
_HTML_START = re.compile(r"<html[\s>]", re.IGNORECASE)
_HTML_END = re.compile(r"</html\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class Extracted:
    html: str
    #: How the document was recovered — recorded in the build's validation
    #: report so a noisy endpoint is visible rather than silently absorbed.
    source: str


def extract_html(text: str) -> Extracted:
    if not text or not text.strip():
        return Extracted(html="", source="empty")

    cleaned = text.lstrip("\ufeff \t\r\n")

    if _DOCTYPE.match(cleaned) or _HTML_START.match(cleaned):
        return Extracted(html=_trim_to_document(cleaned), source="as-is")

    fenced = _best_fenced_candidate(cleaned)
    if fenced is not None:
        html, source = fenced
        return Extracted(html=_trim_to_document(html), source=source)

    # No fence, but a document is in there somewhere behind a preamble.
    match = _DOCTYPE.search(cleaned) or _HTML_START.search(cleaned)
    if match:
        return Extracted(html=_trim_to_document(cleaned[match.start() :]), source="prose-trimmed")

    return Extracted(html="", source="no-document")


def _best_fenced_candidate(text: str) -> tuple[str, str] | None:
    candidates = [
        (match.group("body"), "fence")
        for match in _FENCE.finditer(text)
        if _looks_like_html(match.group("body"))
    ]
    if candidates:
        # Largest wins: an endpoint that shows a snippet before the full file
        # should resolve to the full file.
        return max(candidates, key=lambda pair: len(pair[0]))

    open_only = _OPEN_FENCE.search(text)
    if open_only and _looks_like_html(open_only.group("body")):
        return open_only.group("body"), "unclosed-fence"
    return None


def _looks_like_html(body: str) -> bool:
    stripped = body.strip()
    return bool(_DOCTYPE.search(stripped[:200]) or _HTML_START.search(stripped[:200]))


def _trim_to_document(html: str) -> str:
    """Drop a preamble before the doctype and commentary after ``</html>``."""
    html = html.strip()
    start = _DOCTYPE.search(html) or _HTML_START.search(html)
    if start:
        html = html[start.start() :]
    end = _HTML_END.search(html)
    if end:
        html = html[: end.end()]
    return html.strip()
