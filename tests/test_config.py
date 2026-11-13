"""Configuration is env-first: defaults, overrides, validation and secret bootstrap."""

from __future__ import annotations

import stat
from dataclasses import replace

import pytest

from app.config import ConfigError, bootstrap_secrets, load_settings


def test_ship_defaults() -> None:
    settings = load_settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8080
    assert settings.artifact_port == 8081
    assert settings.artifact_base_url == "http://localhost:8081"
    assert settings.worker_concurrency == 2
    assert settings.job_repair_retries == 1
    assert settings.job_timeout_s == 120
    assert settings.max_prompt_chars == 8000
    assert settings.max_artifact_kb == 200
    assert settings.max_artifact_bytes == 200 * 1024
    assert settings.max_comparison_targets == 4
    assert settings.seed_on_start is True


def test_env_overrides_every_layer_of_the_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FP_PORT", "9000")
    monkeypatch.setenv("FP_ARTIFACT_PORT", "9001")
    monkeypatch.setenv("FP_ARTIFACT_BASE_URL", "https://apps.example.com/")
    monkeypatch.setenv("FP_DATA_DIR", "/srv/funprompts")
    monkeypatch.setenv("FP_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("FP_JOB_REPAIR_RETRIES", "0")
    monkeypatch.setenv("FP_MAX_ARTIFACT_KB", "64")
    monkeypatch.setenv("FP_BLOCK_METADATA_IPS", "false")
    monkeypatch.setenv("FP_ALLOW_PROMPT_MARKERS", "1")
    monkeypatch.setenv("FP_ENDPOINT_ALLOWLIST", "ollama.home.arpa, 10.0.0.7:11434 ,")
    monkeypatch.setenv("FP_ALLOW_ORIGINS", "https://gallery.example.com")
    monkeypatch.setenv("FP_LOG_LEVEL", "debug")

    settings = load_settings()
    assert settings.port == 9000
    assert settings.artifact_port == 9001
    # Trailing slash is stripped so CSP/iframe URLs never double up.
    assert settings.artifact_base_url == "https://apps.example.com"
    assert settings.data_dir.as_posix() == "/srv/funprompts"
    assert settings.db_path.name == "funprompts.db"
    assert settings.worker_concurrency == 4
    assert settings.job_repair_retries == 0
    assert settings.max_artifact_bytes == 64 * 1024
    assert settings.block_metadata_ips is False
    assert settings.allow_prompt_override_markers is True
    assert settings.endpoint_allowlist == ["ollama.home.arpa", "10.0.0.7:11434"]
    assert settings.extra_frame_ancestors == ["https://gallery.example.com"]
    assert settings.log_level == "DEBUG"


def test_key_file_follows_data_dir_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_settings().key_file.as_posix().endswith("/keys/fernet.key")
    monkeypatch.setenv("FP_KEY_FILE", "/vault/fp.key")
    assert load_settings().key_file.as_posix() == "/vault/fp.key"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FP_PORT", "not-a-port"),
        ("FP_WORKER_CONCURRENCY", "0"),
        ("FP_WORKER_CONCURRENCY", "99"),
        ("FP_MAX_PROMPT_CHARS", "-5"),
    ],
)
def test_bad_numbers_are_rejected_at_boot(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_bad_boolean_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_SEED_ON_START", "maybe")
    with pytest.raises(ConfigError):
        load_settings()


def test_app_and_artifact_ports_must_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_ARTIFACT_PORT", "8080")
    with pytest.raises(ConfigError, match="must differ"):
        load_settings()


def test_origin_urls_must_be_absolute_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare host:port here would land inside a CSP directive and do nothing."""
    monkeypatch.setenv("FP_GALLERY_BASE_URL", "gallery.lan:18080")
    with pytest.raises(ConfigError, match="FP_GALLERY_BASE_URL"):
        load_settings()

    monkeypatch.setenv("FP_ARTIFACT_BASE_URL", "localhost:8081")
    with pytest.raises(ConfigError, match="FP_ARTIFACT_BASE_URL"):
        load_settings()


def test_secrets_are_generated_once_and_recorded(tmp_path) -> None:
    settings = replace(load_settings(), data_dir=tmp_path)
    resolved = bootstrap_secrets(settings)

    assert resolved.admin_password and resolved.session_secret
    path = tmp_path / "first-run.txt"
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    assert resolved.admin_password in body
    assert resolved.session_secret in body
    # World must not be able to read the bootstrap credentials.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # A second boot reuses the resolved values instead of rotating them.
    again = bootstrap_secrets(resolved)
    assert again.admin_password == resolved.admin_password
    assert again.session_secret == resolved.session_secret
    assert path.read_text(encoding="utf-8") == body


def test_supplied_secrets_are_left_alone(tmp_path) -> None:
    settings = replace(load_settings(), data_dir=tmp_path, admin_password="hunter2")
    resolved = bootstrap_secrets(settings)
    assert resolved.admin_password == "hunter2"
    assert "hunter2" not in (tmp_path / "first-run.txt").read_text(encoding="utf-8")


def test_startup_dump_never_leaks_secrets(tmp_path) -> None:
    settings = replace(
        load_settings(),
        data_dir=tmp_path,
        admin_password="hunter2",
        session_secret="sup3rsecret",
    )
    dump = settings.redacted_dump()
    assert dump["FP_ADMIN_PASSWORD"] == "***"
    assert dump["FP_SESSION_SECRET"] == "***"
    assert "hunter2" not in repr(dump)
    assert "sup3rsecret" not in repr(dump)
