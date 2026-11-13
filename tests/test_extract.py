"""Extracting one HTML document from messy model output."""

from __future__ import annotations

from app.build.extract import extract_html

DOC = "<!DOCTYPE html><html><head><title>t</title></head><body>hi</body></html>"


def test_a_clean_document_passes_through() -> None:
    extracted = extract_html(DOC)
    assert extracted.html == DOC
    assert extracted.source == "as-is"


def test_leading_whitespace_and_bom_are_ignored() -> None:
    assert extract_html("\ufeff  \n" + DOC).html == DOC


def test_prose_before_and_after_is_dropped() -> None:
    extracted = extract_html(f"Sure! Here you go:\n\n{DOC}\n\nHope that helps!")
    assert extracted.html == DOC
    assert extracted.source == "prose-trimmed"


def test_fenced_document_is_unwrapped() -> None:
    extracted = extract_html(f"Here it is:\n```html\n{DOC}\n```\nDone.")
    assert extracted.html == DOC
    assert extracted.source == "fence"


def test_unclosed_fence_is_recovered() -> None:
    extracted = extract_html(f"```html\n{DOC}")
    assert extracted.html == DOC
    assert extracted.source == "unclosed-fence"


def test_tilde_fences_work_too() -> None:
    assert extract_html(f"~~~\n{DOC}\n~~~").html == DOC


def test_the_largest_html_fence_wins() -> None:
    text = f"snippet:\n```html\n<!DOCTYPE html><html><body>short</body></html>\n```\nfull:\n```html\n{DOC}\n```"
    assert extract_html(text).html == DOC


def test_a_non_html_answer_yields_nothing() -> None:
    extracted = extract_html("I cannot build that for you.")
    assert extracted.html == "" and extracted.source == "no-document"


def test_empty_input_is_reported_as_empty() -> None:
    assert extract_html("").source == "empty"
    assert extract_html("   \n").source == "empty"


def test_commentary_between_fences_and_document_is_removed() -> None:
    extracted = extract_html(f"```html\n\n{DOC}\n\n```")
    assert extracted.html == DOC


def test_a_document_that_only_starts_at_html_is_still_found() -> None:
    extracted = extract_html("prefix <html><body>x</body></html>")
    assert extracted.html == "<html><body>x</body></html>"
