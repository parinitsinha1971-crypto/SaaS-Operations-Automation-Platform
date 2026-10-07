# SaaS Operations Automation Platform

A platform that monitors a fleet of simulated SaaS services, decides when something is wrong, and acts on it: opens an incident, alerts through Alertmanager, restarts the broken container with backoff and a circuit breaker, and writes a daily SLO report.

```
             SaaS Services                       saas-api · saas-web · saas-db (Postgres)
          /       |       \                      each with /health, /metrics, JSON logs, chaos controls
       API      Web App    DB
        \         |        /
                  ↓
          Monitoring Agent                      ops-platform (FastAPI), one cycle every 10s
                  ↓
     Logs      Metrics     Health               log analysis · CPU/mem/disk · HTTP/TCP/API/SQL checks
       └──────────┼──────────┘                  + EWMA anomaly detection, dependency awareness
                  ↓
             Automation                         debounced state machine → incidents
                  ↓
    Alert       Restart    Report               Alertmanager · Docker restart · daily SLO report
```

Prometheus scrapes the platform and every service, Grafana ships with a provisioned dashboard, PostgreSQL stores incident history, and GitHub Actions runs lint, tests on SQLite and Postgres, config validation, and a full end-to-end chaos test against the running stack.

## Quick start

Requires Docker with Compose v2 and `make`.

```bash
make up        # creates .env with random secrets, builds, starts 10 containers, waits until healthy
```

| What | Where |
|---|---|
| Ops console (dashboard) | http://localhost:8080 |
| API docs (OpenAPI) | http://localhost:8080/docs |
| Grafana (user `admin`, password printed by `make init`) | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Alertmanager | http://localhost:9093 |

The API key for restarts, chaos and incident actions is `OPS_API_KEY` in `.env`. Paste it into **Set API key** in the console.

### Break something

Open **Chaos lab** in the console, or:

```bash
make crash-api   # process exits → incident → restart in ~200ms → resolved
make kill-db     # Postgres container killed → restarted
make smoke       # the full CI scenario: crash, hang, kill, alerts, reports
```

What happens on a crash, as recorded on the incident timeline:

```
09:42:26  detected   Container saas-api is exited
09:42:26  alert      Alertmanager: alert sent (critical)
09:42:26  restart    restarted container saas-api in 193ms (attempt 1/3)
09:42:32  resolved   Recovered after 6s
```

## Features

| Feature | How it works | Code |
|---|---|---|
| HTTP health checks | GET with expected status codes and timeout | `ops/checks.py` |
| TCP port checks | async connect with timeout | `ops/checks.py` |
| API monitoring | calls business endpoints, asserts JSON keys and a latency budget; 5xx ratio and p95 also derived from access logs | `ops/checks.py`, `ops/logs.py` |
| Database checks | connects, runs a query, reports DB size and connections | `ops/checks.py` |
| CPU / memory monitoring | Docker stats API (same formula as `docker stats`) against the container's limits | `ops/runtime.py` |
| Disk monitoring | each service's data usage against its quota (Postgres: `pg_database_size`) plus host filesystem | `ops/monitor.py` |
| Service status | per-cycle verdict → debounced state: healthy / degraded / down | `ops/health.py` |
| Log analysis | JSON and plain-text parsing, error fingerprinting (`timeout after 3012ms user_id=88` and `timeout after 941ms user_id=7` become one signature), regex rules, error-rate bursts | `ops/logs.py` |
| Anomaly detection | EWMA baseline per metric; fires on sustained z-score > 3.5 above an absolute floor | `ops/anomaly.py` |
| Automatic restart | Docker restart with policy, backoff, grace period, circuit breaker, dependency check | `ops/remediation.py` |
| Alerting | incidents pushed to Alertmanager v2; 15 Prometheus rules incl. multi-window SLO burn rates | `ops/alerting.py`, `prometheus/rules/` |
| Incident history | deduplicated per service + problem, escalation, event timeline, root-cause hint, ack / notes / manual resolve | `ops/incidents.py` |
| Daily reports | availability vs SLO, 30-day error budget, MTTR, restarts, peaks, top errors; HTML, Markdown and JSON, optional Slack/Discord webhook | `ops/reports.py` |
| Dashboard | the ops console plus a provisioned 19-panel Grafana dashboard | `ops/templates/`, `grafana/` |

