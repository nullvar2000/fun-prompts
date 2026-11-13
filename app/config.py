"""Environment-first configuration.

Every operational value is read from an environment variable with a safe default.
Nothing operational is hardcoded elsewhere: other layers receive a typed ``Settings``
object injected by the composition root (``app.main``) and never read the
environment themselves.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Raised when an environment variable holds an unusable value."""


def _get(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _str(name: str, default: str) -> str:
    return _get(name, default) or default


def _int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (1/0, true/false), got {raw!r}")


def _list(name: str) -> list[str]:
    raw = _get(name)
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Resolved, validated configuration for one process."""

    # --- network ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    artifact_port: int = 8081
    #: Absolute base URL for the artifact origin. Feeds iframe ``src`` values and
    #: the ``frame-ancestors`` CSP directive, so it must match the hostname users
    #: actually reach, not just "localhost".
    artifact_base_url: str = "http://localhost:8081"

    # --- storage ------------------------------------------------------------
    data_dir: Path = Path("/data")
    db_filename: str = "funprompts.db"
    db_idle_timeout_ms: int = 5000

    # --- worker / jobs ------------------------------------------------------
    worker_concurrency: int = 2
    worker_poll_interval_ms: int = 1000
    job_max_attempts: int = 2
    job_timeout_s: int = 120
    job_repair_retries: int = 1

    # --- caps ---------------------------------------------------------------
    max_prompt_chars: int = 8000
    max_artifact_kb: int = 200
    max_output_tokens: int = 8000
    max_comparison_targets: int = 4

    # --- auth / secrets -----------------------------------------------------
    admin_password: str | None = None
    session_secret: str | None = None
    session_ttl_hours: int = 24
    #: Overrides ``<data_dir>/keys/fernet.key`` when set.
    key_file_override: Path | None = None

    # --- provider safety ----------------------------------------------------
    endpoint_allowlist: list[str] = field(default_factory=list)
    block_metadata_ips: bool = True
    allow_insecure_tls: bool = False
    #: Debug escape hatch: when true, the prompt-injection detector lets override
    #: markers through. Never enable on a reachable interface.
    allow_prompt_override_markers: bool = False
    rate_limit_per_hour: int = 0
    extra_frame_ancestors: list[str] = field(default_factory=list)

    # --- boot ---------------------------------------------------------------
    seed_on_start: bool = True
    log_level: str = "INFO"

    # ------------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def key_file(self) -> Path:
        return self.key_file_override or (self.data_dir / "keys" / "fernet.key")

    @property
    def first_run_file(self) -> Path:
        return self.data_dir / "first-run.txt"

    @property
    def max_artifact_bytes(self) -> int:
        return self.max_artifact_kb * 1024

    def redacted_dump(self) -> dict[str, object]:
        """Startup view of the resolved config, secrets never included."""
        raw = {
            "FP_HOST": self.host,
            "FP_PORT": self.port,
            "FP_ARTIFACT_PORT": self.artifact_port,
            "FP_ARTIFACT_BASE_URL": self.artifact_base_url,
            "FP_DATA_DIR": str(self.data_dir),
            "FP_DB_FILENAME": self.db_filename,
            "FP_DB_IDLE_TIMEOUT_MS": self.db_idle_timeout_ms,
            "FP_WORKER_CONCURRENCY": self.worker_concurrency,
            "FP_WORKER_POLL_INTERVAL_MS": self.worker_poll_interval_ms,
            "FP_JOB_MAX_ATTEMPTS": self.job_max_attempts,
            "FP_JOB_TIMEOUT_S": self.job_timeout_s,
            "FP_JOB_REPAIR_RETRIES": self.job_repair_retries,
            "FP_MAX_PROMPT_CHARS": self.max_prompt_chars,
            "FP_MAX_ARTIFACT_KB": self.max_artifact_kb,
            "FP_MAX_OUTPUT_TOKENS": self.max_output_tokens,
            "FP_MAX_COMPARISON_TARGETS": self.max_comparison_targets,
            "FP_SESSION_TTL_HOURS": self.session_ttl_hours,
            "FP_KEY_FILE": str(self.key_file),
            "FP_ENDPOINT_ALLOWLIST": self.endpoint_allowlist or "(any non-blocked host)",
            "FP_BLOCK_METADATA_IPS": self.block_metadata_ips,
            "FP_ALLOW_INSECURE_TLS": self.allow_insecure_tls,
            "FP_ALLOW_PROMPT_MARKERS": self.allow_prompt_override_markers,
            "FP_RATE_LIMIT_PER_HOUR": self.rate_limit_per_hour,
            "FP_ALLOW_ORIGINS": self.extra_frame_ancestors,
            "FP_SEED_ON_START": self.seed_on_start,
            "FP_LOG_LEVEL": self.log_level,
            "FP_ADMIN_PASSWORD": "***" if self.admin_password else "(generated on first boot)",
            "FP_SESSION_SECRET": "***" if self.session_secret else "(generated on first boot)",
        }
        return raw


def load_dotenv_if_enabled(path: str = ".env") -> None:
    """Load a local ``.env`` only when ``FP_DOTENV=1``; container runs stay pure-env."""
    if not _bool("FP_DOTENV", False):
        return
    candidate = Path(path)
    if not candidate.is_file():
        return
    for line in candidate.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    """Read, coerce and validate the environment into a :class:`Settings`."""
    load_dotenv_if_enabled()

    data_dir = Path(_str("FP_DATA_DIR", "/data")).expanduser()
    settings = Settings(
        host=_str("FP_HOST", "0.0.0.0"),
        port=_int("FP_PORT", 8080, minimum=1, maximum=65535),
        artifact_port=_int("FP_ARTIFACT_PORT", 8081, minimum=1, maximum=65535),
        artifact_base_url=_str("FP_ARTIFACT_BASE_URL", "http://localhost:8081").rstrip("/"),
        data_dir=data_dir,
        db_filename=_str("FP_DB_FILENAME", "funprompts.db"),
        db_idle_timeout_ms=_int("FP_DB_IDLE_TIMEOUT_MS", 5000, minimum=0),
        worker_concurrency=_int("FP_WORKER_CONCURRENCY", 2, minimum=1, maximum=16),
        worker_poll_interval_ms=_int("FP_WORKER_POLL_INTERVAL_MS", 1000, minimum=100),
        job_max_attempts=_int("FP_JOB_MAX_ATTEMPTS", 2, minimum=1, maximum=5),
        job_timeout_s=_int("FP_JOB_TIMEOUT_S", 120, minimum=5, maximum=3600),
        job_repair_retries=_int("FP_JOB_REPAIR_RETRIES", 1, minimum=0, maximum=3),
        max_prompt_chars=_int("FP_MAX_PROMPT_CHARS", 8000, minimum=100),
        max_artifact_kb=_int("FP_MAX_ARTIFACT_KB", 200, minimum=1),
        max_output_tokens=_int("FP_MAX_OUTPUT_TOKENS", 8000, minimum=64),
        max_comparison_targets=_int("FP_MAX_COMPARISON_TARGETS", 4, minimum=1, maximum=8),
        admin_password=_get("ADMIN_PASSWORD") or _get("FP_ADMIN_PASSWORD"),
        session_secret=_get("FP_SESSION_SECRET"),
        session_ttl_hours=_int("FP_SESSION_TTL_HOURS", 24, minimum=1, maximum=24 * 30),
        key_file_override=Path(p) if (p := _get("FP_KEY_FILE")) else None,
        endpoint_allowlist=_list("FP_ENDPOINT_ALLOWLIST"),
        block_metadata_ips=_bool("FP_BLOCK_METADATA_IPS", True),
        allow_insecure_tls=_bool("FP_ALLOW_INSECURE_TLS", False),
        allow_prompt_override_markers=_bool("FP_ALLOW_PROMPT_MARKERS", False),
        rate_limit_per_hour=_int("FP_RATE_LIMIT_PER_HOUR", 0, minimum=0),
        extra_frame_ancestors=_list("FP_ALLOW_ORIGINS"),
        seed_on_start=_bool("FP_SEED_ON_START", True),
        log_level=_str("FP_LOG_LEVEL", "INFO").upper(),
    )

    if settings.port == settings.artifact_port:
        raise ConfigError(
            "FP_PORT and FP_ARTIFACT_PORT must differ: the artifact origin only "
            "isolates the gallery when it is served from a different port."
        )
    return settings


def bootstrap_secrets(settings: Settings) -> Settings:
    """Guarantee an admin password and session secret exist.

    Anything not supplied through the environment is generated once and recorded
    in ``<data_dir>/first-run.txt`` (mode 0600) so a fresh container is still
    usable without pre-configuring secrets.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []

    password = settings.admin_password
    if not password:
        password = secrets.token_urlsafe(12)
        generated.append(f"ADMIN_PASSWORD={password}")

    secret = settings.session_secret
    if not secret:
        secret = secrets.token_hex(32)
        generated.append(f"FP_SESSION_SECRET={secret}")

    if generated:
        path = settings.first_run_file
        if not path.exists():
            path.write_text("\n".join(generated) + "\n", encoding="utf-8")
            path.chmod(0o600)

    return replace(settings, admin_password=password, session_secret=secret)
