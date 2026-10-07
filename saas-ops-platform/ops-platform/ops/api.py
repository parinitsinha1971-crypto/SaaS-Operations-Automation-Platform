"""REST API."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from .db import utcnow
from .health import DEGRADED, DOWN, HEALTHY
from .models import AlertRecord, CheckResult, Incident, LogSignature, RemediationAction, Report, ServiceSample
from .reports import update_slo_gauges
from .scheduler import generate_report
from .security import ReadAccess, WriteAccess, operator, require_webhook_token

router = APIRouter(prefix="/api")


def P(request: Request):
    return request.app.state.platform


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def incident_dict(i: Incident, events: bool = False) -> dict:
    d = {
        "id": i.id,
        "service": i.service,
        "key": i.key,
        "kind": i.kind,
        "severity": i.severity,
        "title": i.title,
        "status": i.status,
        "opened_at": _iso(i.opened_at),
        "resolved_at": _iso(i.resolved_at),
        "duration_s": round(i.duration_s),
        "acknowledged_by": i.acknowledged_by,
        "root_cause_hint": i.root_cause_hint,
        "restarts": i.restarts,
    }
    if events:
        d["events"] = [{"ts": _iso(e.ts), "type": e.type, "message": e.message, "data": e.data} for e in i.events]
    return d


def action_dict(a: RemediationAction) -> dict:
    return {
        "id": a.id,
        "service": a.service,
        "action": a.action,
        "reason": a.reason,
        "initiated_by": a.initiated_by,
        "status": a.status,
        "error": a.error,
        "started_at": _iso(a.started_at),
        "duration_ms": a.duration_ms,
        "incident_id": a.incident_id,
    }


def _svc(request: Request, name: str):
    svc = P(request).config.service(name)
    if svc is None:
        raise HTTPException(404, f"unknown service '{name}'")
    return svc


# ------------------------------------------------------------------ overview & services
@router.get("/overview", dependencies=[ReadAccess])
def overview(request: Request):
    p = P(request)
    statuses = [p.monitor.status[s.name].to_dict() for s in p.config.services]
    with p.db.session() as s:
        open_incs = s.scalars(
            select(Incident).where(Incident.status == "open").order_by(desc(Incident.opened_at))
        ).all()
        actions = s.scalars(select(RemediationAction).order_by(desc(RemediationAction.started_at)).limit(8)).all()
        recent = s.scalars(select(Incident).order_by(desc(Incident.opened_at)).limit(8)).all()
        open_list = [incident_dict(i) for i in open_incs]
        action_list = [action_dict(a) for a in actions]
        recent_list = [incident_dict(i) for i in recent]
    counts = {HEALTHY: 0, DEGRADED: 0, DOWN: 0, "unknown": 0}
    for st in statuses:
        counts[st["state"]] = counts.get(st["state"], 0) + 1
    return {
        "generated_at": _iso(utcnow()),
        "counts": counts,
        "services": statuses,
        "open_incidents": open_list,
        "recent_incidents": recent_list,
        "recent_actions": action_list,
        "slo": p.slo_cache,
        "host_disk": p.monitor.host_info(),
        "agent": {
            "cycles": p.monitor.cycles,
            "last_cycle_ms": p.monitor.last_cycle_ms,
            "runtime": p.runtime.name,
            "interval_s": p.config.defaults.interval_s,
            "alertmanager": bool(p.settings.alertmanager_url),
        },
        "links": {"grafana": p.settings.grafana_url},
    }


@router.get("/services", dependencies=[ReadAccess])
def list_services(request: Request):
    p = P(request)
    return [p.monitor.status[s.name].to_dict() for s in p.config.services]


@router.get("/services/{name}", dependencies=[ReadAccess])
def get_service(name: str, request: Request):
    p = P(request)
    svc = _svc(request, name)
    with p.db.session() as s:
        checks = s.scalars(
            select(CheckResult)
            .where(CheckResult.service == name, CheckResult.ok.is_(False))
            .order_by(desc(CheckResult.ts))
            .limit(15)
        ).all()
        actions = s.scalars(
            select(RemediationAction)
            .where(RemediationAction.service == name)
            .order_by(desc(RemediationAction.started_at))
            .limit(10)
        ).all()
        incs = s.scalars(
            select(Incident).where(Incident.service == name).order_by(desc(Incident.opened_at)).limit(10)
        ).all()
        sigs = s.scalars(
            select(LogSignature)
            .where(LogSignature.service == name, LogSignature.day == utcnow().date())
            .order_by(desc(LogSignature.count))
            .limit(8)
        ).all()
        data = {
            "status": p.monitor.status[name].to_dict(),
            "config": svc.model_dump(mode="json"),
            "recent_failures": [
                {
                    "ts": _iso(c.ts),
                    "check": c.check_name,
                    "type": c.check_type,
                    "detail": c.detail,
                    "latency_ms": c.latency_ms,
                }
                for c in checks
            ],
            "actions": [action_dict(a) for a in actions],
            "incidents": [incident_dict(i) for i in incs],
            "log_signatures": [
                {
                    "template": g.template,
                    "sample": g.sample,
                    "level": g.level,
                    "count": g.count,
                    "last_seen": _iso(g.last_seen),
                }
                for g in sigs
            ],
            "slo": p.slo_cache.get(name),
        }
    return data


@router.get("/services/{name}/history", dependencies=[ReadAccess])
def service_history(name: str, request: Request, minutes: int = Query(60, ge=1, le=60 * 24 * 7)):
    _svc(request, name)
    since = utcnow() - timedelta(minutes=minutes)
    with P(request).db.session() as s:
        rows = s.scalars(
            select(ServiceSample)
            .where(ServiceSample.service == name, ServiceSample.ts >= since)
            .order_by(ServiceSample.ts)
        ).all()
        return [
            {
                "ts": _iso(r.ts),
                "state": r.state,
                "up": r.up,
                "latency_ms": r.health_latency_ms,
                "cpu": r.cpu_percent,
                "mem_pct": r.memory_percent,
                "disk_pct": r.disk_percent,
                "p95_ms": r.api_p95_ms,
                "err_ratio": r.api_error_ratio,
                "log_errors": r.log_errors,
            }
            for r in rows
        ]


@router.post("/services/{name}/restart", dependencies=[WriteAccess])
async def restart_service(name: str, request: Request, who: str = Depends(operator)):
    svc = _svc(request, name)
    r = await P(request).monitor.manual_restart(svc, who)
    if r.outcome != "restarted":
        raise HTTPException(409, f"restart failed: {r.detail}")
    return {"service": name, "outcome": r.outcome, "detail": r.detail, "action_id": r.action_id}


@router.post("/services/{name}/remediation/resume", dependencies=[WriteAccess])
def resume_remediation(name: str, request: Request):
    _svc(request, name)
    P(request).monitor.remediation.resume(name)
    return {"service": name, "suspended": False}


# ------------------------------------------------------------------ incidents
@router.get("/incidents", dependencies=[ReadAccess])
def list_incidents(
    request: Request,
    status: Literal["open", "resolved", "all"] = "all",
    service: str | None = None,
    severity: str | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
):
    q = select(Incident).order_by(desc(Incident.opened_at))
    if status != "all":
        q = q.where(Incident.status == status)
    if service:
        q = q.where(Incident.service == service)
    if severity:
        q = q.where(Incident.severity == severity)
    with P(request).db.session() as s:
        return [incident_dict(i) for i in s.scalars(q.limit(limit).offset(offset)).all()]


@router.get("/incidents/{incident_id}", dependencies=[ReadAccess])
def get_incident(incident_id: int, request: Request):
    with P(request).db.session() as s:
        inc = s.get(Incident, incident_id, options=[selectinload(Incident.events)])
        if inc is None:
            raise HTTPException(404, "incident not found")
        d = incident_dict(inc, events=True)
        d["actions"] = [
            action_dict(a)
            for a in s.scalars(
                select(RemediationAction)
                .where(RemediationAction.incident_id == incident_id)
                .order_by(RemediationAction.started_at)
            ).all()
        ]
        return d


class NoteIn(BaseModel):
    text: str


class ResolveIn(BaseModel):
    reason: str = ""


@router.post("/incidents/{incident_id}/ack", dependencies=[WriteAccess])
def ack_incident(incident_id: int, request: Request, who: str = Depends(operator)):
    if P(request).monitor.incidents.acknowledge(incident_id, who) is None:
        raise HTTPException(404, "incident not found")
    return get_incident(incident_id, request)


@router.post("/incidents/{incident_id}/notes", dependencies=[WriteAccess])
def note_incident(incident_id: int, body: NoteIn, request: Request, who: str = Depends(operator)):
    if P(request).monitor.incidents.note(incident_id, who, body.text[:2000]) is None:
        raise HTTPException(404, "incident not found")
    return get_incident(incident_id, request)


@router.post("/incidents/{incident_id}/resolve", dependencies=[WriteAccess])
def resolve_incident(
    incident_id: int, request: Request, body: ResolveIn = Body(default=ResolveIn()), who: str = Depends(operator)
):
    if P(request).monitor.incidents.resolve(incident_id, who, body.reason) is None:
        raise HTTPException(404, "incident not found")
    return get_incident(incident_id, request)


# ------------------------------------------------------------------ remediation, logs
@router.get("/remediation", dependencies=[ReadAccess])
def list_actions(request: Request, service: str | None = None, limit: int = Query(50, le=500)):
    q = select(RemediationAction).order_by(desc(RemediationAction.started_at))
    if service:
        q = q.where(RemediationAction.service == service)
    with P(request).db.session() as s:
        return [action_dict(a) for a in s.scalars(q.limit(limit)).all()]


@router.get("/logs/signatures", dependencies=[ReadAccess])
def log_signatures(
    request: Request, service: str | None = None, day: date | None = None, limit: int = Query(20, le=200)
):
    q = select(LogSignature).where(LogSignature.day == (day or utcnow().date())).order_by(desc(LogSignature.count))
    if service:
        q = q.where(LogSignature.service == service)
    with P(request).db.session() as s:
        return [
            {
                "service": g.service,
                "fingerprint": g.fingerprint,
                "template": g.template,
                "sample": g.sample,
                "level": g.level,
                "count": g.count,
                "first_seen": _iso(g.first_seen),
                "last_seen": _iso(g.last_seen),
            }
            for g in s.scalars(q.limit(limit)).all()
        ]


# ------------------------------------------------------------------ reports
@router.get("/reports", dependencies=[ReadAccess])
def list_reports(request: Request, limit: int = Query(30, le=365)):
    with P(request).db.session() as s:
        rows = s.scalars(select(Report).order_by(desc(Report.day)).limit(limit)).all()
        return [
            {
                "day": r.day.isoformat(),
                "generated_at": _iso(r.generated_at),
                "fleet_availability": r.data.get("fleet_availability"),
                "incidents": r.data.get("incidents_total"),
                "highlights": r.data.get("highlights", []),
            }
            for r in rows
        ]


@router.post("/reports/generate", dependencies=[WriteAccess])
def generate(request: Request, day: date | None = None):
    p = P(request)
    target = day or utcnow().date()
    p.slo_cache = update_slo_gauges(p.db, p.config)
    return generate_report(p.db, p.config, target)


@router.get("/reports/{day}", dependencies=[ReadAccess])
def get_report(day: date, request: Request):
    with P(request).db.session() as s:
        r = s.scalar(select(Report).where(Report.day == day))
        if r is None:
            raise HTTPException(404, "no report for that day; POST /api/reports/generate?day=YYYY-MM-DD to create one")
        return r.data


@router.get("/reports/{day}/markdown", response_class=PlainTextResponse, dependencies=[ReadAccess])
def get_report_md(day: date, request: Request):
    with P(request).db.session() as s:
        r = s.scalar(select(Report).where(Report.day == day))
        if r is None:
            raise HTTPException(404, "no report for that day")
        return r.markdown


# ------------------------------------------------------------------ alerts
@router.post("/alerts/webhook", dependencies=[Depends(require_webhook_token)])
def alertmanager_webhook(request: Request, payload: dict[str, Any] = Body(...)):
    """Alertmanager webhook receiver: records every notification, and turns
    Prometheus-rule alerts into incidents (our own alerts already are incidents)."""
    p = P(request)
    stored, incidents = 0, 0
    with p.db.session() as s:
        for a in payload.get("alerts", []):
            labels, ann = a.get("labels", {}), a.get("annotations", {})
            s.add(
                AlertRecord(
                    fingerprint=a.get("fingerprint"),
                    alertname=labels.get("alertname", "unknown"),
                    service=labels.get("service") or labels.get("job"),
                    severity=labels.get("severity"),
                    status=a.get("status", payload.get("status", "firing")),
                    source=labels.get("source", "prometheus"),
                    summary=ann.get("summary") or ann.get("description"),
                    labels=labels,
                    starts_at=_parse_ts(a.get("startsAt")),
                    ends_at=_parse_ts(a.get("endsAt")),
                )
            )
            stored += 1
    for a in payload.get("alerts", []):
        labels = a.get("labels", {})
        if labels.get("source") == "ops-platform":
            continue
        service = labels.get("service") or labels.get("job") or "platform"
        change = p.monitor.incidents.upsert_external(
            service,
            labels.get("alertname", "unknown"),
            labels.get("severity", "warning"),
            a.get("status", "firing"),
            (a.get("annotations") or {}).get("summary", ""),
        )
        incidents += change is not None
    return {"stored": stored, "incident_changes": incidents}


def _parse_ts(v: str | None) -> datetime | None:
    if not v or v.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


@router.get("/alerts", dependencies=[ReadAccess])
def list_alerts(request: Request, limit: int = Query(50, le=500)):
    with P(request).db.session() as s:
        rows = s.scalars(select(AlertRecord).order_by(desc(AlertRecord.received_at)).limit(limit)).all()
        return [
            {
                "received_at": _iso(r.received_at),
                "alertname": r.alertname,
                "service": r.service,
                "severity": r.severity,
                "status": r.status,
                "source": r.source,
                "summary": r.summary,
            }
            for r in rows
        ]


# ------------------------------------------------------------------ chaos
CHAOS_ACTIONS = {"latency", "errors", "cpu", "memory", "disk", "unhealthy", "hang", "crash", "reset", "kill"}


@router.get("/chaos", dependencies=[ReadAccess])
async def chaos_overview(request: Request):
    p = P(request)
    out = []
    for svc in p.config.services:
        if not svc.chaos.enabled:
            continue
        state = None
        if svc.chaos.url:
            try:
                r = await p.client.get(f"{svc.chaos.url}/chaos", headers=_chaos_headers(p), timeout=2)
                state = r.json() if r.status_code == 200 else None
            except httpx.HTTPError:
                state = None
        out.append(
            {
                "service": svc.name,
                "label": svc.label,
                "http": bool(svc.chaos.url),
                "container": svc.container,
                "state": state,
            }
        )
    return out


def _chaos_headers(p) -> dict:
    return {"X-Chaos-Token": p.settings.chaos_token} if p.settings.chaos_token else {}


@router.post("/chaos/{name}/{action}", dependencies=[WriteAccess])
async def chaos(
    name: str, action: str, request: Request, body: dict[str, Any] = Body(default={}), who: str = Depends(operator)
):
    p = P(request)
    svc = _svc(request, name)
    if not svc.chaos.enabled:
        raise HTTPException(403, f"chaos is disabled for {name}")
    if action not in CHAOS_ACTIONS:
        raise HTTPException(400, f"unknown action; choose one of {sorted(CHAOS_ACTIONS)}")
    if action == "kill":
        if not svc.container:
            raise HTTPException(400, "service has no container")
        try:
            await p.runtime.kill(svc.container)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"service": name, "action": "kill", "by": who}
    if not svc.chaos.url:
        raise HTTPException(400, f"{name} has no chaos endpoint; only 'kill' is available")
    try:
        r = await p.client.post(f"{svc.chaos.url}/chaos/{action}", json=body, headers=_chaos_headers(p), timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"service unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return {"service": name, "action": action, "by": who, "result": r.json()}
