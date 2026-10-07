"""End-to-end through the real app: monitor cycles, incidents, alerts, restarts, API, UI."""

import json

import pytest
from conftest import FakeRuntime, db_url, make_config
from fastapi.testclient import TestClient

from ops.main import create_app
from ops.runtime import ContainerStats
from ops.settings import Settings

KEY = "test-key"


@pytest.fixture
def app_env(tmp_path, fleet):
    runtime = FakeRuntime(fleet)
    settings = Settings(
        database_url=db_url(tmp_path),
        api_key=KEY,
        webhook_token="hook",
        alertmanager_url="http://alertmanager:9093",
        runtime="none",
    )
    app = create_app(
        settings=settings, config=make_config(), runtime=runtime, start_background=False, http_transport=fleet.transport
    )
    with TestClient(app) as client:
        yield client, app.state.platform, runtime, fleet


def cycle(client, platform, n=1):
    for _ in range(n):
        client.portal.call(platform.monitor.cycle)


def test_outage_detected_alerted_restarted_and_resolved(app_env):
    client, platform, runtime, fleet = app_env
    cycle(client, platform, 2)
    assert platform.monitor.status["saas-api"].state == "healthy"

    fleet.restart_fixes = True
    fleet.healthy["saas-api"] = False
    cycle(client, platform, 2)
    assert platform.monitor.status["saas-api"].state == "healthy"  # debounced, not down yet
    cycle(client, platform, 1)  # third failure -> down -> incident -> restart

    assert runtime.restarts == ["saas-api"]
    incs = client.get("/api/incidents?status=all").json()
    api_inc = next(i for i in incs if i["service"] == "saas-api" and i["key"] == "availability")
    assert api_inc["severity"] == "critical"
    # web depends on api: it shows degraded reasons but is not restarted
    assert "saas-web" not in runtime.restarts

    alert = next(a for a in fleet.alerts if a["labels"]["service"] == "saas-api")
    assert alert["labels"]["severity"] == "critical" and alert["labels"]["source"] == "ops-platform"
    assert alert["generatorURL"].endswith(f"/incidents/{api_inc['id']}")

    cycle(client, platform, 3)  # restart fixed it; recovery needs 2 healthy cycles
    detail = client.get(f"/api/incidents/{api_inc['id']}").json()
    assert detail["status"] == "resolved"
    types = [e["type"] for e in detail["events"]]
    assert types[0] == "detected"
    assert {"restart", "alert", "resolved"} <= set(types)
    assert detail["restarts"] == 1
    assert any("endsAt" in a for a in fleet.alerts)  # resolution pushed to Alertmanager


