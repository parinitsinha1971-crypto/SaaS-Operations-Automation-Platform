"""ORM models.

service_samples   one row per service per monitor cycle (state + resources + API stats)
check_results     one row per check per cycle
incidents         deduplicated problems, with an event timeline
remediation       every restart attempt, automatic or manual
log_signatures    fingerprinted log errors, bucketed per day
alerts            notifications received back from Alertmanager
reports           generated daily reports
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


class ServiceSample(Base):
    __tablename__ = "service_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    state: Mapped[str] = mapped_column(String(16))  # debounced state
    raw_state: Mapped[str] = mapped_column(String(16))  # this cycle's verdict
    up: Mapped[bool] = mapped_column(Boolean)  # all critical checks passed
    health_latency_ms: Mapped[float | None] = mapped_column(Float)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    memory_bytes: Mapped[float | None] = mapped_column(Float)
    memory_percent: Mapped[float | None] = mapped_column(Float)
    disk_percent: Mapped[float | None] = mapped_column(Float)
    api_requests: Mapped[int | None] = mapped_column(Integer)
    api_error_ratio: Mapped[float | None] = mapped_column(Float)
    api_p95_ms: Mapped[float | None] = mapped_column(Float)
    log_errors: Mapped[int | None] = mapped_column(Integer)
    container_state: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_samples_service_ts", "service", "ts"),)


class CheckResult(Base):
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64))
    check_name: Mapped[str] = mapped_column(String(64))
    check_type: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean)
    critical: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    status_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_checks_service_ts", "service", "ts"),)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128))  # dedupe key within a service, e.g. "availability"
    kind: Mapped[str] = mapped_column(String(32))  # availability|resource|logs|anomaly|prometheus|remediation
    severity: Mapped[str] = mapped_column(String(16))  # warning|critical
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|resolved
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    root_cause_hint: Mapped[str | None] = mapped_column(Text)
    restarts: Mapped[int] = mapped_column(Integer, default=0)

    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", order_by="IncidentEvent.ts", cascade="all, delete-orphan"
    )

    @property
    def duration_s(self) -> float:
        end = self.resolved_at or utcnow()
        return (end - self.opened_at).total_seconds()


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)

    incident: Mapped[Incident] = relationship(back_populates="events")


class RemediationAction(Base):
    __tablename__ = "remediation_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(32))  # restart
    reason: Mapped[str] = mapped_column(String(255))
    initiated_by: Mapped[str] = mapped_column(String(64))  # auto | api
    status: Mapped[str] = mapped_column(String(16))  # success|failed|skipped
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"))


class LogSignature(Base):
    __tablename__ = "log_signatures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    fingerprint: Mapped[str] = mapped_column(String(64))
    template: Mapped[str] = mapped_column(Text)
    sample: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("service", "day", "fingerprint", name="uq_logsig"),)


class AlertRecord(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    alertname: Mapped[str] = mapped_column(String(128))
    service: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # firing|resolved
    source: Mapped[str] = mapped_column(String(32))  # prometheus|ops-platform
    summary: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict | None] = mapped_column(JSON)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    data: Mapped[dict] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)
