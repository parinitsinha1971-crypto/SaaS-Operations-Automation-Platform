"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

log = logging.getLogger("ops.settings")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./ops.db"))
    config_path: str = field(default_factory=lambda: _env("OPS_CONFIG", "config/services.yaml"))
    api_key: str = field(default_factory=lambda: _env("OPS_API_KEY"))
    webhook_token: str = field(default_factory=lambda: _env("OPS_WEBHOOK_TOKEN"))
    protect_reads: bool = field(default_factory=lambda: _bool("OPS_PROTECT_READS", False))
    runtime: str = field(default_factory=lambda: _env("OPS_RUNTIME", "docker"))  # docker | none
    run_agent: bool = field(default_factory=lambda: _bool("OPS_RUN_AGENT", True))
    alertmanager_url: str = field(default_factory=lambda: _env("ALERTMANAGER_URL"))
    public_url: str = field(default_factory=lambda: _env("OPS_PUBLIC_URL", "http://localhost:8080"))
    grafana_url: str = field(default_factory=lambda: _env("GRAFANA_URL", "http://localhost:3000"))
    chaos_token: str = field(default_factory=lambda: _env("CHAOS_TOKEN"))
    report_webhook_url: str = field(default_factory=lambda: _env("REPORT_WEBHOOK_URL"))
    report_time_utc: str = field(default_factory=lambda: _env("OPS_REPORT_TIME_UTC", "00:05"))
    retention_days: int = field(default_factory=lambda: int(_env("OPS_RETENTION_DAYS", "14")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    def effective_api_key(self) -> str:
        return self.api_key or _generated_key()


_GENERATED: dict[str, str] = {}


def _generated_key() -> str:
    """No key configured: mint one per process and print it once, never run open."""
    if "key" not in _GENERATED:
        _GENERATED["key"] = secrets.token_urlsafe(24)
        log.warning("OPS_API_KEY not set; generated a temporary key for this run: %s", _GENERATED["key"])
    return _GENERATED["key"]
