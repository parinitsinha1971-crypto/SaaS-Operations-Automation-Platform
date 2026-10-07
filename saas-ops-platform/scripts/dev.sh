#!/usr/bin/env bash
# Run the API, web app, traffic generator and ops platform as local processes,
# without Docker. Useful for working on the platform's code.
#   ./scripts/dev.sh        then open http://localhost:8080   (Ctrl-C stops everything)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
export DEV_DIR="$ROOT/.dev"
mkdir -p "$DEV_DIR"

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

start_sim() {  # role port
  (cd services/sim && SERVICE_ROLE=$1 SERVICE_NAME=saas-$1 DATA_DIR="$DEV_DIR/data-$1" DISK_QUOTA_MB=500 \
    LOG_FILE="$DEV_DIR/saas-$1.log" API_URL=http://127.0.0.1:18001 \
    python -m uvicorn sim.app:app --port "$2" --no-access-log >"$DEV_DIR/$1.stdout" 2>&1) &
  pids+=($!)
}

start_sim api 18001
start_sim web 18002
(cd services/sim && TARGETS=http://127.0.0.1:18001,http://127.0.0.1:18002 RPS=5 python -m sim.traffic \
  >"$DEV_DIR/traffic.stdout" 2>&1) &
pids+=($!)

echo "API on :18001, web app on :18002, dashboard on http://localhost:8080"
echo "API key: ${OPS_API_KEY:=dev-key}"
cd ops-platform
OPS_CONFIG=config/local.yaml OPS_RUNTIME=none OPS_API_KEY=$OPS_API_KEY \
  DATABASE_URL="sqlite:///$DEV_DIR/ops.db" python -m uvicorn ops.main:app --port 8080
