"""Generates grafana/dashboards/saas-ops-overview.json.

Kept as code so panels stay consistent and the JSON is always valid:
    python grafana/build_dashboard.py
"""

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "ops-postgres"}
SVC_FILTER = 'service=~"$service"'
# 0 healthy, 1 degraded, 2 down
STATE_EXPR = (
    f'sum by (service) (ops_service_state{{state="degraded",{SVC_FILTER}}})'
    f' + 2 * sum by (service) (ops_service_state{{state="down",{SVC_FILTER}}})'
)

_id = 0


def nid() -> int:
    global _id
    _id += 1
    return _id


def target(expr, legend="{{service}}", ref="A"):
    return {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def ts(title, exprs, x, y, w=8, h=8, unit="short", desc="", max_=None, thresholds=None, stack=False):
    if isinstance(exprs, str):
        exprs = [(exprs, "{{service}}")]
    defaults = {
        "unit": unit,
        "custom": {
            "lineWidth": 2,
            "fillOpacity": 8,
            "showPoints": "never",
            "spanNulls": False,
            "stacking": {"mode": "normal" if stack else "none"},
        },
    }
    if max_ is not None:
        defaults["max"] = max_
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["custom"]["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "id": nid(),
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(e, lg, chr(65 + i)) for i, (e, lg) in enumerate(exprs)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
    }


def stat(title, expr, x, y, w=4, h=4, unit="short", legend="{{service}}", steps=None, mappings=None, desc=""):
    return {
        "id": nid(),
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr, legend)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "mappings": mappings or [],
                "thresholds": {"mode": "absolute", "steps": steps or [{"color": "green", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value_and_name",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
        },
    }


def row(title, y):
    return {
        "id": nid(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def table_sql(title, sql, x, y, w=12, h=9):
    return {
        "id": nid(),
        "type": "table",
        "title": title,
        "datasource": PG,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"datasource": PG, "refId": "A", "format": "table", "rawQuery": True, "editorMode": "code", "rawSql": sql}
        ],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "severity"},
                    "properties": [
                        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                        {
                            "id": "mappings",
                            "value": [
                                {
                                    "type": "value",
                                    "options": {
                                        "critical": {"color": "red", "index": 0},
                                        "warning": {"color": "orange", "index": 1},
                                    },
                                }
                            ],
                        },
                    ],
                },
                {"matcher": {"id": "byName", "options": "duration"}, "properties": [{"id": "unit", "value": "s"}]},
            ],
        },
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


STATE_MAP = [
    {
        "type": "value",
        "options": {
            "0": {"text": "healthy", "color": "green", "index": 0},
            "1": {"text": "degraded", "color": "orange", "index": 1},
            "2": {"text": "down", "color": "red", "index": 2},
        },
    }
]

panels = []
y = 0
panels.append(row("Right now", y))
y += 1
panels.append(
    stat(
        "Service state",
        STATE_EXPR,
        0,
        y,
        w=8,
        h=5,
        mappings=STATE_MAP,
        steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 2}],
        desc="Debounced state from the ops platform's checks.",
    )
)
panels.append(
    stat(
        "Open incidents",
        "sum(ops_incidents_open)",
        8,
        y,
        w=4,
        h=5,
        legend="open",
        steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 3}],
    )
)
panels.append(
    stat(
        "Auto-restarts, 24h",
        'sum(increase(ops_restarts_total{initiated_by="auto"}[24h]))',
        12,
        y,
        w=4,
        h=5,
        legend="restarts",
        steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}, {"color": "red", "value": 5}],
    )
)
panels.append(
    stat(
        "30-day error budget left",
        f"ops_error_budget_remaining_ratio{{{SVC_FILTER}}}",
        16,
        y,
        w=8,
        h=5,
        unit="percentunit",
        steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0}, {"color": "green", "value": 0.25}],
        desc="1 - bad cycles / allowed bad cycles over 30 days. Negative means overspent.",
    )
)
y += 5
panels.append(
    {
        "id": nid(),
        "type": "state-timeline",
        "title": "State history",
        "datasource": PROM,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 6},
        "targets": [target(STATE_EXPR)],
        "fieldConfig": {
            "defaults": {
                "mappings": STATE_MAP,
                "custom": {"fillOpacity": 85, "lineWidth": 0},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "orange", "value": 1},
                        {"color": "red", "value": 2},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}},
    }
)
y += 6

