"""Container runtime adapters.

DockerRuntime talks to the Docker Engine API (directly, or through a
socket proxy via DOCKER_HOST). It refuses to restart or kill any
container that is not labelled `ops.managed=true`, so a bad config
can never take down the platform's own database or Grafana.

NoopRuntime is used when no container runtime is available (local dev):
monitoring still works, remediation is recorded as failed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

log = logging.getLogger("ops.runtime")

MANAGED_LABEL = "ops.managed"


class RuntimeUnavailable(RuntimeError):
    pass


class NotManaged(PermissionError):
    pass


@dataclass
class ContainerInfo:
    name: str
    state: str  # running | exited | restarting | paused | dead | created | missing
    health: str | None  # healthy | unhealthy | starting | None
    restart_count: int
    started_at: str | None
    managed: bool


@dataclass
class ContainerStats:
    cpu_percent: float
    memory_bytes: float
    memory_limit_bytes: float

    @property
    def memory_percent(self) -> float:
        return 100.0 * self.memory_bytes / self.memory_limit_bytes if self.memory_limit_bytes else 0.0


class Runtime(Protocol):
    name: str

    async def info(self, container: str) -> ContainerInfo | None: ...
    async def stats(self, container: str) -> ContainerStats | None: ...
    async def restart(self, container: str, timeout: int = 10) -> None: ...
    async def kill(self, container: str) -> None: ...
    async def logs_since(self, container: str, since: datetime) -> list[str]: ...


def cpu_percent_from_stats(s: dict) -> float:
    """Same formula `docker stats` uses."""
    cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    if cpu_delta <= 0 or sys_delta <= 0:
        return 0.0
    return round(cpu_delta / sys_delta * ncpu * 100.0, 2)


def memory_from_stats(s: dict) -> tuple[float, float]:
    mem = s.get("memory_stats", {})
    usage = mem.get("usage", 0)
    stats = mem.get("stats", {})
    # cgroup v2 reports inactive_file, v1 reports total_inactive_file; docker CLI subtracts it
    cache = stats.get("inactive_file", stats.get("total_inactive_file", 0))
    return float(max(usage - cache, 0)), float(mem.get("limit", 0))


class DockerRuntime:
    name = "docker"

    def __init__(self) -> None:
        import docker  # lazy: tests and local runs don't need the SDK

        self._docker = docker
        self.client = docker.from_env(timeout=15)
        self.client.ping()

    def _get(self, container: str):
        try:
            return self.client.containers.get(container)
        except self._docker.errors.NotFound:
            return None

    def _managed(self, c) -> bool:
        return (c.labels or {}).get(MANAGED_LABEL, "").lower() == "true"

    async def info(self, container: str) -> ContainerInfo | None:
        def _info():
            c = self._get(container)
            if c is None:
                return ContainerInfo(container, "missing", None, 0, None, False)
            st = c.attrs.get("State", {})
            return ContainerInfo(
                name=container,
                state=st.get("Status", "unknown"),
                health=(st.get("Health") or {}).get("Status"),
                restart_count=c.attrs.get("RestartCount", 0),
                started_at=st.get("StartedAt"),
                managed=self._managed(c),
            )

        return await asyncio.to_thread(_info)

    async def stats(self, container: str) -> ContainerStats | None:
        def _stats():
            c = self._get(container)
            if c is None or c.status != "running":
                return None
            s = c.stats(stream=False)  # blocks ~1s while Docker takes two samples
            used, limit = memory_from_stats(s)
            return ContainerStats(cpu_percent_from_stats(s), used, limit)

        return await asyncio.to_thread(_stats)

    async def restart(self, container: str, timeout: int = 10) -> None:
        def _restart():
            c = self._get(container)
            if c is None:
                raise RuntimeUnavailable(f"container {container} not found")
            if not self._managed(c):
                raise NotManaged(f"container {container} is not labelled {MANAGED_LABEL}=true")
            c.restart(timeout=timeout)

        await asyncio.to_thread(_restart)

    async def kill(self, container: str) -> None:
        def _kill():
            c = self._get(container)
            if c is None:
                raise RuntimeUnavailable(f"container {container} not found")
            if not self._managed(c):
                raise NotManaged(f"container {container} is not labelled {MANAGED_LABEL}=true")
            c.kill()

        await asyncio.to_thread(_kill)

    async def logs_since(self, container: str, since: datetime) -> list[str]:
        """Lines are prefixed with Docker's RFC3339Nano timestamp so callers can de-duplicate."""

        def _logs():
            c = self._get(container)
            if c is None:
                return []
            ts = since.replace(tzinfo=UTC).timestamp()
            raw = c.logs(since=ts, stdout=True, stderr=True, timestamps=True)
            return raw.decode("utf-8", "replace").splitlines()

        return await asyncio.to_thread(_logs)


class NoopRuntime:
    name = "none"

    async def info(self, container: str) -> ContainerInfo | None:
        return None

    async def stats(self, container: str) -> ContainerStats | None:
        return None

    async def restart(self, container: str, timeout: int = 10) -> None:
        raise RuntimeUnavailable("no container runtime configured (OPS_RUNTIME=none)")

    async def kill(self, container: str) -> None:
        raise RuntimeUnavailable("no container runtime configured (OPS_RUNTIME=none)")

    async def logs_since(self, container: str, since: datetime) -> list[str]:
        return []


def make_runtime(kind: str) -> Runtime:
    if kind == "docker":
        try:
            return DockerRuntime()
        except Exception as exc:
            log.error("Docker runtime unavailable (%s); falling back to no-op runtime", exc)
    return NoopRuntime()
