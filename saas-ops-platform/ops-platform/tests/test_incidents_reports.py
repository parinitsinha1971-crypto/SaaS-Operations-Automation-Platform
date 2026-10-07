from datetime import timedelta

import pytest
from conftest import make_config

from ops.db import utcnow
from ops.incidents import Condition, IncidentManager
from ops.models import Incident, LogSignature, RemediationAction, ServiceSample
from ops.reports import build_daily_report, error_budget, render_markdown, save_report


def C(key="availability", sev="critical", kind="availability"):
    return Condition(key, kind, sev, f"{key} {sev}", "detail")


def test_open_dedupe_escalate_resolve(db):
    mgr = IncidentManager(db)
    ch = mgr.sync("saas-api", [C(sev="warning")])
    assert [c.change for c in ch] == ["opened"]
    assert mgr.sync("saas-api", [C(sev="warning")]) == []  # deduplicated
    ch = mgr.sync("saas-api", [C(sev="critical")])
    assert [c.change for c in ch] == ["escalated"]
    ch = mgr.sync("saas-api", [])
    assert [c.change for c in ch] == ["resolved"]
    with db.session() as s:
        inc = s.query(Incident).one()
        assert inc.status == "resolved" and inc.severity == "critical"
        assert [e.type for e in inc.events] == ["detected", "escalated", "resolved"]


def test_incidents_are_per_service_and_key(db):
    mgr = IncidentManager(db)
    mgr.sync("saas-api", [C(), C("resource:cpu", "warning", "resource")])
    mgr.sync("saas-web", [C()])
    assert sum(mgr.open_counts().values()) == 3
    mgr.sync("saas-api", [C()])  # cpu recovered
    assert mgr.open_counts()[("saas-api", "critical")] == 1
    assert ("saas-api", "warning") not in mgr.open_counts()


def test_external_alerts_are_not_resolved_by_sync(db):
    mgr = IncidentManager(db)
    assert mgr.upsert_external("saas-api", "HighLatencyP95", "warning", "firing", "p95 > 500ms").change == "opened"
    mgr.sync("saas-api", [])
    assert mgr.open_counts()[("saas-api", "warning")] == 1
    assert mgr.upsert_external("saas-api", "HighLatencyP95", "warning", "resolved", "").change == "resolved"


def test_operator_actions(db):
    mgr = IncidentManager(db)
    iid = mgr.sync("saas-api", [C()])[0].incident_id
    mgr.acknowledge(iid, "sahil")
    mgr.note(iid, "sahil", "looking at it")
    mgr.resolve(iid, "sahil", "fixed config")
    with db.session() as s:
        inc = s.get(Incident, iid)
        assert inc.acknowledged_by == "sahil" and inc.status == "resolved"
        assert [e.type for e in inc.events][-3:] == ["acknowledged", "note", "resolved"]


@pytest.mark.parametrize(
    "total,up,target,remaining",
    [
        (10_000, 10_000, 99.5, 1.0),  # perfect: whole budget left
        (10_000, 9_975, 99.5, 0.5),  # used half of 50 allowed bad cycles
        (10_000, 9_950, 99.5, 0.0),  # exactly spent
        (10_000, 9_900, 99.5, -1.0),  # overspent by 100%
    ],
)
def test_error_budget(total, up, target, remaining):
    assert error_budget(total, up, target)["remaining"] == pytest.approx(remaining)


def test_error_budget_no_data():
    assert error_budget(0, 0, 99.9)["availability"] is None


def test_daily_report(db):
    cfg = make_config()
    day = (utcnow() - timedelta(days=1)).date()
    start = utcnow().replace(year=day.year, month=day.month, day=day.day, hour=10, minute=0, second=0, microsecond=0)
    with db.session() as s:
        for i in range(100):
            s.add(
                ServiceSample(
                    service="saas-api",
                    ts=start + timedelta(seconds=10 * i),
                    state="healthy",
                    raw_state="healthy",
                    up=i >= 5,
                    health_latency_ms=20 + i,
                    cpu_percent=10 + i / 10,
                )
            )
            s.add(
                ServiceSample(
                    service="saas-web",
                    ts=start + timedelta(seconds=10 * i),
                    state="healthy",
                    raw_state="healthy",
                    up=True,
                    health_latency_ms=15,
                )
            )
        s.add(
            Incident(
                service="saas-api",
                key="availability",
                kind="availability",
                severity="critical",
                title="API is down",
                status="resolved",
                opened_at=start,
                resolved_at=start + timedelta(seconds=50),
            )
        )
        s.add(
            RemediationAction(
                service="saas-api",
                action="restart",
                reason="down",
                initiated_by="auto",
                status="success",
                started_at=start + timedelta(seconds=30),
            )
        )
        s.add(
            LogSignature(
                service="saas-api",
                day=day,
                fingerprint="abc",
                template="database timeout after <*>",
                sample="database timeout after 3000ms",
                level="ERROR",
                count=77,
            )
        )

    r = build_daily_report(db, cfg, day)
    api = next(x for x in r["services"] if x["service"] == "saas-api")
    web = next(x for x in r["services"] if x["service"] == "saas-web")
    assert api["availability"] == pytest.approx(0.95)
    assert not api["slo_met"] and web["slo_met"]
    assert api["downtime_s"] == 5  # 5 cycles x interval 1s in the test config
    assert api["mttr_s"] == 50
    assert api["restarts"]["success"] == 1
    assert api["incidents"]["critical"] == 1
    assert api["top_errors"][0]["count"] == 77
    assert any("missed" in h for h in r["highlights"])
    md = render_markdown(r)
    assert "| API |" in md and "database timeout" in md
    save_report(db, r)
    save_report(db, r)  # idempotent upsert