panels.append(row("Checks and API", y))
y += 1
panels.append(ts("Health check latency", f'ops_check_latency_seconds{{check="health",{SVC_FILTER}}}', 0, y, unit="s"))
panels.append(
    ts(
        "Requests per second",
        f"service:http_requests:rate5m{{{SVC_FILTER}}}",
        8,
        y,
        unit="reqps",
        desc="Scraped directly from each service.",
    )
)
panels.append(
    ts(
        "5xx error ratio",
        f"service:http_errors:ratio5m{{{SVC_FILTER}}}",
        16,
        y,
        unit="percentunit",
        thresholds=[
            {"color": "green", "value": None},
            {"color": "orange", "value": 0.05},
            {"color": "red", "value": 0.25},
        ],
    )
)
y += 8
panels.append(
    ts(
        "p95 latency (service metrics)",
        f"service:http_latency:p95_5m{{{SVC_FILTER}}}",
        0,
        y,
        unit="s",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.5}],
    )
)
panels.append(
    ts(
        "Check success",
        f"min by (service) (ops_check_success{{{SVC_FILTER}}})",
        8,
        y,
        unit="bool",
        max_=1,
        desc="1 when every check (critical or not) passed.",
    )
)
panels.append(
    ts(
        "Anomaly z-score",
        [(f"ops_anomaly_zscore{{{SVC_FILTER}}}", "{{service}} {{metric}}")],
        16,
        y,
        desc="EWMA z-score per metric; anomalies fire above the configured threshold (3.5).",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 3.5}],
    )
)
y += 8

panels.append(row("Resources", y))
y += 1
panels.append(
    ts(
        "CPU (100% = one core)",
        f"ops_container_cpu_percent{{{SVC_FILTER}}}",
        0,
        y,
        unit="percent",
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 95}],
    )
)
panels.append(
    ts(
        "Memory (% of limit)",
        f"ops_container_memory_percent{{{SVC_FILTER}}}",
        8,
        y,
        unit="percent",
        max_=100,
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 92}],
    )
)
panels.append(
    ts(
        "Disk (% of quota)",
        [(f"ops_disk_used_percent{{{SVC_FILTER}}}", "{{service}}"), ("ops_host_disk_used_percent", "host {{path}}")],
        16,
        y,
        unit="percent",
        max_=100,
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 90}],
    )
)
y += 8

panels.append(row("Logs and automation", y))
y += 1
panels.append(
    ts(
        "Error log lines per minute",
        f'sum by (service) (rate(ops_log_lines_total{{level=~"ERROR|CRITICAL|FATAL|PANIC",{SVC_FILTER}}}[2m])) * 60',
        0,
        y,
    )
)
panels.append(
    ts(
        "Restarts",
        [(f"sum by (service, result) (increase(ops_restarts_total{{{SVC_FILTER}}}[5m]))", "{{service}} {{result}}")],
        8,
        y,
        stack=True,
    )
)
panels.append(
    ts(
        "Alerts pushed to Alertmanager",
        [(f"sum by (service, status) (increase(ops_alerts_sent_total{{{SVC_FILTER}}}[5m]))", "{{service}} {{status}}")],
        16,
        y,
        stack=True,
    )
)
y += 8
panels.append(
    table_sql(
        "Recent incidents",
        'SELECT opened_at AS "opened (UTC)", service, severity, title, status,\n'
        "  EXTRACT(EPOCH FROM (COALESCE(resolved_at, now() AT TIME ZONE 'utc') - opened_at))::int AS duration,\n"
        "  restarts\nFROM incidents\nWHERE $__timeFilter(opened_at)\nORDER BY opened_at DESC\nLIMIT 50",
        0,
        y,
        w=14,
    )
)
panels.append(
    table_sql(
        "Restarts",
        'SELECT started_at AS "time (UTC)", service, reason, initiated_by AS "by", status, round(duration_ms) AS ms,'
        " error\nFROM remediation_actions\nWHERE $__timeFilter(started_at)\nORDER BY started_at DESC\nLIMIT 50",
        14,
        y,
        w=10,
    )
)

dashboard = {
    "uid": "saas-ops-overview",
    "title": "SaaS Ops Overview",
    "tags": ["saas-ops"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "graphTooltip": 1,
    "templating": {
        "list": [
            {
                "name": "service",
                "label": "Service",
                "type": "query",
                "datasource": PROM,
                "query": {"query": "label_values(ops_service_up, service)", "refId": "service"},
                "definition": "label_values(ops_service_up, service)",
                "includeAll": True,
                "multi": True,
                "allValue": ".*",
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
                "refresh": 2,
            }
        ]
    },
    "links": [{"title": "Ops console", "url": "http://localhost:8080", "type": "link", "targetBlank": True}],
    "annotations": {
        "list": [
            {
                "name": "Restarts",
                "datasource": PG,
                "enable": True,
                "iconColor": "blue",
                "target": {
                    "rawSql": "SELECT started_at AS time, service || ' restarted (' || reason || ')' AS text "
                    "FROM remediation_actions WHERE status = 'success' AND $__timeFilter(started_at)",
                    "format": "table",
                    "refId": "Anno",
                },
            }
        ]
    },
    "panels": panels,
}

out = Path(__file__).parent / "dashboards" / "saas-ops-overview.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(panels)} panels)")
