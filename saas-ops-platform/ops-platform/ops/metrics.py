"""Prometheus metrics exported by the platform on /metrics.

These are what Prometheus scrapes, what the alert rules evaluate and what
the Grafana dashboard plots.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .health import DEGRADED, DOWN, HEALTHY, UNKNOWN

REGISTRY = CollectorRegistry(auto_describe=True)

SERVICE_UP = Gauge("ops_service_up", "1 if every critical check passed this cycle", ["service"], registry=REGISTRY)
SERVICE_STATE = Gauge("ops_service_state", "Debounced service state (one-hot)", ["service", "state"], registry=REGISTRY)
CHECK_SUCCESS = Gauge("ops_check_success", "Check passed", ["service", "check", "type"], registry=REGISTRY)
CHECK_LATENCY = Gauge("ops_check_latency_seconds", "Check latency", ["service", "check", "type"], registry=REGISTRY)

CPU = Gauge("ops_container_cpu_percent", "Container CPU percent (100 = one core)", ["service"], registry=REGISTRY)
MEM = Gauge("ops_container_memory_bytes", "Container memory working set", ["service"], registry=REGISTRY)
MEM_PCT = Gauge("ops_container_memory_percent", "Memory as percent of limit", ["service"], registry=REGISTRY)
DISK_PCT = Gauge("ops_disk_used_percent", "Service data disk used vs quota", ["service"], registry=REGISTRY)
HOST_DISK_PCT = Gauge("ops_host_disk_used_percent", "Host filesystem used percent", ["path"], registry=REGISTRY)

LOG_LINES = Counter("ops_log_lines_total", "Log lines analyzed", ["service", "level"], registry=REGISTRY)
LOG_RULE_HITS = Counter("ops_log_rule_hits_total", "Log rule matches", ["service", "rule"], registry=REGISTRY)
API_ERROR_RATIO = Gauge(
    "ops_api_error_ratio", "5xx ratio from access logs (last cycle)", ["service"], registry=REGISTRY
)
API_P95 = Gauge(
    "ops_api_latency_p95_seconds", "p95 latency from access logs (last cycle)", ["service"], registry=REGISTRY
)
ANOMALY_Z = Gauge("ops_anomaly_zscore", "EWMA z-score", ["service", "metric"], registry=REGISTRY)
ANOMALY_ACTIVE = Gauge("ops_anomaly_active", "1 while an anomaly is active", ["service", "metric"], registry=REGISTRY)

INCIDENTS_OPEN = Gauge("ops_incidents_open", "Open incidents", ["service", "severity"], registry=REGISTRY)
INCIDENTS_TOTAL = Counter("ops_incidents_total", "Incidents opened", ["service", "kind", "severity"], registry=REGISTRY)
RESTARTS = Counter("ops_restarts_total", "Restart attempts", ["service", "result", "initiated_by"], registry=REGISTRY)
REMEDIATION_SUSPENDED = Gauge(
    "ops_remediation_suspended", "1 when auto-restart is suspended (circuit open)", ["service"], registry=REGISTRY
)
ALERTS_SENT = Counter(
    "ops_alerts_sent_total", "Alerts pushed to Alertmanager", ["service", "status"], registry=REGISTRY
)

SLO_AVAILABILITY = Gauge(
    "ops_slo_availability_ratio", "Measured availability", ["service", "window"], registry=REGISTRY
)
SLO_TARGET = Gauge("ops_slo_target_ratio", "Availability target", ["service"], registry=REGISTRY)
ERROR_BUDGET_REMAINING = Gauge(
    "ops_error_budget_remaining_ratio", "Fraction of the 30d error budget left", ["service"], registry=REGISTRY
)

CYCLE_SECONDS = Histogram(
    "ops_monitor_cycle_seconds",
    "Time to run one monitoring cycle",
    registry=REGISTRY,
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13),
)
LAST_CYCLE = Gauge("ops_monitor_last_cycle_timestamp", "Unix time of last completed cycle", registry=REGISTRY)


def set_state(service: str, state: str) -> None:
    for s in (HEALTHY, DEGRADED, DOWN, UNKNOWN):
        SERVICE_STATE.labels(service, s).set(1 if s == state else 0)
