"""Monitored-service configuration (config/services.yaml).

Everything the agent knows about a service lives here: its checks,
where to read resources and logs, its SLO, and how it may be remediated.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Defaults(BaseModel):
    interval_s: float = 10
    timeout_s: float = 3
    failure_threshold: int = 3  # consecutive failing cycles before DOWN
    degraded_threshold: int = 2  # consecutive degraded cycles before DEGRADED
    recovery_threshold: int = 2  # consecutive healthy cycles before HEALTHY


class Level(BaseModel):
    warn: float
    crit: float

    @model_validator(mode="after")
    def _order(self):
        if self.crit < self.warn:
            raise ValueError("crit must be >= warn")
        return self


class Thresholds(BaseModel):
    cpu_percent: Level = Level(warn=80, crit=95)
    memory_percent: Level = Level(warn=80, crit=92)
    disk_percent: Level = Level(warn=80, crit=90)
    sustain_samples: int = 3  # a resource must breach this many cycles in a row
    log_errors_per_min: Level = Level(warn=10, crit=60)
    api_error_ratio: Level = Level(warn=0.05, crit=0.25)


class AnomalyCfg(BaseModel):
    enabled: bool = True
    z_threshold: float = 3.5
    warmup_samples: int = 30
    alpha: float = 0.05  # EWMA smoothing; lower = longer memory
    sustain_samples: int = 2
    # Ignore "anomalies" too small to matter in absolute terms.
    min_values: dict[str, float] = Field(
        default_factory=lambda: {"health_latency_ms": 50, "api_p95_ms": 150, "api_error_ratio": 0.02, "log_errors": 5}
    )


class RemediationCfg(BaseModel):
    enabled: bool = True
    max_restarts: int = 3
    window_minutes: int = 15
    backoff_s: list[float] = Field(default_factory=lambda: [0, 30, 120])
    grace_s: float = 20  # ignore failures this long after a restart while the service boots
    restart_on: list[Literal["down", "memory_critical", "exited"]] = Field(
        default_factory=lambda: ["down", "memory_critical", "exited"]
    )


class _CheckBase(BaseModel):
    name: str
    critical: bool = True  # failing a critical check means DOWN; others mean DEGRADED
    timeout_s: float | None = None


class HttpCheck(_CheckBase):
    type: Literal["http"] = "http"
    url: str
    method: str = "GET"
    expect_status: list[int] = Field(default_factory=lambda: [200])

    @field_validator("expect_status", mode="before")
    @classmethod
    def _listify(cls, v):
        return [v] if isinstance(v, int) else v


class ApiCheck(HttpCheck):
    """An HTTP call against a real business endpoint, with content and latency assertions."""

    type: Literal["api"] = "api"  # type: ignore[assignment]
    critical: bool = False
    json_body: dict | None = None
    expect_json_keys: list[str] = Field(default_factory=list)
    max_latency_ms: float | None = None


class TcpCheck(_CheckBase):
    type: Literal["tcp"] = "tcp"
    host: str
    port: int


class PostgresCheck(_CheckBase):
    type: Literal["postgres"] = "postgres"
    dsn_env: str = "SAAS_DB_DSN"
    query: str = "SELECT 1"


Check = Annotated[HttpCheck | ApiCheck | TcpCheck | PostgresCheck, Field(discriminator="type")]


class ResourcesCfg(BaseModel):
    cpu_mem: Literal["docker", "health", "none"] = "docker"
    disk: Literal["health", "postgres", "none"] = "health"
    disk_quota_mb: float | None = None  # required for postgres disk


class LogRule(BaseModel):
    name: str
    pattern: str
    severity: Literal["warning", "critical"] = "warning"
    min_count: int = 1  # matches per cycle needed to raise

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v):
        re.compile(v)
        return v


class LogsCfg(BaseModel):
    source: Literal["docker", "file", "none"] = "docker"
    path: str | None = None
    rules: list[LogRule] = Field(default_factory=list)


class SloCfg(BaseModel):
    availability: float = 99.5  # percent
    latency_ms: float | None = None


class ChaosCfg(BaseModel):
    enabled: bool = True
    url: str | None = None  # base url of the service's /chaos endpoints


class ServiceCfg(BaseModel):
    name: str
    display_name: str | None = None
    kind: Literal["api", "web", "database", "worker", "other"] = "other"
    container: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    checks: list[Check]
    resources: ResourcesCfg = ResourcesCfg()
    logs: LogsCfg = LogsCfg()
    slo: SloCfg = SloCfg()
    remediation: RemediationCfg | None = None  # falls back to global
    chaos: ChaosCfg = ChaosCfg(enabled=False)

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @field_validator("name")
    @classmethod
    def _slug(cls, v):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError("service name must be lowercase letters, digits and dashes")
        return v


class PlatformConfig(BaseModel):
    defaults: Defaults = Defaults()
    thresholds: Thresholds = Thresholds()
    anomaly: AnomalyCfg = AnomalyCfg()
    remediation: RemediationCfg = RemediationCfg()
    host_disk_paths: list[str] = Field(default_factory=lambda: ["/"])
    services: list[ServiceCfg]

    @model_validator(mode="after")
    def _validate(self):
        names = [s.name for s in self.services]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate service names: {sorted(dupes)}")
        for svc in self.services:
            for dep in svc.depends_on:
                if dep not in names:
                    raise ValueError(f"{svc.name} depends on unknown service {dep}")
            if svc.resources.disk == "postgres" and not svc.resources.disk_quota_mb:
                raise ValueError(f"{svc.name}: disk=postgres needs disk_quota_mb")
        return self

    def service(self, name: str) -> ServiceCfg | None:
        return next((s for s in self.services if s.name == name), None)

    def remediation_for(self, svc: ServiceCfg) -> RemediationCfg:
        return svc.remediation or self.remediation


_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
    """Support ${VAR} and ${VAR:-default} in the YAML."""
    return _ENV_RE.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), text)


def load_config(path: str | Path) -> PlatformConfig:
    raw = yaml.safe_load(_expand_env(Path(path).read_text()))
    return PlatformConfig.model_validate(raw)
