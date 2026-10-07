"""Automatic remediation (container restarts) with guard rails.

Guard rails, in the order they're applied:
  1. policy     remediation enabled, service has a container, trigger is in restart_on
  2. circuit    after `max_restarts` within `window_minutes`, auto-restart is
                suspended and the incident is escalated - a restart loop is a
                human's problem
  3. dependency if an upstream service is down, restarting this one won't
                help, so skip (and say so on the incident)
  4. grace      no new restart while a freshly restarted service is booting
  5. backoff    attempt n waits backoff_s[n] after the previous attempt

The circuit closes again after the service stays healthy for a full window,
or when an operator calls resume.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import PlatformConfig, ServiceCfg
from .db import Database, utcnow
from .metrics import REMEDIATION_SUSPENDED, RESTARTS
from .models import RemediationAction
from .runtime import Runtime

log = logging.getLogger("ops.remediation")


@dataclass
class _SvcState:
    attempts: deque = field(default_factory=deque)  # datetimes of restart attempts in window
    last_attempt: datetime | None = None
    suspended: bool = False
    suspended_at: datetime | None = None
    healthy_since: datetime | None = None
    last_skip_reason: str | None = None


@dataclass
class RemediationResult:
    service: str
    outcome: str  # restarted | failed | skipped | suspended
    reason: str
    detail: str = ""
    action_id: int | None = None


class RemediationEngine:
    def __init__(self, runtime: Runtime, db: Database, config: PlatformConfig) -> None:
        self.runtime = runtime
        self.db = db
        self.config = config
        self.state: dict[str, _SvcState] = {s.name: _SvcState() for s in config.services}

    # ---- queries --------------------------------------------------------
    def _st(self, service: str) -> _SvcState:
        return self.state.setdefault(service, _SvcState())

    def in_grace(self, svc: ServiceCfg, now: datetime | None = None) -> bool:
        st = self._st(svc.name)
        if st.last_attempt is None:
            return False
        grace = self.config.remediation_for(svc).grace_s
        return ((now or utcnow()) - st.last_attempt).total_seconds() < grace

    def is_suspended(self, service: str) -> bool:
        return self._st(service).suspended

    def status(self, svc: ServiceCfg) -> dict:
        st = self._st(svc.name)
        policy = self.config.remediation_for(svc)
        self._prune(st, policy.window_minutes)
        return {
            "enabled": policy.enabled and bool(svc.container),
            "suspended": st.suspended,
            "suspended_at": st.suspended_at.isoformat() + "Z" if st.suspended_at else None,
            "attempts_in_window": len(st.attempts),
            "max_restarts": policy.max_restarts,
            "window_minutes": policy.window_minutes,
            "last_attempt": st.last_attempt.isoformat() + "Z" if st.last_attempt else None,
        }

    # ---- lifecycle hooks --------------------------------------------------
    def note_healthy(self, svc: ServiceCfg, now: datetime | None = None) -> bool:
        """Call every healthy cycle. Returns True if the circuit just closed."""
        now = now or utcnow()
        st = self._st(svc.name)
        if st.healthy_since is None:
            st.healthy_since = now
        st.last_skip_reason = None
        window = timedelta(minutes=self.config.remediation_for(svc).window_minutes)
        if st.suspended and now - st.healthy_since >= window:
            self.resume(svc.name)
            return True
        return False

    def note_unhealthy(self, svc: ServiceCfg) -> None:
        self._st(svc.name).healthy_since = None

    def resume(self, service: str) -> None:
        st = self._st(service)
        st.suspended, st.suspended_at = False, None
        st.attempts.clear()
        REMEDIATION_SUSPENDED.labels(service).set(0)
        log.info("auto-restart resumed for %s", service)

    @staticmethod
    def _prune(st: _SvcState, window_minutes: int, now: datetime | None = None) -> None:
        cutoff = (now or utcnow()) - timedelta(minutes=window_minutes)
        while st.attempts and st.attempts[0] < cutoff:
            st.attempts.popleft()

    # ---- decision ------------------------------------------------------------
    async def evaluate(
        self,
        svc: ServiceCfg,
        triggers: set[str],
        deps_down: list[str],
        incident_id: int | None,
        now: datetime | None = None,
    ) -> RemediationResult | None:
        now = now or utcnow()
        policy = self.config.remediation_for(svc)
        if not policy.enabled or not svc.container:
            return None
        reasons = sorted(triggers & set(policy.restart_on))
        if not reasons:
            return None
        reason = ",".join(reasons)
        st = self._st(svc.name)
        if st.suspended:
            return None

        if deps_down:
            msg = f"upstream down: {', '.join(deps_down)}"
            if st.last_skip_reason != msg:  # record once per streak, not every cycle
                st.last_skip_reason = msg
                aid = self._record(svc.name, reason, "auto", "skipped", msg, None, incident_id)
                return RemediationResult(svc.name, "skipped", reason, msg, aid)
            return None

        if self.in_grace(svc, now):
            return None

        self._prune(st, policy.window_minutes, now)
        n = len(st.attempts)
        if n >= policy.max_restarts:
            st.suspended, st.suspended_at = True, now
            REMEDIATION_SUSPENDED.labels(svc.name).set(1)
            msg = (
                f"{n} restarts in {policy.window_minutes}m did not fix {svc.name}; "
                "auto-restart suspended, escalating to on-call"
            )
            log.error(msg)
            return RemediationResult(svc.name, "suspended", reason, msg)

        backoff = policy.backoff_s[min(n, len(policy.backoff_s) - 1)] if policy.backoff_s else 0
        if st.last_attempt and (now - st.last_attempt).total_seconds() < backoff:
            return None

        return await self._restart(svc, reason, "auto", incident_id, now)

    async def manual_restart(self, svc: ServiceCfg, who: str, incident_id: int | None) -> RemediationResult:
        if not svc.container:
            return RemediationResult(svc.name, "failed", "manual", "service has no container configured")
        return await self._restart(svc, "manual", who, incident_id, utcnow(), count_toward_circuit=False)

    async def _restart(
        self,
        svc: ServiceCfg,
        reason: str,
        who: str,
        incident_id: int | None,
        now: datetime,
        count_toward_circuit: bool = True,
    ) -> RemediationResult:
        st = self._st(svc.name)
        st.last_attempt = now
        if count_toward_circuit:
            st.attempts.append(now)
        start = time.perf_counter()
        try:
            await self.runtime.restart(svc.container)
        except Exception as exc:
            ms = (time.perf_counter() - start) * 1000
            aid = self._record(svc.name, reason, who, "failed", str(exc), ms, incident_id)
            RESTARTS.labels(svc.name, "failed", "auto" if who == "auto" else "manual").inc()
            log.error("restart of %s failed: %s", svc.container, exc)
            return RemediationResult(svc.name, "failed", reason, str(exc), aid)
        ms = (time.perf_counter() - start) * 1000
        aid = self._record(svc.name, reason, who, "success", None, ms, incident_id)
        RESTARTS.labels(svc.name, "success", "auto" if who == "auto" else "manual").inc()
        attempt = len(st.attempts)
        detail = f"restarted container {svc.container} in {ms:.0f}ms"
        if count_toward_circuit:
            detail += f" (attempt {attempt}/{self.config.remediation_for(svc).max_restarts})"
        log.warning("%s: %s", svc.name, detail)
        return RemediationResult(svc.name, "restarted", reason, detail, aid)

    def _record(self, service, reason, who, status, error, ms, incident_id) -> int:
        with self.db.session() as s:
            row = RemediationAction(
                service=service,
                action="restart",
                reason=reason,
                initiated_by=who,
                status=status,
                error=error,
                duration_ms=ms,
                incident_id=incident_id,
                started_at=utcnow(),
            )
            s.add(row)
            s.flush()
            return row.id