def test_restart_loop_opens_circuit_and_escalates(app_env):
    client, platform, runtime, fleet = app_env
    fleet.restart_fixes = False
    fleet.healthy["saas-api"] = False
    cycle(client, platform, 12)
    assert len(runtime.restarts) == 2  # max_restarts in test config
    assert platform.monitor.remediation.is_suspended("saas-api")
    open_keys = {i["key"] for i in client.get("/api/incidents?status=open").json() if i["service"] == "saas-api"}
    assert {"availability", "remediation"} <= open_keys

    r = client.post("/api/services/saas-api/remediation/resume", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and not platform.monitor.remediation.is_suspended("saas-api")


def test_resource_and_log_conditions(app_env):
    client, platform, runtime, fleet = app_env
    runtime.stats_by_container["saas-api"] = ContainerStats(cpu_percent=97, memory_bytes=95, memory_limit_bytes=100)
    for _ in range(3):
        runtime.logs["saas-api"] = [json.dumps({"level": "CRITICAL", "msg": "chaos: fatal error, process exiting"})]
        cycle(client, platform)
    keys = {i["key"] for i in client.get("/api/incidents?status=open").json()}
    assert {"resource:cpu", "resource:memory", "logs:fatal"} <= keys
    assert "saas-api" in runtime.restarts  # memory critical triggers a restart
    action = client.get("/api/remediation").json()[0]
    assert action["reason"] == "memory_critical" and action["incident_id"] is not None
    mem_inc = client.get(f"/api/incidents/{action['incident_id']}").json()
    assert mem_inc["key"] == "resource:memory" and mem_inc["restarts"] >= 1
    status = client.get("/api/services/saas-api").json()
    assert status["status"]["resources"]["memory_percent"] == 95
    assert status["log_signatures"][0]["template"].startswith("chaos: fatal error")


def test_exited_container_restarts_immediately(app_env):
    client, platform, runtime, fleet = app_env
    cycle(client, platform, 2)
    runtime.state["saas-api"] = "exited"
    fleet.healthy["saas-api"] = False
    cycle(client, platform, 1)
    assert runtime.restarts == ["saas-api"]  # no waiting for the failure threshold
    inc = client.get("/api/incidents?service=saas-api").json()[0]
    assert "crashed" in inc["title"] and inc["severity"] == "critical"
    cycle(client, platform, 1)  # back up: crash incident resolves
    detail = client.get(f"/api/incidents/{inc['id']}").json()
    assert detail["status"] == "resolved" and detail["restarts"] == 1


def test_write_endpoints_need_key(app_env):
    client, platform, runtime, fleet = app_env
    assert client.post("/api/services/saas-api/restart").status_code == 401
    assert client.post("/api/services/saas-api/restart", headers={"X-API-Key": "wrong"}).status_code == 401
    r = client.post("/api/services/saas-api/restart", headers={"Authorization": f"Bearer {KEY}", "X-Operator": "sahil"})
    assert r.status_code == 200 and runtime.restarts == ["saas-api"]
    actions = client.get("/api/remediation").json()
    assert actions[0]["initiated_by"] == "sahil"
    assert client.get("/api/services/nope").status_code == 404


def test_chaos_proxy(app_env):
    client, platform, runtime, fleet = app_env
    h = {"X-API-Key": KEY}
    r = client.post("/api/chaos/saas-api/latency", json={"ms": 500, "seconds": 30}, headers=h)
    assert r.status_code == 200
    assert fleet.chaos_calls[-1] == ("saas-api", "/chaos/latency", {"ms": 500, "seconds": 30})
    assert client.post("/api/chaos/saas-api/kill", headers=h).status_code == 200 and runtime.kills == ["saas-api"]
    assert client.post("/api/chaos/saas-web/crash", headers=h).status_code == 403  # chaos disabled
    assert client.post("/api/chaos/saas-api/rm-rf", headers=h).status_code == 400


def test_alertmanager_webhook(app_env):
    client, platform, runtime, fleet = app_env
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighLatencyP95", "service": "saas-api", "severity": "warning"},
                "annotations": {"summary": "p95 above 500ms"},
                "startsAt": "2026-10-07T10:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "abc",
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "OpsAvailability",
                    "service": "saas-api",
                    "severity": "critical",
                    "source": "ops-platform",
                },
                "annotations": {"summary": "API is down"},
            },
        ],
    }
    assert client.post("/api/alerts/webhook", json=payload).status_code == 401
    r = client.post("/api/alerts/webhook", json=payload, headers={"Authorization": "Bearer hook"})
    assert r.json() == {"stored": 2, "incident_changes": 1}  # our own alert isn't duplicated
    assert len(client.get("/api/alerts").json()) == 2
    incs = client.get("/api/incidents?status=open").json()
    assert incs[0]["kind"] == "prometheus" and "HighLatencyP95" in incs[0]["title"]


def test_reports_and_pages_render(app_env):
    client, platform, runtime, fleet = app_env
    cycle(client, platform, 3)
    r = client.post("/api/reports/generate", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    day = r.json()["day"]
    assert client.get(f"/api/reports/{day}").json()["services"][0]["samples"] == 3
    assert "Daily operations report" in client.get(f"/api/reports/{day}/markdown").text
    for path in [
        "/",
        "/services/saas-api",
        "/incidents",
        "/reports",
        f"/reports/{day}",
        "/chaos",
        "/healthz",
        "/readyz",
        "/metrics",
        "/api/overview",
        "/api/chaos",
        "/docs",
    ]:
        assert client.get(path).status_code == 200, path
    metrics = client.get("/metrics").text
    assert 'ops_service_up{service="saas-api"} 1.0' in metrics
    assert "ops_monitor_cycle_seconds_count" in metrics


def test_restore_state_prevents_flapping(app_env, tmp_path):
    client, platform, runtime, fleet = app_env
    fleet.healthy["saas-api"] = False
    fleet.restart_fixes = False
    cycle(client, platform, 3)
    assert platform.monitor.status["saas-api"].state == "down"
    # a fresh monitor (platform restart) seeds state from the DB instead of starting at "unknown"
    from ops.monitor import Monitor

    m2 = Monitor(platform.config, platform.db, runtime, platform.alerter, platform.client)
    m2.restore_state()
    assert m2.trackers["saas-api"].state == "down"
