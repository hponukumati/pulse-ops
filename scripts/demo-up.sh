#!/usr/bin/env sh
# Start the stack in explicit LOCAL DEMO MODE and seed synthetic demo accounts/services.
# Credentials are random per run, written to .demo-credentials.json (0600, gitignored).
# Dependencies: Docker Compose v2, POSIX sh. Windows: run from WSL.
set -eu
cd "$(dirname "$0")/.."
[ -n "$(ls -A infra/secrets 2>/dev/null)" ] || ./scripts/gen-secrets.sh
PULSEOPS_DEMO_MODE=true docker compose up --build -d
echo "Waiting for the API to become healthy..."
i=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null)" = "healthy" ]; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "API did not become healthy" >&2; exit 1; }; sleep 2
done
umask 077
docker compose exec -T api pulseops-admin seed-demo > .demo-credentials.json
echo "Synthetic demo accounts written to .demo-credentials.json (local only; do not share)."
echo "Open http://127.0.0.1:8080"
