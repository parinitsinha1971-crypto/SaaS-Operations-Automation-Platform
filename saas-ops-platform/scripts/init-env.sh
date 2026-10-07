#!/usr/bin/env bash
# Creates .env with random secrets and the Alertmanager webhook token file.
# Safe to re-run: an existing .env is never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."

rand() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }

if [[ -f .env ]]; then
  echo ".env already exists; leaving it alone."
else
  sed \
    -e "s|^OPS_API_KEY=.*|OPS_API_KEY=$(rand 40)|" \
    -e "s|^OPS_WEBHOOK_TOKEN=.*|OPS_WEBHOOK_TOKEN=$(rand 40)|" \
    -e "s|^OPS_DB_PASSWORD=.*|OPS_DB_PASSWORD=$(rand)|" \
    -e "s|^SAAS_DB_PASSWORD=.*|SAAS_DB_PASSWORD=$(rand)|" \
    -e "s|^GRAFANA_DB_PASSWORD=.*|GRAFANA_DB_PASSWORD=$(rand)|" \
    -e "s|^GRAFANA_ADMIN_PASSWORD=.*|GRAFANA_ADMIN_PASSWORD=$(rand 20)|" \
    -e "s|^CHAOS_TOKEN=.*|CHAOS_TOKEN=$(rand)|" \
    .env.example > .env
  chmod 600 .env
  echo "Created .env with fresh secrets."
fi

# Alertmanager can't read env vars in its config, so the webhook token goes in a file
token=$(grep '^OPS_WEBHOOK_TOKEN=' .env | cut -d= -f2-)
mkdir -p alertmanager/secrets
printf '%s' "$token" > alertmanager/secrets/webhook_token
chmod 644 alertmanager/secrets/webhook_token   # read by the alertmanager container user
echo "Wrote alertmanager/secrets/webhook_token."

echo
echo "API key:          $(grep '^OPS_API_KEY=' .env | cut -d= -f2-)"
echo "Grafana password: $(grep '^GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-)  (user: admin)"
