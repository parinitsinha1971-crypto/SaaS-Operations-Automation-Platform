"""Incident lifecycle.

The monitor describes each service's problems as a list of Conditions every
cycle. IncidentManager.sync() turns that into incidents:

  * a new condition key            -> open an incident (deduplicated per service+key)
  * same key, higher severity      -> escalate the open incident
  * key no longer present          -> resolve the incident

Incidents from Prometheus alerts (kind="prometheus") are opened and resolved by
the Alertmanager webhook instead, so sync() leaves them alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .db import Database, utcnow
from .models import Incident, IncidentEvent

SEV_RANK = {"warning": 1, "critical": 2}
MANAGED_KINDS = ("availability", "resource", "logs", "anomaly", "remediation")


@dataclass
class Condition:
    key: str
    kind: str
    severity: str
    title: str
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class IncidentChange:
    incident_id: int
    service: str
    key: str
    kind: str
    severity: str
    title: str
    change: str  # opened | escalated | resolved
    opened_at: str
    detail: str = ""


class IncidentManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    def sync(self, service: str, conditions: list[Condition], hint: str | None = None) -> list[IncidentChange]:
        changes: list[IncidentChange] = []
        now = utcnow()
        with self.db.session() as s:
            open_incs = {
                i.key: i
                for i in s.scalars(
                    select(Incident).where(
                        Incident.service == service, Incident.status == "open", Incident.kind.in_(MANAGED_KINDS)
                    )
                )
            }
            active = {c.key: c for c in conditions}

            for key, cond in active.items():
                inc = open_incs.get(key)
                if inc is None:
                    inc = Incident(
                        service=service,
                        key=key,
                        kind=cond.kind,
                        severity=cond.severity,
                        title=cond.title,
                        opened_at=now,
                        root_cause_hint=hint,
                    )
                    inc.events.append(IncidentEvent(ts=now, type="detected", message=cond.detail, data=cond.data))
                    s.add(inc)
                    s.flush()
                    changes.append(_change(inc, "opened", cond.detail))
                elif SEV_RANK[cond.severity] > SEV_RANK[inc.severity]:
                    inc.severity, inc.title = cond.severity, cond.title
                    inc.events.append(IncidentEvent(ts=now, type="escalated", message=cond.detail, data=cond.data))
                    changes.append(_change(inc, "escalated", cond.detail))

            for key, inc in open_incs.items():
                if key not in active:
                    inc.status, inc.resolved_at = "resolved", now
                    msg = f"Recovered after {_fmt_duration((now - inc.opened_at).total_seconds())}"
                    inc.events.append(IncidentEvent(ts=now, type="resolved", message=msg))
                    changes.append(_change(inc, "resolved", msg))
        return changes

    def add_event(self, incident_id: int, type_: str, message: str, data: dict | None = None) -> None:
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                return
            inc.events.append(IncidentEvent(ts=utcnow(), type=type_, message=message, data=data))
            if type_ == "restart":
                inc.restarts += 1

    def open_incident_id(self, service: str, key: str = "availability") -> int | None:
        with self.db.session() as s:
            return s.scalar(
                select(Incident.id).where(Incident.service == service, Incident.key == key, Incident.status == "open")
            )

    def open_for_alerting(self) -> list[Incident]:
        with self.db.session() as s:
            return list(s.scalars(select(Incident).where(Incident.status == "open", Incident.kind.in_(MANAGED_KINDS))))

    def open_counts(self) -> dict[tuple[str, str], int]:
        with self.db.session() as s:
            rows = s.execute(
                select(Incident.service, Incident.severity, func.count())
                .where(Incident.status == "open")
                .group_by(Incident.service, Incident.severity)
            ).all()
        return {(svc, sev): n for svc, sev, n in rows}

    # ---- operator actions ------------------------------------------------
    def acknowledge(self, incident_id: int, who: str) -> Incident | None:
        with self.db.session() as s:
            inc = s.get(Incident, incident_id, options=[selectinload(Incident.events)])
            if inc is None:
                return None
            inc.acknowledged_by = who
            inc.events.append(IncidentEvent(ts=utcnow(), type="acknowledged", message=f"Acknowledged by {who}"))
            return inc

    def note(self, incident_id: int, who: str, text: str) -> Incident | None:
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None:
                return None
            inc.events.append(IncidentEvent(ts=utcnow(), type="note", message=text, data={"by": who}))
            return inc

    def resolve(self, incident_id: int, who: str, reason: str = "") -> Incident | None:
        with self.db.session() as s:
            inc = s.get(Incident, incident_id)
            if inc is None or inc.status == "resolved":
                return inc
            inc.status, inc.resolved_at = "resolved", utcnow()
            inc.events.append(
                IncidentEvent(ts=utcnow(), type="resolved", message=f"Resolved manually by {who}. {reason}".strip())
            )
            return inc

    # ---- Prometheus/Alertmanager-sourced incidents ---------------------------
    def upsert_external(
        self, service: str, alertname: str, severity: str, status: str, summary: str
    ) -> IncidentChange | None:
        key = f"prom:{alertname}"
        now = utcnow()
        with self.db.session() as s:
            inc = s.scalar(
                select(Incident).where(Incident.service == service, Incident.key == key, Incident.status == "open")
            )
            if status == "firing" and inc is None:
                inc = Incident(
                    service=service,
                    key=key,
                    kind="prometheus",
                    severity=severity if severity in SEV_RANK else "warning",
                    title=f"{alertname}: {summary}"[:255],
                    opened_at=now,
                )
                inc.events.append(
                    IncidentEvent(
                        ts=now,
                        type="detected",
                        message=f"Prometheus alert {alertname} firing",
                        data={"summary": summary},
                    )
                )
                s.add(inc)
                s.flush()
                return _change(inc, "opened", summary)
            if status == "resolved" and inc is not None:
                inc.status, inc.resolved_at = "resolved", now
                inc.events.append(
                    IncidentEvent(ts=now, type="resolved", message=f"Prometheus alert {alertname} resolved")
                )
                return _change(inc, "resolved", summary)
        return None


def _change(inc: Incident, change: str, detail: str) -> IncidentChange:
    return IncidentChange(
        incident_id=inc.id,
        service=inc.service,
        key=inc.key,
        kind=inc.kind,
        severity=inc.severity,
        title=inc.title,
        change=change,
        opened_at=inc.opened_at.isoformat() + "Z",
        detail=detail,
    )


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def build_hint(
    *,
    container_state: str | None,
    deps_down: list[str],
    failed_checks: list[str],
    deps_degraded: list[str] | None = None,
    top_errors: list[str],
    resource_notes: list[str],
) -> str | None:
    """A short, human-readable guess at the cause, attached when an incident opens."""
    parts = []
    if deps_down:
        parts.append(f"Upstream dependency down: {', '.join(deps_down)}. Restarting this service is unlikely to help.")
    elif deps_degraded:
        parts.append(f"Upstream dependency degraded: {', '.join(deps_degraded)}. This is likely a knock-on effect.")
    if container_state and container_state not in ("running", "missing"):
        parts.append(f"Container is {container_state}.")
    if failed_checks:
        parts.append("Failing checks: " + "; ".join(failed_checks[:3]) + ".")
    if resource_notes:
        parts.append("Resources: " + "; ".join(resource_notes) + ".")
    if top_errors:
        parts.append(f"Most frequent recent error: “{top_errors[0][:160]}”.")
    return " ".join(parts) or None
