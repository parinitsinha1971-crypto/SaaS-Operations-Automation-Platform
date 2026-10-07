"""The monitoring agent.

One cycle, every `interval_s`:

  1. probe     run every service's checks, read container state + stats, read new log lines
               (all services concurrently)
  2. evaluate  in dependency order: thresholds, log analysis, anomalies -> verdict ->
               debounced state -> list of Conditions + remediation triggers
  3. persist   samples, check results, log signatures
  4. incidents open / escalate / resolve, push changes to Alertmanager
  5. remediate restart if policy allows, record it on the incident
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from . import metrics as m
from .alerting import Alerter
from .anomaly import AnomalyBank
from .checks import CheckOutcome, run_check
from .config import Level, PlatformConfig, ServiceCfg
from .db import Database, utcnow
from .health import DEGRADED, DOWN, HEALTHY, UNKNOWN, HealthTracker, verdict_from
from .incidents import Condition, IncidentChange, IncidentManager, build_hint
from .logs import DockerLogSource, FileLogSource, LogWindow, NullLogSource, analyze
from .models import CheckResult, LogSignature, ServiceSample
from .remediation import RemediationEngine, RemediationResult
from .runtime import ContainerInfo, ContainerStats, Runtime

log = logging.getLogger("ops.monitor")

LOG_CONDITION_HOLD_S = 120  # keep log/anomaly incidents open this long after the last hit
MIN_REQUESTS_FOR_RATIO = 10


@dataclass
class Probe:
    svc: ServiceCfg
    outcomes: list[CheckOutcome]
    info: ContainerInfo | None
    stats: ContainerStats | None
    lines: list[str]
    duration_ms: float


@dataclass
class ServiceStatus:
    name: str
    label: str
    kind: str
    state: str = UNKNOWN
    raw_state: str = UNKNOWN
    up: bool | None = None
    last_check: str | None = None
    state_since: str | None = None
    checks: list[dict] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    api: dict = field(default_factory=dict)
    logs: dict = field(default_factory=dict)
    anomalies: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    container: dict | None = None
    depends_on: list[str] = field(default_factory=list)
    deps_down: list[str] = field(default_factory=list)
    remediation: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=90))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["history"] = list(self.history)
        return d


def _level(value: float | None, lv: Level) -> str | None:
    if value is None:
        return None
    if value >= lv.crit:
        return "critical"
    if value >= lv.warn:
        return "warning"
    return None


def _topo(services: list[ServiceCfg]) -> list[ServiceCfg]:
    by_name = {s.name: s for s in services}
    out, seen = [], set()

    def visit(s: ServiceCfg, stack: tuple = ()):
        if s.name in seen:
            return
        if s.name in stack:
            raise ValueError(f"dependency cycle: {' -> '.join(stack + (s.name,))}")
        for d in s.depends_on:
            visit(by_name[d], stack + (s.name,))
        seen.add(s.name)
        out.append(s)

    for s in services:
        visit(s)
    return out


class Monitor:
    def __init__(
        self, config: PlatformConfig, db: Database, runtime: Runtime, alerter: Alerter, client: httpx.AsyncClient
    ) -> None:
        self.config = config
        self.db = db
        self.runtime = runtime
        self.alerter = alerter
        self.client = client
        self.incidents = IncidentManager(db)
        self.remediation = RemediationEngine(runtime, db, config)
        d = config.defaults
        self.trackers = {
            s.name: HealthTracker(d.failure_threshold, d.degraded_threshold, d.recovery_threshold)
            for s in config.services
        }
        a = config.anomaly
        self.anomalies = AnomalyBank(a.alpha, a.z_threshold, a.warmup_samples, a.sustain_samples, a.min_values)
        self.order = _topo(config.services)
        self.status = {s.name: ServiceStatus(s.name, s.label, s.kind, depends_on=s.depends_on) for s in config.services}
        self.log_sources = {s.name: self._log_source(s) for s in config.services}
        self._breach: dict[tuple[str, str], int] = {}
        self._held: dict[tuple[str, str], tuple[datetime, Condition]] = {}
        self._last_cycle: datetime | None = None
        self._last_refresh = 0.0
        self.cycles = 0
        self.last_cycle_ms: float | None = None

    def _log_source(self, svc: ServiceCfg):
        if svc.logs.source == "docker" and svc.container:
            return DockerLogSource(self.runtime, svc.container)
        if svc.logs.source == "file" and svc.logs.path:
            return FileLogSource(svc.logs.path)
        return NullLogSource()

    # ------------------------------------------------------------------ startup
    def restore_state(self) -> None:
        """Seed trackers from the last stored sample so a platform restart doesn't flap incidents."""
        cutoff = utcnow() - timedelta(minutes=5)
        with self.db.session() as s:
            for svc in self.config.services:
                row = s.scalar(
                    select(ServiceSample)
                    .where(ServiceSample.service == svc.name)
                    .order_by(ServiceSample.ts.desc())
                    .limit(1)
                )
                if row and row.ts >= cutoff and row.state in (HEALTHY, DEGRADED, DOWN):
                    self.trackers[svc.name].state = row.state
                    self.status[svc.name].state = row.state
                    log.info("restored %s state=%s", svc.name, row.state)

    # ------------------------------------------------------------------ loop
    async def run_forever(self, stop: asyncio.Event) -> None:
        interval = self.config.defaults.interval_s
        log.info(
            "monitor started: %d services, every %ss, runtime=%s, alertmanager=%s",
            len(self.config.services),
            interval,
            self.runtime.name,
            self.alerter.url or "off",
        )
        while not stop.is_set():
            started = time.perf_counter()
            try:
                await self.cycle()
            except Exception:
                log.exception("monitor cycle failed")
            elapsed = time.perf_counter() - started
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.5, interval - elapsed))
            except TimeoutError:
                pass

    async def cycle(self) -> None:
        start = time.perf_counter()
        now = utcnow()
        window_s = (now - self._last_cycle).total_seconds() if self._last_cycle else self.config.defaults.interval_s
        self._last_cycle = now

        probes = await asyncio.gather(*(self._probe(s) for s in self.order))
        for probe in probes:
            await self._handle(probe, now, max(1.0, window_s))

        self._host_disk()
        await asyncio.to_thread(self._incident_gauges)
        if self.alerter.enabled and time.monotonic() - self._last_refresh > 60:
            self._last_refresh = time.monotonic()
            await self.alerter.refresh(await asyncio.to_thread(self.incidents.open_for_alerting))

        self.cycles += 1
        self.last_cycle_ms = round((time.perf_counter() - start) * 1000, 1)
        m.CYCLE_SECONDS.observe(time.perf_counter() - start)
        m.LAST_CYCLE.set(time.time())

    # ------------------------------------------------------------------ probe
    async def _probe(self, svc: ServiceCfg) -> Probe:
        t0 = time.perf_counter()
        timeout = self.config.defaults.timeout_s

        async def _checks():
            return await asyncio.gather(*(run_check(c, self.client, timeout) for c in svc.checks))

        async def _safe(coro, default=None):
            try:
                return await asyncio.wait_for(coro, timeout + 5)
            except Exception as exc:
                log.debug("%s probe step failed: %s", svc.name, exc)
                return default

        outcomes, info, stats, lines = await asyncio.gather(
            _checks(),
            _safe(self.runtime.info(svc.container)) if svc.container else asyncio.sleep(0, None),
            _safe(self.runtime.stats(svc.container))
            if svc.container and svc.resources.cpu_mem == "docker"
            else asyncio.sleep(0, None),
            _safe(self.log_sources[svc.name].read(), []),
        )
        return Probe(svc, list(outcomes), info, stats, lines or [], (time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------------------ evaluate
    def _sustained(self, svc: str, key: str, level: str | None) -> bool:
        k = (svc, key)
        self._breach[k] = self._breach.get(k, 0) + 1 if level else 0
        return self._breach[k] >= self.config.thresholds.sustain_samples

    def _hold(self, svc: str, cond: Condition, now: datetime) -> None:
        self._held[(svc, cond.key)] = (now + timedelta(seconds=LOG_CONDITION_HOLD_S), cond)

    def _held_conditions(self, svc: str, now: datetime) -> list[Condition]:
        out = []
        for (s, key), (until, cond) in list(self._held.items()):
            if s != svc:
                continue
            if now > until:
                del self._held[(s, key)]
            else:
                out.append(cond)
        return out

    def _resources(self, p: Probe) -> dict:
        svc = p.svc
        health_json = next(
            (
                o.extra["json"]
                for o in p.outcomes
                if isinstance(o.extra.get("json"), dict) and "resources" in o.extra["json"]
            ),
            None,
        )
        hres = (health_json or {}).get("resources", {})
        res: dict = {
            "cpu_percent": None,
            "memory_bytes": None,
            "memory_limit_bytes": None,
            "memory_percent": None,
            "disk_used_bytes": None,
            "disk_quota_bytes": None,
            "disk_percent": None,
        }
        if p.stats:
            res.update(
                cpu_percent=p.stats.cpu_percent,
                memory_bytes=p.stats.memory_bytes,
                memory_limit_bytes=p.stats.memory_limit_bytes,
                memory_percent=round(p.stats.memory_percent, 2),
            )
        elif svc.resources.cpu_mem == "health" or (svc.resources.cpu_mem == "docker" and self.runtime.name == "none"):
            if hres:
                res.update(cpu_percent=hres.get("cpu_percent"), memory_bytes=hres.get("memory_rss_bytes"))
                if hres.get("memory_limit_bytes"):
                    res["memory_limit_bytes"] = hres["memory_limit_bytes"]
                    res["memory_percent"] = round(100 * hres["memory_rss_bytes"] / hres["memory_limit_bytes"], 2)

        if svc.resources.disk == "health" and hres.get("disk_quota_bytes"):
            res["disk_used_bytes"] = hres.get("disk_used_bytes", 0)
            res["disk_quota_bytes"] = hres["disk_quota_bytes"]
        elif svc.resources.disk == "postgres":
            pg = next((o.extra for o in p.outcomes if o.type == "postgres" and o.ok), None)
            if pg and svc.resources.disk_quota_mb:
                res["disk_used_bytes"] = pg["db_size_bytes"]
                res["disk_quota_bytes"] = svc.resources.disk_quota_mb * 1024 * 1024
        if res["disk_quota_bytes"]:
            res["disk_percent"] = round(100 * res["disk_used_bytes"] / res["disk_quota_bytes"], 2)
        return res

    async def _handle(self, p: Probe, now: datetime, window_s: float) -> None:
        svc, th = p.svc, self.config.thresholds
        st = self.status[svc.name]
        conditions: list[Condition] = []
        check_reasons: list[str] = []  # degrade the availability incident
        other_reasons: list[str] = []  # have their own incidents
        triggers: set[str] = set()

        # ---- checks
        critical_failed = [o for o in p.outcomes if o.critical and not o.ok]
        for o in p.outcomes:
            if not o.ok and not o.critical:
                check_reasons.append(f"{o.name}: {o.detail}")
            elif o.slow:
                check_reasons.append(f"{o.name}: {o.detail}")
            m.CHECK_SUCCESS.labels(svc.name, o.name, o.type).set(1 if o.ok else 0)
            if o.latency_ms is not None:
                m.CHECK_LATENCY.labels(svc.name, o.name, o.type).set(o.latency_ms / 1000)
        up = not critical_failed
        m.SERVICE_UP.labels(svc.name).set(1 if up else 0)
        health_latency = next(
            (o.latency_ms for o in p.outcomes if o.type in ("http", "postgres", "tcp") and o.ok and o.critical), None
        )

        # ---- resources
        res = self._resources(p)
        resource_notes = []
        for key, value, lv, unit in (
            ("cpu", res["cpu_percent"], th.cpu_percent, "% CPU"),
            ("memory", res["memory_percent"], th.memory_percent, "% memory"),
            ("disk", res["disk_percent"], th.disk_percent, "% disk"),
        ):
            level = _level(value, lv)
            if self._sustained(svc.name, key, level):
                title = f"{svc.label} {key} {'critical' if level == 'critical' else 'high'}: {value:.0f}{unit}"
                conditions.append(
                    Condition(
                        f"resource:{key}",
                        "resource",
                        level,
                        title,
                        f"{key} at {value:.1f}{unit} for {th.sustain_samples}+ cycles (warn {lv.warn}, crit {lv.crit})",
                        {"value": value},
                    )
                )
                other_reasons.append(title)
                resource_notes.append(f"{key} {value:.0f}{unit}")
                if key == "memory" and level == "critical":
                    triggers.add("memory_critical")
        if res["cpu_percent"] is not None:
            m.CPU.labels(svc.name).set(res["cpu_percent"])
        if res["memory_bytes"] is not None:
            m.MEM.labels(svc.name).set(res["memory_bytes"])
        if res["memory_percent"] is not None:
            m.MEM_PCT.labels(svc.name).set(res["memory_percent"])
        if res["disk_percent"] is not None:
            m.DISK_PCT.labels(svc.name).set(res["disk_percent"])

        # ---- logs
        lw: LogWindow = analyze(p.lines, svc.logs.rules, window_s)
        for level_name, n in lw.by_level.items():
            m.LOG_LINES.labels(svc.name, level_name).inc(n)
        level = _level(lw.errors_per_min, th.log_errors_per_min)
        if level:
            top = lw.top_signatures(1)
            self._hold(
                svc.name,
                Condition(
                    "logs:error-rate",
                    "logs",
                    level,
                    f"{svc.label} error log burst: {lw.errors_per_min:.0f}/min",
                    f"{lw.errors} error lines in {window_s:.0f}s" + (f"; top: {top[0].template}" if top else ""),
                    {"errors_per_min": round(lw.errors_per_min, 1)},
                ),
                now,
            )
        for rule in svc.logs.rules:
            hits = lw.rule_hits.get(rule.name, 0)
            m.LOG_RULE_HITS.labels(svc.name, rule.name).inc(hits)
            if hits >= rule.min_count:
                self._hold(
                    svc.name,
                    Condition(
                        f"logs:{rule.name}",
                        "logs",
                        rule.severity,
                        f"{svc.label} log pattern '{rule.name}' matched",
                        f"{hits} matching lines; sample: {lw.rule_samples.get(rule.name, '')[:300]}",
                        {"hits": hits, "pattern": rule.pattern},
                    ),
                    now,
                )

        ratio = lw.api_error_ratio if lw.api_requests >= MIN_REQUESTS_FOR_RATIO else None
        if ratio is not None:
            m.API_ERROR_RATIO.labels(svc.name).set(ratio)
            level = _level(ratio, th.api_error_ratio)
            if level:
                self._hold(
                    svc.name,
                    Condition(
                        "api:error-ratio",
                        "logs",
                        level,
                        f"{svc.label} 5xx ratio {ratio * 100:.0f}%",
                        f"{lw.api_5xx}/{lw.api_requests} requests failed in {window_s:.0f}s",
                        {"ratio": ratio},
                    ),
                    now,
                )
        if lw.api_p95_ms is not None:
            m.API_P95.labels(svc.name).set(lw.api_p95_ms / 1000)

        # ---- anomalies (only on a reachable service; an outage isn't an "anomaly")
        anomalies = []
        if self.config.anomaly.enabled and up:
            for metric, value in (
                ("health_latency_ms", health_latency),
                ("api_p95_ms", lw.api_p95_ms),
                ("api_error_ratio", ratio),
                ("log_errors", lw.errors_per_min),
            ):
                r = self.anomalies.observe(svc.name, metric, value)
                if r is None:
                    continue
                m.ANOMALY_Z.labels(svc.name, metric).set(r.z)
                m.ANOMALY_ACTIVE.labels(svc.name, metric).set(1 if r.anomalous else 0)
                if r.anomalous:
                    anomalies.append(asdict(r))
                    self._hold(
                        svc.name,
                        Condition(
                            f"anomaly:{metric}",
                            "anomaly",
                            "warning",
                            f"{svc.label} anomalous {metric.replace('_', ' ')}: {r.value:.3g} (baseline {r.mean:.3g})",
                            f"z-score {r.z} over EWMA baseline mean={r.mean} std={r.std}",
                            {"z": r.z, "value": r.value},
                        ),
                        now,
                    )

        held = self._held_conditions(svc.name, now)
        conditions += held
        other_reasons += [c.title for c in held]

        # ---- dependencies
        deps_down = [d for d in svc.depends_on if self.status[d].state == DOWN]
        deps_degraded = [d for d in svc.depends_on if self.status[d].state == DEGRADED]
        if deps_down:
            check_reasons.append(f"upstream down: {', '.join(deps_down)}")

        # ---- verdict -> debounced state
        verdict = verdict_from(bool(critical_failed), check_reasons + other_reasons)
        in_grace = self.remediation.in_grace(svc, now)
        tracker = self.trackers[svc.name]
        transition = None
        if not (in_grace and verdict == DOWN):
            transition = tracker.observe(verdict)
        state = tracker.state
        m.set_state(svc.name, state)
        if transition:
            log.info("%s: %s -> %s", svc.name, transition.old, transition.new)
            st.state_since = now.isoformat() + "Z"

        exited = bool(p.info and p.info.state in ("exited", "dead"))
        if exited:
            triggers.add("exited")

        if state == DOWN:
            detail = "; ".join(f"{o.name}: {o.detail}" for o in critical_failed) or "critical checks failing"
            conditions.append(
                Condition(
                    "availability",
                    "availability",
                    "critical",
                    f"{svc.label} is down",
                    detail,
                    {"failed_checks": [o.name for o in critical_failed]},
                )
            )
            # only restart while it is failing *now*: a service that just came back passes its
            # checks but stays "down" until recovery_threshold healthy cycles have passed
            if critical_failed:
                triggers.add("down")
        elif exited:
            # A crashed container is restarted on the next cycle, usually before the debounced
            # state reaches "down" - record it anyway so crashes are never invisible.
            conditions.append(
                Condition(
                    "availability",
                    "availability",
                    "critical",
                    f"{svc.label} crashed (container {p.info.state})",
                    f"Container {svc.container} is {p.info.state}",
                    {"container_state": p.info.state},
                )
            )
        elif state == DEGRADED and check_reasons:
            conditions.append(
                Condition(
                    "availability", "availability", "warning", f"{svc.label} is degraded", "; ".join(check_reasons)
                )
            )
        if self.remediation.is_suspended(svc.name):
            conditions.append(
                Condition(
                    "remediation",
                    "remediation",
                    "critical",
                    f"{svc.label}: auto-restart exhausted, needs a human",
                    "Restart circuit breaker is open",
                )
            )

        if state == HEALTHY and up:
            if self.remediation.note_healthy(svc, now):
                log.info("%s healthy for a full window; restart circuit closed", svc.name)
        else:
            self.remediation.note_unhealthy(svc)

        # ---- live status for the API/UI
        st.state, st.raw_state, st.up = state, verdict, up
        st.last_check = now.isoformat() + "Z"
        st.checks = [{k: v for k, v in asdict(o).items() if k != "extra"} for o in p.outcomes]
        st.resources = res
        st.api = {"requests": lw.api_requests, "errors_5xx": lw.api_5xx, "error_ratio": ratio, "p95_ms": lw.api_p95_ms}
        st.logs = {
            "lines": lw.lines,
            "errors": lw.errors,
            "errors_per_min": round(lw.errors_per_min, 1),
            "by_level": dict(lw.by_level),
            "top": [{"template": s.template, "count": s.count, "sample": s.sample} for s in lw.top_signatures(3)],
        }
        st.anomalies = anomalies
        st.reasons = check_reasons + other_reasons
        st.deps_down = deps_down
        st.container = asdict(p.info) if p.info else None
        st.remediation = self.remediation.status(svc)
        st.history.append(
            {
                "ts": st.last_check,
                "up": up,
                "state": state,
                "latency_ms": health_latency,
                "cpu": res["cpu_percent"],
                "mem_pct": res["memory_percent"],
                "mem_bytes": res["memory_bytes"],
                "disk_pct": res["disk_percent"],
                "p95_ms": lw.api_p95_ms,
                "err_ratio": ratio,
                "log_errors": lw.errors,
            }
        )

        hint = build_hint(
            container_state=p.info.state if p.info else None,
            deps_down=deps_down,
            deps_degraded=deps_degraded,
            failed_checks=[f"{o.name} ({o.detail})" for o in critical_failed],
            top_errors=[s.template for s in lw.top_signatures(1)],
            resource_notes=resource_notes,
        )

        # ---- persist + incidents (DB work off the event loop)
        changes = await asyncio.to_thread(
            self._persist_and_sync, svc, p, st, res, lw, health_latency, now, conditions, hint
        )
        delivered = await self.alerter.notify(changes)
        for c in changes:
            if c.change == "opened":
                m.INCIDENTS_TOTAL.labels(svc.name, c.kind, c.severity).inc()
        if delivered:
            await asyncio.to_thread(self._mark_alerted, delivered)

        # ---- remediation (attached to the incident that explains it)
        incident_id = await asyncio.to_thread(self.incidents.open_incident_id, svc.name)
        if incident_id is None and "memory_critical" in triggers:
            incident_id = await asyncio.to_thread(self.incidents.open_incident_id, svc.name, "resource:memory")
        result = await self.remediation.evaluate(svc, triggers, deps_down, incident_id, now)
        if result:
            await asyncio.to_thread(self._record_remediation, result, incident_id)
            if result.outcome == "restarted":
                tracker.reset()

    # ------------------------------------------------------------------ persistence
    def _persist_and_sync(
        self,
        svc,
        p: Probe,
        st: ServiceStatus,
        res: dict,
        lw: LogWindow,
        health_latency,
        now: datetime,
        conditions: list[Condition],
        hint,
    ) -> list[IncidentChange]:
        with self.db.session() as s:
            s.add(
                ServiceSample(
                    service=svc.name,
                    ts=now,
                    state=st.state,
                    raw_state=st.raw_state,
                    up=bool(st.up),
                    health_latency_ms=health_latency,
                    cpu_percent=res["cpu_percent"],
                    memory_bytes=res["memory_bytes"],
                    memory_percent=res["memory_percent"],
                    disk_percent=res["disk_percent"],
                    api_requests=lw.api_requests,
                    api_error_ratio=st.api["error_ratio"],
                    api_p95_ms=lw.api_p95_ms,
                    log_errors=lw.errors,
                    container_state=p.info.state if p.info else None,
                )
            )
            for o in p.outcomes:
                s.add(
                    CheckResult(
                        service=svc.name,
                        check_name=o.name,
                        check_type=o.type,
                        ts=now,
                        ok=o.ok,
                        critical=o.critical,
                        latency_ms=o.latency_ms,
                        status_code=o.status_code,
                        detail=(o.detail or None) and o.detail[:500],
                    )
                )
            if lw.signatures:
                day = now.date()
                existing = {
                    r.fingerprint: r
                    for r in s.scalars(
                        select(LogSignature).where(
                            LogSignature.service == svc.name,
                            LogSignature.day == day,
                            LogSignature.fingerprint.in_(list(lw.signatures)),
                        )
                    )
                }
                for fp, sig in lw.signatures.items():
                    row = existing.get(fp)
                    if row:
                        row.count += sig.count
                        row.last_seen, row.sample = now, sig.sample
                    else:
                        s.add(
                            LogSignature(
                                service=svc.name,
                                day=day,
                                fingerprint=fp,
                                template=sig.template,
                                sample=sig.sample,
                                level=sig.level,
                                count=sig.count,
                                first_seen=now,
                                last_seen=now,
                            )
                        )
        return self.incidents.sync(svc.name, conditions, hint)

    def _mark_alerted(self, changes: list[IncidentChange]) -> None:
        for c in changes:
            verb = "resolution sent" if c.change == "resolved" else "alert sent"
            self.incidents.add_event(c.incident_id, "alert", f"Alertmanager: {verb} ({c.severity})")

    def _record_remediation(self, r: RemediationResult, incident_id: int | None) -> None:
        if incident_id is None:
            return
        type_ = {
            "restarted": "restart",
            "failed": "restart_failed",
            "skipped": "restart_skipped",
            "suspended": "escalated",
        }[r.outcome]
        self.incidents.add_event(incident_id, type_, r.detail, {"reason": r.reason, "action_id": r.action_id})

    def _incident_gauges(self) -> None:
        counts = self.incidents.open_counts()
        for svc in self.config.services:
            for sev in ("warning", "critical"):
                m.INCIDENTS_OPEN.labels(svc.name, sev).set(counts.get((svc.name, sev), 0))

    def _host_disk(self) -> None:
        for path in self.config.host_disk_paths:
            try:
                u = shutil.disk_usage(path)
                m.HOST_DISK_PCT.labels(path).set(round(100 * u.used / u.total, 2))
            except OSError:
                pass

    # ------------------------------------------------------------------ manual ops
    async def manual_restart(self, svc: ServiceCfg, who: str) -> RemediationResult:
        incident_id = await asyncio.to_thread(self.incidents.open_incident_id, svc.name)
        result = await self.remediation.manual_restart(svc, who, incident_id)
        await asyncio.to_thread(self._record_remediation, result, incident_id)
        if result.outcome == "restarted":
            self.trackers[svc.name].reset()
        return result

    def host_info(self) -> dict:
        out = {}
        for path in self.config.host_disk_paths:
            try:
                u = shutil.disk_usage(path)
                out[path] = {"total": u.total, "used": u.used, "percent": round(100 * u.used / u.total, 1)}
            except OSError:
                pass
        return out
