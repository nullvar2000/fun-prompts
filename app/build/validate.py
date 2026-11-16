"""The artifact gate.

Nothing is published unless it passes here, because a published artifact is
served to browsers later on. The checks are about containment, not aesthetics:

* one complete HTML document, within the size cap
* no URL that a browser would fetch (namespaces excepted — they are identifiers,
  not requests)
* no ``<base>`` and no meta refresh, both of which could point the sandboxed
  frame somewhere else

A rejection keeps the raw response for inspection; it never silently publishes
a mangled document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Namespace declarations look like URLs but are never fetched. Exempting them
# keeps inline SVG working, which is one of the few legitimate uses of a URL in
# a self-contained page.
_ALLOWED_URL_LITERALS = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/1999/xhtml",
)

_URL_PATTERNS = (
    re.compile(r"""https?://[^"'()<>\s`]+""", re.IGNORECASE),
    re.compile(r"""(?<![:/])//[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}[^"'()<>\s`]*"""),
)
_DOCTYPE = re.compile(r"<!doctype\s+html\s*>?", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HEAD = re.compile(r"<head[\s>]", re.IGNORECASE)
_BODY = re.compile(r"<body[\s>]", re.IGNORECASE)
_BASE_TAG = re.compile(r"<base[\s>]", re.IGNORECASE)
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*>""", re.IGNORECASE
)
_EVENT_HANDLER_URL = re.compile(r"""\b(?:window\.location|location\.href|location\.replace)\s*=""", re.IGNORECASE)
_UNCLOSED_SCRIPT = re.compile(r"<script(?![^>]*src=)[^>]*>", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    size_bytes: int = 0
    external_urls: tuple[str, ...] = ()
    notes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "warnings": list(self.warnings),
            "size_bytes": self.size_bytes,
            "external_urls": list(self.external_urls),
            "notes": self.notes,
        }


def validate_artifact(html: str, *, max_bytes: int) -> ValidationReport:
    violations: list[str] = []
    warnings: list[str] = []
    body = (html or "").strip()
    size = len(body.encode("utf-8"))

    if not body:
        return ValidationReport(ok=False, violations=("empty-response",), size_bytes=0)

    if size > max_bytes:
        violations.append(f"oversized:{size}>{max_bytes}")

    if not _DOCTYPE.match(body):
        violations.append("missing-doctype")
    if len(_DOCTYPE.findall(body)) > 1:
        violations.append("multiple-documents")
    if not _HTML_OPEN.search(body):
        violations.append("missing-html-element")
    if not _HEAD.search(body):
        violations.append("missing-head")
    if not _BODY.search(body):
        violations.append("missing-body")
    if body.lower().count("</html>") != 1:
        violations.append("missing-or-duplicate-html-close")

    found_urls = _external_urls(body)
    if found_urls:
        violations.append("external-url")

    if _BASE_TAG.search(body):
        violations.append("base-tag")
    if _META_REFRESH.search(body):
        violations.append("meta-refresh")
    if _EVENT_HANDLER_URL.search(body):
        warnings.append("script-sets-location")

    open_scripts = len(_UNCLOSED_SCRIPT.findall(body))
    if body.lower().count("</script>") != open_scripts:
        warnings.append("unbalanced-script-tags")

    return ValidationReport(
        ok=not violations,
        violations=tuple(violations),
        warnings=tuple(warnings),
        size_bytes=size,
        external_urls=found_urls,
    )


def repair_instruction(report: ValidationReport, *, truncated_at: int | None = None) -> str:
    """The follow-up used for the single automatic repair retry.

    A truncated answer gets different advice: asking for the same complete
    document again, at the same ceiling, just reproduces the same cut-off. The
    retry has to be told to spend fewer tokens than the last answer did.
    """
    problems = ", ".join(report.violations) or "unknown problem"
    if truncated_at:
        return (
            "Your previous answer was cut off at the "
            f"{truncated_at}-token output ceiling, so the document stopped mid-build "
            f"(and what had arrived was wrong as well: {problems}). Reply again with "
            "ONLY the complete, self-contained HTML document: start at <!DOCTYPE html>, "
            f"end at </html>, and stay comfortably under {truncated_at} tokens — "
            "leaner CSS, shorter names, and none of the decoration you cannot afford. "
            "No markdown fences, no commentary, no external URLs."
        )
    return (
        "Your previous answer could not be published "
        f"(reasons: {problems}). Reply again with ONLY the complete, "
        "self-contained HTML document: start at <!DOCTYPE html>, end at </html>, "
        "no markdown fences, no commentary, and no external URLs."
    )


def _external_urls(body: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in _URL_PATTERNS:
        for match in pattern.finditer(body):
            candidate = match.group(0)
            if any(literal in candidate for literal in _ALLOWED_URL_LITERALS):
                continue
            if candidate not in found:
                found.append(candidate)
    return tuple(found[:5])
