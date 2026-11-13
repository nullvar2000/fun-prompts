"""Operator CLI: endpoint creation, discovery, and the URL policy on the way in."""

from __future__ import annotations

import pytest

from app.cli import main
from app.container import build_container
from app.providers import registry
from tests.fakes import FakeProvider


@pytest.fixture
def data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the CLI at a throwaway database and allow only localhost."""
    monkeypatch.setenv("FP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FP_ENDPOINT_ALLOWLIST", "127.0.0.1:9999")
    monkeypatch.delenv("FP_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    return tmp_path


@pytest.fixture
def fake_provider() -> FakeProvider:
    provider = FakeProvider()
    registry.register("fake", lambda build: provider)
    return provider


def test_add_then_list_shows_the_endpoint_and_its_models(
    data_dir, capsys: pytest.CaptureFixture[str], fake_provider: FakeProvider
) -> None:
    code = main(
        [
            "endpoints",
            "add",
            "lan",
            "http://127.0.0.1:9999/v1",
            "--provider",
            "fake",
            "--model",
            "fake-1",
            "--tag",
            "demo",
        ]
    )
    assert code == 0
    listing = main(["endpoints", "list"])
    out = capsys.readouterr().out
    assert listing == 0
    assert "lan" in out and "http://127.0.0.1:9999/v1" in out and "fake-1" in out


def test_the_api_key_is_never_stored_in_the_clear(data_dir) -> None:
    main(
        [
            "endpoints",
            "add",
            "guarded",
            "http://127.0.0.1:9999/v1",
            "--api-key",
            "sk-secret-value",
        ]
    )
    container = build_container(_settings(data_dir))
    row = container.db.query_one("SELECT api_key_encrypted FROM endpoints WHERE name = 'guarded'")
    assert "sk-secret-value" not in row["api_key_encrypted"]
    assert container.key_ring.decrypt(row["api_key_encrypted"]) == "sk-secret-value"


def test_an_allowlisted_host_is_required(data_dir, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["endpoints", "add", "elsewhere", "http://10.9.9.9:11434/v1"])
    assert code == 2
    assert "not in FP_ENDPOINT_ALLOWLIST" in capsys.readouterr().err
    container = build_container(_settings(data_dir))
    assert container.endpoints.get_by_name("elsewhere") is None


def test_a_metadata_address_is_refused_even_with_a_permissive_policy(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FP_ENDPOINT_ALLOWLIST", raising=False)
    code = main(["endpoints", "add", "evil", "http://169.254.169.254/v1"])
    assert code == 2
    assert "blocked address" in capsys.readouterr().err


def test_discover_stores_what_the_endpoint_offers(
    data_dir, capsys: pytest.CaptureFixture[str], fake_provider: FakeProvider
) -> None:
    main(["endpoints", "add", "lan", "http://127.0.0.1:9999/v1", "--provider", "fake"])
    code = main(["endpoints", "discover", "lan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2 models" in out and "fake-model-1" in out

    container = build_container(_settings(data_dir))
    endpoint = container.endpoints.get_by_name("lan")
    assert {m.model_id for m in container.endpoints.list_models(endpoint.id)} == {  # type: ignore[union-attr]
        "fake-model-1",
        "fake-model-2",
    }


def test_probe_reports_a_failing_endpoint(
    data_dir, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.providers.base import ProviderUnavailable

    failing = FakeProvider(fail_with=ProviderUnavailable("connection refused"))
    registry.register("fake-failing", lambda build: failing)
    main(["endpoints", "add", "down", "http://127.0.0.1:9999/v1", "--provider", "fake-failing"])
    code = main(["endpoints", "probe", "down"])
    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_a_duplicate_endpoint_name_is_a_clean_error(
    data_dir, capsys: pytest.CaptureFixture[str], fake_provider: FakeProvider
) -> None:
    main(["endpoints", "add", "lan", "http://127.0.0.1:9999/v1", "--provider", "fake"])
    capsys.readouterr()
    assert main(["endpoints", "add", "lan", "http://127.0.0.1:9999/v1", "--provider", "fake"]) == 2
    assert "already exists" in capsys.readouterr().err


def test_discovery_reports_an_unreachable_endpoint_without_a_traceback(
    data_dir, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.providers.base import ProviderUnavailable

    registry.register("fake-down", lambda build: FakeProvider(fail_with=ProviderUnavailable("refused")))
    main(["endpoints", "add", "gone", "http://127.0.0.1:9999/v1", "--provider", "fake-down"])
    capsys.readouterr()
    assert main(["endpoints", "discover", "gone"]) == 1
    err = capsys.readouterr().err
    assert "discovery failed" in err and "Traceback" not in err


def test_status_summarises_the_queue_and_the_guard(data_dir, capsys: pytest.CaptureFixture[str]) -> None:
    container = build_container(_settings(data_dir))
    container.guard_log.log(matched_rule="role-spoofing", snippet="system:", action="block")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "guard blocks/flags: 1" in out


def test_unknown_endpoint_name_is_a_usable_error(
    data_dir, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["endpoints", "probe", "nope"]) == 2
    assert "no endpoint named nope" in capsys.readouterr().err


def _settings(data_dir):  # noqa: ANN001, ANN202
    from app.config import Settings

    return Settings(data_dir=data_dir)
