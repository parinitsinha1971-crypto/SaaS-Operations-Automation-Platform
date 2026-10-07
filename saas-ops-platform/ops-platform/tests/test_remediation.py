from datetime import timedelta

from conftest import make_config

from ops.db import utcnow
from ops.models import RemediationAction
from ops.remediation import RemediationEngine


def _engine(runtime, db, **remediation):
    cfg = make_config(
        remediation={"max_restarts": 3, "window_minutes": 15, "backoff_s": [0, 30, 120], "grace_s": 20, **remediation}
    )
    return RemediationEngine(runtime, db, cfg), cfg.service("saas-api"), cfg


async def test_restarts_on_down(runtime, db):
    eng, svc, _ = _engine(runtime, db)
    r = await eng.evaluate(svc, {"down"}, [], None)
    assert r.outcome == "restarted" and runtime.restarts == ["saas-api"]
    with db.session() as s:
        row = s.query(RemediationAction).one()
        assert row.status == "success" and row.initiated_by == "auto" and row.reason == "down"


async def test_ignores_triggers_not_in_policy(runtime, db):
    eng, svc, _ = _engine(runtime, db, restart_on=["exited"])
    assert await eng.evaluate(svc, {"down"}, [], None) is None
    assert runtime.restarts == []


async def test_grace_and_backoff(runtime, db):
    eng, svc, _ = _engine(runtime, db)
    t0 = utcnow()
    assert (await eng.evaluate(svc, {"down"}, [], None, now=t0)).outcome == "restarted"
    assert await eng.evaluate(svc, {"down"}, [], None, now=t0 + timedelta(seconds=10)) is None  # grace
    assert await eng.evaluate(svc, {"down"}, [], None, now=t0 + timedelta(seconds=25)) is None  # backoff 30s
    r = await eng.evaluate(svc, {"down"}, [], None, now=t0 + timedelta(seconds=31))
    assert r.outcome == "restarted" and len(runtime.restarts) == 2


async def test_circuit_breaker_suspends_then_resume(runtime, db):
    eng, svc, _ = _engine(runtime, db, backoff_s=[0], grace_s=0)
    t = utcnow()
    for i in range(3):
        r = await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(seconds=i))
        assert r.outcome == "restarted"
    r = await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(seconds=5))
    assert r.outcome == "suspended" and eng.is_suspended("saas-api")
    assert await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(seconds=6)) is None
    assert len(runtime.restarts) == 3
    eng.resume("saas-api")
    assert (await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(seconds=7))).outcome == "restarted"


async def test_circuit_closes_after_healthy_window(runtime, db):
    eng, svc, _ = _engine(runtime, db, backoff_s=[0], grace_s=0, max_restarts=1, window_minutes=5)
    t = utcnow()
    await eng.evaluate(svc, {"down"}, [], None, now=t)
    assert (await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(seconds=1))).outcome == "suspended"
    assert not eng.note_healthy(svc, now=t + timedelta(minutes=1))
    assert eng.note_healthy(svc, now=t + timedelta(minutes=6))
    assert not eng.is_suspended("saas-api")


async def test_window_expiry_frees_budget(runtime, db):
    eng, svc, _ = _engine(runtime, db, backoff_s=[0], grace_s=0, max_restarts=1, window_minutes=5)
    t = utcnow()
    await eng.evaluate(svc, {"down"}, [], None, now=t)
    r = await eng.evaluate(svc, {"down"}, [], None, now=t + timedelta(minutes=6))
    assert r.outcome == "restarted"


async def test_skips_when_dependency_down_once_per_streak(runtime, db):
    eng, svc, _ = _engine(runtime, db)
    r = await eng.evaluate(svc, {"down"}, ["saas-db"], None)
    assert r.outcome == "skipped" and "saas-db" in r.detail
    assert await eng.evaluate(svc, {"down"}, ["saas-db"], None) is None  # not recorded again
    assert runtime.restarts == []


async def test_failed_restart_is_recorded(runtime, db):
    runtime.fail_restart = True
    eng, svc, _ = _engine(runtime, db)
    r = await eng.evaluate(svc, {"exited"}, [], None)
    assert r.outcome == "failed" and "unreachable" in r.detail


async def test_manual_restart_does_not_consume_budget(runtime, db):
    eng, svc, _ = _engine(runtime, db, max_restarts=1, backoff_s=[0], grace_s=0)
    await eng.manual_restart(svc, "sahil", None)
    assert eng.status(svc)["attempts_in_window"] == 0
    assert (await eng.evaluate(svc, {"down"}, [], None)).outcome == "restarted"
