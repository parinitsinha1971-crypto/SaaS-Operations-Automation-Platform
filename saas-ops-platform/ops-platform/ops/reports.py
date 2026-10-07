"""Daily operations report and SLO / error-budget math.

Availability is measured from monitor cycles: a cycle counts as "up" when
every critical check passed. With a fixed check interval this is a good
time-weighted approximation and needs no extra bookkeeping.

Error budget (30 days rolling):
    allowed_bad = (1 - target) * cycles
    remaining   = 1 - bad_cycles / allowed_bad      (negative = overspent)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from statistics import mean

import httpx
from sqlalchemy import case, func, select

from .config import PlatformConfig
from .db import Database, utcnow
from .metrics import ERROR_BUDGET_REMAINING, SLO_AVAILABILITY, SLO_TARGET
from .models import AlertRecord, Incident, LogSignature, RemediationAction, Report, ServiceSample

log = logging.getLogger("ops.reports")


def _p95(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))], 1)


def _avail(db: Database, service: str, start: datetime, end: datetime) -> tuple[int, int]:
    with db.session() as s:
        total, up = s.execute(
            select(func.count(), func.coalesce(func.sum(case((ServiceSample.up.is_(True), 1), else_=0)), 0)).where(
                ServiceSample.service == service, ServiceSample.ts >= start, ServiceSample.ts < end
            )
        ).one()
    return int(total), int(up)


def error_budget(total: int, up: int, target_pct: float) -> dict:
    target = target_pct / 100
    if total == 0:
        return {"availability": None, "remaining": None, "bad_cycles": 0, "allowed_bad_cycles": 0}
    bad = total - up
    allowed = (1 - target) * total
    remaining = 1 - bad / allowed if allowed > 0 else (1.0 if bad == 0 else -1.0)
    return {
        "availability": up / total,
        "remaining": round(remaining, 4),
        "bad_cycles": bad,
        "allowed_bad_cycles": round(allowed, 1),
    }


def update_slo_gauges(db: Database, config: PlatformConfig) -> dict[str, dict]:
    now = utcnow()
    out = {}
    for svc in config.services:
        t24, u24 = _avail(db, svc.name, now - timedelta(hours=24), now)
        t30, u30 = _avail(db, svc.name, now - timedelta(days=30), now)
        budget = error_budget(t30, u30, svc.slo.availability)
        SLO_TARGET.labels(svc.name).set(svc.slo.availability / 100)
        if t24:
            SLO_AVAILABILITY.labels(svc.name, "24h").set(u24 / t24)
        if t30:
            SLO_AVAILABILITY.labels(svc.name, "30d").set(u30 / t30)
            ERROR_BUDGET_REMAINING.labels(svc.name).set(budget["remaining"])
        out[svc.name] = {
            "availability_24h": u24 / t24 if t24 else None,
            "availability_30d": u30 / t30 if t30 else None,
            "budget_remaining": budget["remaining"],
            "target": svc.slo.availability,
        }
    return out


def build_daily_report(db: Database, config: PlatformConfig, day: date) -> dict:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    interval = config.defaults.interval_s
    services = []

    with db.session() as s:
        for svc in config.services:
            rows = s.execute(
                select(
                    ServiceSample.up,
                    ServiceSample.health_latency_ms,
                    ServiceSample.cpu_percent,
                    ServiceSample.memory_percent,
                    ServiceSample.disk_percent,
                    ServiceSample.api_p95_ms,
                    ServiceSample.api_error_ratio,
                ).where(ServiceSample.service == svc.name, ServiceSample.ts >= start, ServiceSample.ts < end)
            ).all()
            total = len(rows)
            up = sum(1 for r in rows if r.up)
            lat = [r.health_latency_ms for r in rows if r.health_latency_ms is not None]

            t30, u30 = _avail(db, svc.name, end - timedelta(days=30), end)
            budget = error_budget(t30, u30, svc.slo.availability)

            incidents = s.scalars(
                select(Incident).where(
                    Incident.service == svc.name, Incident.opened_at >= start, Incident.opened_at < end
                )
            ).all()
            resolved = s.scalars(
                select(Incident).where(
                    Incident.service == svc.name, Incident.resolved_at >= start, Incident.resolved_at < end
                )
            ).all()
            ttr = [(i.resolved_at - i.opened_at).total_seconds() for i in resolved]

            actions = s.execute(
                select(RemediationAction.status, RemediationAction.initiated_by, func.count())
                .where(
                    RemediationAction.service == svc.name,
                    RemediationAction.started_at >= start,
                    RemediationAction.started_at < end,
                )
                .group_by(RemediationAction.status, RemediationAction.initiated_by)
            ).all()
            restarts = {"success": 0, "failed": 0, "skipped": 0, "manual": 0}
            for status, who, n in actions:
                restarts[status] = restarts.get(status, 0) + n
                if who != "auto":
                    restarts["manual"] += n

            sigs = s.scalars(
                select(LogSignature)
                .where(LogSignature.service == svc.name, LogSignature.day == day)
                .order_by(LogSignature.count.desc())
                .limit(5)
            ).all()

            availability = up / total if total else None
            latency_ok = None
            if svc.slo.latency_ms and lat:
                latency_ok = sum(1 for v in lat if v <= svc.slo.latency_ms) / len(lat)

            services.append(
                {
                    "service": svc.name,
                    "label": svc.label,
                    "kind": svc.kind,
                    "samples": total,
                    "availability": availability,
                    "slo_target": svc.slo.availability,
                    "slo_met": availability is not None and availability * 100 >= svc.slo.availability,
                    "downtime_s": round((total - up) * interval),
                    "availability_30d": budget["availability"],
                    "error_budget_remaining": budget["remaining"],
                    "latency_slo_ms": svc.slo.latency_ms,
                    "latency_compliance": latency_ok,
                    "health_latency_avg_ms": round(mean(lat), 1) if lat else None,
                    "health_latency_p95_ms": _p95(lat),
                    "api_p95_ms_peak": max((r.api_p95_ms for r in rows if r.api_p95_ms is not None), default=None),
                    "cpu_peak": max((r.cpu_percent for r in rows if r.cpu_percent is not None), default=None),
                    "memory_peak_pct": max(
                        (r.memory_percent for r in rows if r.memory_percent is not None), default=None
                    ),
                    "disk_peak_pct": max((r.disk_percent for r in rows if r.disk_percent is not None), default=None),
                    "incidents": {
                        "total": len(incidents),
                        "critical": sum(1 for i in incidents if i.severity == "critical"),
                        "warning": sum(1 for i in incidents if i.severity == "warning"),
                        "list": [
                            {
                                "id": i.id,
                                "title": i.title,
                                "severity": i.severity,
                                "status": i.status,
                                "opened_at": i.opened_at.isoformat() + "Z",
                                "duration_s": round(i.duration_s),
                            }
                            for i in incidents
                        ],
                    },
                    "mttr_s": round(mean(ttr)) if ttr else None,
                    "restarts": restarts,
                    "top_errors": [{"template": g.template, "sample": g.sample, "count": g.count} for g in sigs],
                }
            )

        alerts_received = (
            s.scalar(
                select(func.count())
                .select_from(AlertRecord)
                .where(AlertRecord.received_at >= start, AlertRecord.received_at < end)
            )
            or 0
        )
        still_open = s.scalar(select(func.count()).select_from(Incident).where(Incident.status == "open")) or 0

    measured = [x for x in services if x["availability"] is not None]
    fleet = (sum(x["availability"] for x in measured) / len(measured)) if measured else None
    return {
        "day": day.isoformat(),
        "generated_at": utcnow().isoformat() + "Z",
        "fleet_availability": fleet,
        "incidents_total": sum(x["incidents"]["total"] for x in services),
        "incidents_open_now": still_open,
        "restarts_total": sum(x["restarts"]["success"] + x["restarts"]["failed"] for x in services),
        "alerts_received": alerts_received,
        "highlights": _highlights(services),
        "services": services,
    }


def _pct(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _dur(s: float | None) -> str:
    if s is None:
        return "n/a"
    s = int(s)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"


def _highlights(services: list[dict]) -> list[str]:
    out = []
    for x in services:
        if x["availability"] is None:
            out.append(f"{x['label']}: no monitoring data for this day.")
            continue
        if not x["slo_met"]:
            out.append(
                f"{x['label']} missed its {x['slo_target']}% availability SLO "
                f"({_pct(x['availability'])}, {_dur(x['downtime_s'])} down)."
            )
        rem = x["error_budget_remaining"]
        if rem is not None and rem < 0:
            out.append(f"{x['label']} has overspent its 30-day error budget ({rem * 100:.0f}% remaining).")
        elif rem is not None and rem < 0.25:
            out.append(
                f"{x['label']} has {rem * 100:.0f}% of its 30-day error budget left; consider freezing risky changes."
            )
        if x["restarts"]["success"] >= 3:
            out.append(f"{x['label']} was auto-restarted {x['restarts']['success']} times; check for a crash loop.")
        if x["top_errors"] and x["top_errors"][0]["count"] >= 50:
            e = x["top_errors"][0]
            out.append(f"{x['label']}'s top error occurred {e['count']} times: “{e['template'][:90]}”.")
        for name, peak in (("CPU", x["cpu_peak"]), ("memory", x["memory_peak_pct"]), ("disk", x["disk_peak_pct"])):
            # CPU is per-core (200% = two cores), so only memory and disk have a hard ceiling at 100%
            limit = 150 if name == "CPU" else 90
            if peak is not None and peak >= limit:
                out.append(f"{x['label']}'s {name} peaked at {peak:.0f}%.")

    slo_misses = sum(1 for x in services if x["availability"] is not None and not x["slo_met"])
    total = sum(x["incidents"]["total"] for x in services)
    critical = sum(x["incidents"]["critical"] for x in services)
    if not slo_misses and any(x["availability"] is not None for x in services):
        summary = "All services met their availability SLOs"
        summary += (
            f", with {total} incident{'s' if total != 1 else ''} ({critical} critical)."
            if total
            else " with no incidents."
        )
        out.insert(0, summary)
    return out


def render_markdown(r: dict) -> str:
    lines = [
        f"# Daily operations report — {r['day']}",
        "",
        f"Fleet availability **{_pct(r['fleet_availability'])}**, incidents **{r['incidents_total']}** "
        f"({r['incidents_open_now']} open now), restarts **{r['restarts_total']}**, "
        f"alerts received **{r['alerts_received']}**",
        "",
        "## Highlights",
        *[f"- {h}" for h in r["highlights"]],
        "",
        "## Services",
        "",
        "| Service | Availability | SLO | 30d budget left | Incidents | MTTR | Restarts | p95 health latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for x in r["services"]:
        budget = "n/a" if x["error_budget_remaining"] is None else f"{x['error_budget_remaining'] * 100:.0f}%"
        lines.append(
            f"| {x['label']} | {_pct(x['availability'])} "
            f"| {x['slo_target']}% {'met' if x['slo_met'] else 'MISSED'} | {budget} "
            f"| {x['incidents']['total']} | {_dur(x['mttr_s'])} | {x['restarts']['success']} "
            f"| {x['health_latency_p95_ms'] if x['health_latency_p95_ms'] is not None else 'n/a'} ms |"
        )
    for x in r["services"]:
        if x["top_errors"]:
            lines += ["", f"### Top errors — {x['label']}"]
            lines += [f"- `{e['template'][:120]}` × {e['count']}" for e in x["top_errors"]]
    return "\n".join(lines) + "\n"


def save_report(db: Database, data: dict) -> Report:
    day = date.fromisoformat(data["day"])
    md = render_markdown(data)
    with db.session() as s:
        row = s.scalar(select(Report).where(Report.day == day))
        if row is None:
            row = Report(day=day, data=data, markdown=md, generated_at=utcnow())
            s.add(row)
        else:
            row.data, row.markdown, row.generated_at = data, md, utcnow()
        s.flush()
        return row


async def deliver_report(webhook_url: str, data: dict, public_url: str, client: httpx.AsyncClient) -> None:
    """Optional: post a short summary to a Slack/Discord-compatible webhook."""
    if not webhook_url:
        return
    text = (
        f"*Daily ops report {data['day']}* — fleet availability {_pct(data['fleet_availability'])}, "
        f"{data['incidents_total']} incidents, {data['restarts_total']} restarts.\n"
        + "\n".join(f"• {h}" for h in data["highlights"])
        + f"\n{public_url.rstrip('/')}/reports/{data['day']}"
    )
    try:
        await client.post(webhook_url, json={"text": text, "content": text}, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("report webhook failed: %s", exc)
