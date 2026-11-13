"""The artifact gate: what may be served to a browser later."""

from __future__ import annotations

from app.build.validate import repair_instruction, validate_artifact

MAX = 20_000


def doc(body: str, head: str = "") -> str:
    return (
        "<!DOCTYPE html><html><head>"
        f"{head}"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def test_a_clean_self_contained_document_passes() -> None:
    report = validate_artifact(doc("<canvas id=c></canvas><script>void 0</script>"), max_bytes=MAX)
    assert report.ok and report.violations == () and report.size_bytes > 0


def test_empty_output_is_rejected() -> None:
    report = validate_artifact("   ", max_bytes=MAX)
    assert not report.ok and report.violations == ("empty-response",)


def test_external_scripts_and_links_are_rejected() -> None:
    report = validate_artifact(
        doc("<script src='https://cdn.example.com/x.js'></script>", '<link href="https://fonts.example.com/f.css" rel=stylesheet>'),
        max_bytes=MAX,
    )
    assert "external-url" in report.violations
    assert any("cdn.example.com" in url for url in report.external_urls)


def test_protocol_relative_urls_are_rejected() -> None:
    report = validate_artifact(doc('<img src="//cdn.example.com/a.png">'), max_bytes=MAX)
    assert "external-url" in report.violations


def test_svg_namespaces_and_data_uris_are_allowed() -> None:
    report = validate_artifact(
        doc(
            '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'
            '<img src="data:image/png;base64,iVBORw0KGgo=">'
        ),
        max_bytes=MAX,
    )
    assert report.ok and report.external_urls == ()


def test_a_base_tag_could_redirect_the_whole_frame() -> None:
    report = validate_artifact(doc("<p>x</p>", '<base href="https://evil.example/">'), max_bytes=MAX)
    assert "base-tag" in report.violations


def test_meta_refresh_can_escape_the_frame() -> None:
    report = validate_artifact(
        doc("<p>x</p>", '<meta http-equiv="refresh" content="0; url=/admin/">'), max_bytes=MAX
    )
    assert "meta-refresh" in report.violations


def test_structure_requirements_are_enforced() -> None:
    assert "missing-doctype" in validate_artifact("<html><head></head><body></body></html>", max_bytes=MAX).violations
    assert "missing-head" in validate_artifact("<!DOCTYPE html><html><body></body></html>", max_bytes=MAX).violations
    assert "missing-body" in validate_artifact("<!DOCTYPE html><html><head></head></html>", max_bytes=MAX).violations
    assert (
        "missing-or-duplicate-html-close"
        in validate_artifact("<!DOCTYPE html><html><head></head><body></body>", max_bytes=MAX).violations
    )


def test_two_documents_in_one_response_are_rejected() -> None:
    report = validate_artifact(doc("<p>one</p>") + doc("<p>two</p>"), max_bytes=MAX)
    assert "multiple-documents" in report.violations


def test_size_cap_is_a_violation() -> None:
    report = validate_artifact(doc("<p>" + "a" * 500 + "</p>"), max_bytes=200)
    assert any(v.startswith("oversized") for v in report.violations)


def test_dangerous_but_not_forbidden_code_is_a_warning_only() -> None:
    report = validate_artifact(doc("<script>window.location = '/x'</script>"), max_bytes=MAX)
    assert report.ok and "script-sets-location" in report.warnings


def test_unbalanced_script_tags_warn() -> None:
    report = validate_artifact(doc("<script>a</script><script>b"), max_bytes=MAX)
    assert report.ok and "unbalanced-script-tags" in report.warnings
    # A matched pair does not warn.
    assert "unbalanced-script-tags" not in validate_artifact(
        doc("<script>a</script>"), max_bytes=MAX
    ).warnings


def test_the_repair_instruction_names_the_problems() -> None:
    report = validate_artifact(doc("<script src='https://x.example/a.js'></script>"), max_bytes=MAX)
    instruction = repair_instruction(report)
    assert "external-url" in instruction
    assert "ONLY the complete" in instruction


def test_report_serialises_for_storage() -> None:
    report = validate_artifact(doc("<p>ok</p>"), max_bytes=MAX)
    as_dict = report.as_dict()
    assert as_dict["ok"] is True and isinstance(as_dict["violations"], list)