## Design decisions worth knowing

**Flap protection.** A service is only marked down after 3 consecutive failed cycles and healthy again after 2 good ones. One dropped packet doesn't page anyone or restart anything.

**Crashes are handled immediately but never invisibly.** An exited container is restarted on the next cycle without waiting for the failure threshold, and it still gets an incident, so the crash shows up in history and reports.

**Restart guard rails**, applied in order:
1. *Policy*: only triggers listed in `restart_on` (`down`, `exited`, `memory_critical`).
2. *Circuit breaker*: after 3 restarts in 15 minutes, auto-restart is suspended and a critical "needs a human" incident opens. Restart loops are a human's problem. It closes again after a full healthy window, or via **Resume** in the console.
3. *Dependencies*: if an upstream service is down (the web app depends on the API), restarting the downstream won't help, so it's skipped and the incident says why.
4. *Grace and backoff*: no new restart while a service boots, and attempts back off 0s / 30s / 120s.
5. *Failing now*: a restart only fires while checks are actually failing. A service that just recovered stays "down" until it has passed 2 cycles, and is not restarted again in that window.

**Least privilege for Docker.** The platform never sees the Docker socket. It talks to [`docker-socket-proxy`](https://github.com/tecnativa/docker-socket-proxy), which allows only container reads plus restart and kill, on an internal-only network. On top of that, the platform refuses to touch any container without the `ops.managed=true` label, including its own.

**Monitored services have Docker's restart policy off** (`restart: "no"`), so the platform is the one doing the restarting, with all the guard rails above. Infrastructure containers (Prometheus, Grafana, the platform itself) use `unless-stopped`.

**Who watches the watcher.** Prometheus scrapes the services directly as well as through the platform. If the platform dies, `OpsPlatformDown`, `MonitorLoopStalled` and `ScrapeTargetDown` still fire. Alerts the platform pushes expire after 5 minutes unless refreshed, so a dead platform can't leave stale alerts firing forever.

**Anomalies don't become the baseline.** While a metric is anomalous, the detector freezes its variance and only nudges its mean, so a long incident keeps alerting rather than quietly becoming "normal".

## Configuration

Services, checks, thresholds and policies live in [`ops-platform/config/services.yaml`](ops-platform/config/services.yaml). Adding a service is a YAML block:

```yaml
- name: billing
  display_name: Billing
  container: billing             # must carry the label ops.managed=true to be restartable
  depends_on: [saas-db]
  slo: { availability: 99.9, latency_ms: 300 }
  checks:
    - { type: http, name: health, url: "http://billing:8000/health" }
    - { type: api, name: invoices, url: "http://billing:8000/v1/invoices?limit=1", expect_json_keys: [data], max_latency_ms: 400 }
  logs:
    source: docker
    rules:
      - { name: stripe-errors, pattern: "stripe.*(declined|timeout)", severity: warning, min_count: 5 }
```

Environment variables (see `.env.example`): `OPS_CHECK_INTERVAL`, `OPS_REPORT_TIME_UTC`, `OPS_RETENTION_DAYS`, `OPS_PROTECT_READS` (require the key for read endpoints too), `REPORT_WEBHOOK_URL`, and the public URLs used in alert links.

To route alerts to Slack, PagerDuty or email, add a receiver in [`alertmanager/alertmanager.yml`](alertmanager/alertmanager.yml); a commented Slack example is there.

## API

All under `/api`; full schema at `/docs`. Write endpoints need `X-API-Key` (or `Authorization: Bearer`). `X-Operator` names who acted, for the audit trail.

| Method | Path | |
|---|---|---|
| GET | `/overview` | everything the console shows |
| GET | `/services/{name}` · `/services/{name}/history?minutes=60` | live status, recent failures, restarts, top errors · stored samples |
| POST | `/services/{name}/restart` · `/services/{name}/remediation/resume` | manual restart · close the circuit breaker |
| GET | `/incidents?status=open&service=` · `/incidents/{id}` | history · detail with timeline |
| POST | `/incidents/{id}/ack` · `/notes` · `/resolve` | operator actions |
| GET / POST | `/reports` · `/reports/{day}` · `/reports/{day}/markdown` · `/reports/generate?day=` | daily reports |
| POST | `/chaos/{service}/{action}` | `latency`, `errors`, `cpu`, `memory`, `disk`, `unhealthy`, `hang`, `crash`, `reset`, `kill` |
| POST | `/alerts/webhook` | Alertmanager receiver (separate token) |

Plus `/metrics` (Prometheus), `/healthz` and `/readyz`.

## Development

```bash
make install   # Python deps
make test      # 49 tests, ~2s (SQLite)
make lint
make dev       # run services + platform as local processes, no Docker
```

Run the same tests on PostgreSQL with `TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost/ops_test make test`. Alert rules have their own unit tests: `promtool test rules prometheus/tests/alerts_test.yml`. The Grafana dashboard is generated from `grafana/build_dashboard.py` (`make dashboard`); CI fails if the JSON is out of date.

### CI (GitHub Actions)

`.github/workflows/ci.yml` runs on every push and PR:
1. **lint**: ruff, shellcheck, dashboard JSON freshness
2. **test**: the suite on SQLite and on a PostgreSQL 16 service container
3. **validate-config**: `docker compose config`, `promtool check config`, promtool rule unit tests, `amtool check-config`, both service configs
4. **integration**: builds the images, starts the full stack, runs `scripts/smoke-test.sh` (crash the API, hang the web app, kill the database; expects detection, incidents, restarts, alerts through Alertmanager and back, reports), dumps logs on failure

`release.yml` publishes multi-arch images to GHCR on `v*` tags.

## Running on a Linux server

```bash
sudo cp -r saas-ops-platform /opt/
sudo cp /opt/saas-ops-platform/deploy/systemd/saas-ops.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now saas-ops
```

The unit generates secrets on first start and brings the stack up at boot. Put the console behind a reverse proxy with TLS before exposing it, and set `OPS_PROTECT_READS=true` if the dashboard shouldn't be public.

## Layout

```
services/sim/          simulated SaaS service (one image: API or web role) + traffic generator
ops-platform/ops/      the platform: checks, runtime, logs, anomaly, health, incidents,
                       remediation, alerting, reports, monitor loop, API, dashboard
ops-platform/config/   services.yaml (Docker), local.yaml (no Docker)
ops-platform/tests/    unit + end-to-end tests with fake Docker, fleet and Alertmanager
prometheus/            scrape config, alert + recording rules, rule unit tests
alertmanager/          routing, inhibition, webhook back to the platform
grafana/               provisioned datasources (Prometheus + read-only Postgres) and dashboard
db/ops-init/           creates Grafana's read-only database role
scripts/               init-env, smoke-test, dev
deploy/systemd/        boot-time service unit
.github/workflows/     CI and image release
```

## Limitations and next steps

- **One monitoring instance.** The loop runs in-process; running two replicas would double-restart. The API can scale separately with `OPS_RUN_AGENT=false`; leader election via a Postgres advisory lock would be the next step.
- **Schema via `create_all`.** Fine for a fresh install; add Alembic migrations before changing models on a live database.
- **Single host.** The runtime adapter is Docker. A Kubernetes adapter would implement the same five methods in `ops/runtime.py` (info, stats, restart, kill, logs).
- **Restart is the only remediation.** The engine is built to take more actions (scale out, fail over, clear a cache) behind the same guard rails.
