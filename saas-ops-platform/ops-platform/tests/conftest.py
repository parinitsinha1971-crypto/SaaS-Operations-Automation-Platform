"""Shared fixtures: an in-memory fake of the SaaS fleet, Docker and Alertmanager."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.config import PlatformConfig  # noqa: E402
from ops.db import Database  # noqa: E402
from ops.runtime import ContainerInfo, ContainerStats  # noqa: E402


class FakeRuntime:
    """Stands in for Docker: records restarts, serves stats and log lines."""

    name = "fake"

    def __init__(self, fleet: FakeFleet | None = None) -> None:
        self.fleet = fleet
        self.restarts: list[str] = []
        self.kills: list[str] = []
        self.fail_restart = False
        self.stats_by_container: dict[str, ContainerStats] = {}
        self.logs: dict[str, list[str]] = {}
        self.state: dict[str, str] = {}

    async def info(self, container):
        return ContainerInfo(container, self.state.get(container, "running"), None, 0, None, True)

    async def stats(self, container):
        return self.stats_by_container.get(container)

    async def restart(self, container, timeout=10):
        if self.fail_restart:
            raise RuntimeError("docker daemon unreachable")
        self.restarts.append(container)
        self.state[container] = "running"
        if self.fleet:
            self.fleet.on_restart(container)

    async def kill(self, container):
        self.kills.append(container)
        self.state[container] = "exited"

    async def logs_since(self, container, since: datetime):
        lines, self.logs[container] = self.logs.get(container, []), []
        return lines


class FakeFleet:
    """httpx transport that plays the API, the web app and Alertmanager.

    Flip `healthy[...]` or `latency_ms[...]` to change what the monitor sees.
    """

    def __init__(self) -> None:
        self.healthy = {"saas-api": True, "saas-web": True}
        self.restart_fixes = True
        self.alerts: list[dict] = []
        self.chaos_calls: list[tuple[str, str, dict]] = []

    def on_restart(self, container: str) -> None:
        if self.restart_fixes and container in self.healthy:
            self.healthy[container] = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "alertmanager":
            self.alerts.extend(json.loads(request.content))
            return httpx.Response(200, json={})
        if host not in self.healthy:
            return httpx.Response(404)
        if path.startswith("/chaos"):
            body = json.loads(request.content) if request.content else {}
            self.chaos_calls.append((host, path, body))
            return httpx.Response(200, json={"ok": True})
        if not self.healthy[host]:
            raise httpx.ConnectError("connection refused", request=request)
        if path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "resources": {
                        "cpu_percent": 3.0,
                        "memory_rss_bytes": 50_000_000,
                        "disk_used_bytes": 10 * 1024 * 1024,
                        "disk_quota_bytes": 500 * 1024 * 1024,
                    },
                },
            )
        if path == "/api/v1/orders":
            return httpx.Response(200, json={"orders": [], "total": 0})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_config(**overrides) -> PlatformConfig:
    raw = {
        "defaults": {
            "interval_s": 1,
            "timeout_s": 1,
            "failure_threshold": 3,
            "degraded_threshold": 2,
            "recovery_threshold": 2,
        },
        "thresholds": {"sustain_samples": 2},
        "anomaly": {"enabled": False},
        "remediation": {"max_restarts": 2, "window_minutes": 15, "backoff_s": [0, 0], "grace_s": 0},
        "services": [
            {
                "name": "saas-api",
                "display_name": "API",
                "kind": "api",
                "container": "saas-api",
                "checks": [
                    {"type": "http", "name": "health", "url": "http://saas-api/health"},
                    {
                        "type": "api",
                        "name": "orders",
                        "url": "http://saas-api/api/v1/orders",
                        "expect_json_keys": ["orders"],
                    },
                ],
                "resources": {"cpu_mem": "docker", "disk": "health"},
                "logs": {
                    "source": "docker",
                    "rules": [{"name": "fatal", "pattern": "fatal error", "severity": "critical"}],
                },
                "chaos": {"enabled": True, "url": "http://saas-api"},
            },
            {
                "name": "saas-web",
                "display_name": "Web App",
                "kind": "web",
                "container": "saas-web",
                "depends_on": ["saas-api"],
                "checks": [{"type": "http", "name": "health", "url": "http://saas-web/health"}],
                "resources": {"cpu_mem": "docker", "disk": "health"},
                "logs": {"source": "none"},
            },
        ],
    }
    for k, v in overrides.items():
        raw[k] = v if not isinstance(v, dict) else {**raw.get(k, {}), **v}
    return PlatformConfig.model_validate(raw)


def db_url(tmp_path) -> str:
    """SQLite by default; CI sets TEST_DATABASE_URL to run the same tests on PostgreSQL."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        from ops.db import Base

        d = Database(url)
        d.create_all()
        Base.metadata.drop_all(d.engine)
        d.engine.dispose()
        return url
    return f"sqlite:///{tmp_path / 'ops.db'}"


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(db_url(tmp_path))
    d.create_all()
    yield d
    d.engine.dispose()


@pytest.fixture
def fleet() -> FakeFleet:
    return FakeFleet()


@pytest.fixture
def runtime(fleet) -> FakeRuntime:
    return FakeRuntime(fleet)


@pytest.fixture
def config() -> PlatformConfig:
    return make_config()
