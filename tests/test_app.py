"""App skeleton: health, rendered page, static assets, security headers."""

from __future__ import annotations

from dataclasses import replace

from app.config import Settings
from app.web.security import build_csp


def test_healthz_reports_version_and_artifact_origin(client) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["artifact_origin"] == "http://localhost:8081"


def test_home_page_renders_with_vendored_htmx(client) -> None:
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    # No CDN anywhere: htmx is served from our own static mount.
    assert 'src="/static/htmx.min.js"' in html
    assert "unpkg.com" not in html and "cdn." not in html
    assert '<meta name="artifact-origin" content="http://localhost:8081"' in html
    # htmx must not inject an inline <style>: style-src is 'self' and stays that way.
    assert '"includeIndicatorStyles": false' in html


def test_static_assets_are_served(client) -> None:
    for path in ("/static/style.css", "/static/htmx.min.js"):
        res = client.get(path)
        assert res.status_code == 200, path
    assert "text/css" in client.get("/static/style.css").headers["content-type"]


def test_app_origin_headers_lock_down_scripting(client, settings) -> None:
    res = client.get("/")
    csp = res.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert f"frame-src {settings.artifact_base_url}" in csp
    assert "frame-ancestors 'none'" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cross-origin-opener-policy"] == "same-origin"


def test_csp_follows_the_configured_artifact_origin() -> None:
    settings = replace(
        Settings(),
        artifact_base_url="https://apps.example.com",
        extra_frame_ancestors=["https://gallery.example.com"],
    )
    csp = build_csp(settings)
    assert "frame-src https://apps.example.com" in csp
    assert "frame-ancestors 'none' https://gallery.example.com" in csp
