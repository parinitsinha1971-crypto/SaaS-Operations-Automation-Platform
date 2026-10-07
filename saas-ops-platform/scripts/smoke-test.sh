#!/usr/bin/env bash
# End-to-end check of a running stack (used by CI, handy locally):
#   1. every service turns healthy and every Prometheus target is up
#   2. Grafana has the dashboard and both datasources work
#   3. crash the API -> incident opens -> platform restarts it -> incident resolves
#   4. kill the database container -> platform restarts it
#   5. alerts flow platform -> Alertmanager -> webhook back into the platform
#   6. a daily report can be generated
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
set -a; source .env; set +a

OPS=${OPS_URL:-http://localhost:8080}
PROM=${PROM_URL:-http://localhost:9090}
GRAFANA=${GRAFANA_URL:-http://localhost:3000}
AUTH=(-H "X-API-Key: ${OPS_API_KEY}" -H "X-Operator: smoke-test")

pass() { printf '  \033[32mok\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; exit 1; }

# wait_for <seconds> <description> <command...>
wait_for() {
  local timeout=$1 what=$2; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    (( SECONDS - start > timeout )) && fail "timed out after ${timeout}s waiting for: $what"
    sleep 3
  done
  pass "$what ($((SECONDS - start))s)"
}

healthy_count() { curl -fsS "$OPS/api/overview" | jq -e ".counts.healthy == $1"; }
service_state() { curl -fsS "$OPS/api/services/$1" | jq -e --arg s "$2" '.status.state == $s'; }
restarted() { curl -fsS "$OPS/api/remediation?service=$1" | jq -e 'map(select(.status == "success" and .initiated_by == "auto")) | length >= 1'; }
targets_up() { curl -fsS "$PROM/api/v1/targets" | jq -e '[.data.activeTargets[].health] | all(. == "up")'; }

echo "Stack"
wait_for 120 "ops platform ready" curl -fsS "$OPS/readyz"
wait_for 180 "all 3 services healthy" healthy_count 3
wait_for 90 "all Prometheus targets up" targets_up
wait_for 60 "Grafana healthy" curl -fsS "$GRAFANA/api/health"
curl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" "$GRAFANA/api/dashboards/uid/saas-ops-overview" | jq -e '.dashboard.panels | length > 10' >/dev/null \
  && pass "Grafana dashboard provisioned" || fail "Grafana dashboard missing"
for ds in prometheus ops-postgres; do
  curl -fsS -u "admin:${GRAFANA_ADMIN_PASSWORD}" "$GRAFANA/api/datasources/uid/$ds/health" | jq -e '.status == "OK"' >/dev/null \
    && pass "Grafana datasource $ds works" || fail "Grafana datasource $ds failing"
done

resolved_with_restart() {
  curl -fsS "$OPS/api/incidents?service=$1&status=resolved" \
    | jq -e "map(select(.key == \"availability\" and .restarts >= 1)) | length >= $2"
}

echo "Auto-remediation: crash the API (process exits)"
curl -fsS -X POST "${AUTH[@]}" "$OPS/api/chaos/saas-api/crash" >/dev/null && pass "crash injected"
wait_for 90 "API auto-restarted" restarted saas-api
wait_for 90 "crash incident resolved with the restart on its timeline" resolved_with_restart saas-api 1
wait_for 60 "API healthy" service_state saas-api healthy

echo "Auto-remediation: hang the web app (process alive, health check times out)"
curl -fsS -X POST "${AUTH[@]}" "$OPS/api/chaos/saas-web/hang" >/dev/null && pass "hang injected"
wait_for 120 "web app marked down after the failure threshold" service_state saas-web down
wait_for 90 "web app auto-restarted" restarted saas-web
wait_for 120 "web app healthy again" service_state saas-web healthy
wait_for 60 "hang incident resolved" resolved_with_restart saas-web 1

echo "Auto-remediation: kill the database container"
curl -fsS -X POST "${AUTH[@]}" "$OPS/api/chaos/saas-db/kill" >/dev/null && pass "saas-db killed"
wait_for 120 "database auto-restarted" restarted saas-db
wait_for 120 "database healthy again" service_state saas-db healthy

echo "Alerting"
wait_for 90 "platform alerts came back through Alertmanager's webhook" \
  bash -c "curl -fsS '$OPS/api/alerts' | jq -e 'map(select(.source == \"ops-platform\")) | length >= 1'"

echo "Reports"
curl -fsS -X POST "${AUTH[@]}" "$OPS/api/reports/generate" | jq -e '.services | length == 3' >/dev/null \
  && pass "daily report generated" || fail "report generation failed"

echo "Auth"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$OPS/api/services/saas-api/restart")
[[ $code == 401 ]] && pass "write endpoints reject missing key" || fail "expected 401, got $code"

echo
echo "All smoke tests passed."
