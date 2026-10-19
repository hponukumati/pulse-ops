#!/usr/bin/env sh
# Restore an encrypted backup into an ISOLATED PostgreSQL instance (internal-only Docker
# network, no published ports) and verify it. Never touches the live database.
#   ./scripts/restore-check.sh [BACKUP.dump.age]   (default: newest in $PULSEOPS_BACKUP_DIR or ./backups)
# Checks: sha256 vs manifest, decrypt + pg_restore, migrations at head, row counts and
# per-service aggregates vs manifest, foreign keys, then the post-restore reconciliation
# (revoke all sessions + ingest tokens, enforce retention, reapply deletions from the ledger)
# and a stale-permission diff against the live grants. Writes a JSON report next to the backup.
set -eu
cd "$(dirname "$0")/.."
DEST=${PULSEOPS_BACKUP_DIR:-./backups}
FILE=${1:-$(ls -t "$DEST"/pulseops-*.dump.age 2>/dev/null | head -1)}
[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "no backup found" >&2; exit 2; }
MAN=${FILE%.dump.age}.manifest.json
IDENTITY=${PULSEOPS_BACKUP_IDENTITY_FILE:-$HOME/.pulseops-backup-keys/identity.txt}
[ -f "$IDENTITY" ] || { echo "missing decryption identity ($IDENTITY)" >&2; exit 2; }
PROJECT=$(basename "$PWD" | tr -cd 'a-z0-9_-')
NET="pulseops-restorecheck-$$"; DB="pulseops-restorecheck-$$"
umask 077; TMP=$(mktemp -d)
cleanup() { docker rm -f "$DB" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1"; }
q() { docker exec "$DB" psql -U postgres -d pulseops -At -c "$1"; }
T0=$(date +%s)

SHA=$(shasum -a 256 "$FILE" 2>/dev/null | cut -d' ' -f1 || sha256sum "$FILE" | cut -d' ' -f1)
[ "$SHA" = "$(jq -r .sha256 "$MAN")" ] && ok "sha256 matches manifest" || bad "sha256 mismatch"

docker network create --internal "$NET" >/dev/null
# Trust auth is acceptable only because the instance is reachable solely on this internal network.
docker run -d --name "$DB" --network "$NET" -e POSTGRES_HOST_AUTH_METHOD=trust postgres:18.6-alpine >/dev/null
i=0; until docker exec "$DB" pg_isready -U postgres >/dev/null 2>&1 || [ $i -ge 60 ]; do sleep 1; i=$((i + 1)); done
for r in migrator app maint; do openssl rand -hex 24 > "$TMP/$r"; done
docker exec -i "$DB" psql -U postgres -v ON_ERROR_STOP=1 -q <<SQL
CREATE ROLE pulseops_migrator LOGIN PASSWORD '$(cat "$TMP/migrator")';
CREATE ROLE pulseops_app LOGIN NOINHERIT PASSWORD '$(cat "$TMP/app")';
CREATE ROLE pulseops_maint LOGIN NOINHERIT PASSWORD '$(cat "$TMP/maint")';
CREATE DATABASE pulseops OWNER pulseops_migrator;
SQL

R0=$(date +%s)
docker compose --profile tools build -q backup-tools >/dev/null
# Run as the identity's owner so the private key can stay 0600 (Linux enforces ownership on bind mounts).
docker compose --profile tools run --rm -T --no-deps --user "$(id -u):$(id -g)" -v "$IDENTITY:/keys/identity.txt:ro" backup-tools age -d -i /keys/identity.txt < "$FILE" \
  | docker exec -i "$DB" pg_restore -U postgres -d pulseops --exit-on-error --single-transaction
RESTORE_S=$(($(date +%s) - R0))
ok "decrypted and restored in ${RESTORE_S}s"

# Throwaway secrets for running the app image against the isolated copy.
mkdir -p "$TMP/s" "$TMP/ledger"
for r in migrator app maint; do cp "$TMP/$r" "$TMP/s/$r"; done
openssl rand -hex 24 > "$TMP/s/redis"
for k in refresh ratelimit; do openssl rand -base64 32 > "$TMP/s/$k"; done
printf '{"current":1,"keys":{"1":"%s"}}' "$(openssl rand -base64 32)" > "$TMP/s/ingest.json"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$TMP/s/jwt.pem" 2>/dev/null
chmod 755 "$TMP" "$TMP/s" "$TMP/ledger"; chmod 644 "$TMP/s"/*
docker run --rm -v "${PROJECT}_ledger:/ledger:ro" --entrypoint sh pulseops-backend:local -c 'cat /ledger/deletions.jsonl 2>/dev/null || true' > "$TMP/ledger/deletions.jsonl"
chmod 644 "$TMP/ledger/deletions.jsonl"
APP="docker run --rm --network $NET -v $TMP/s:/s:ro -v $TMP/ledger:/ledger:ro
  -e PULSEOPS_ENV=local -e PULSEOPS_DB_HOST=$DB -e PULSEOPS_REDIS_HOST=unused
  -e PULSEOPS_DB_PASSWORD_FILE=/s/app -e PULSEOPS_DB_MAINT_PASSWORD_FILE=/s/maint -e PULSEOPS_DB_MIGRATOR_PASSWORD_FILE=/s/migrator
  -e PULSEOPS_REDIS_PASSWORD_FILE=/s/redis -e PULSEOPS_JWT_PRIVATE_KEY_FILE=/s/jwt.pem -e PULSEOPS_REFRESH_HMAC_KEY_FILE=/s/refresh
  -e PULSEOPS_RATE_LIMIT_KEY_FILE=/s/ratelimit -e PULSEOPS_INGEST_HMAC_KEYS_FILE=/s/ingest.json pulseops-backend:local"

$APP alembic upgrade head >/dev/null 2>&1 && HEAD=$($APP alembic current 2>/dev/null | awk '{print $1}' | tail -1)
[ "${HEAD:-}" = "$(jq -r .alembic "$MAN")" ] && ok "migrations at head ($HEAD)" || bad "migration head ${HEAD:-none}"

COUNTS=$(q "SELECT json_build_object('users',(SELECT count(*) FROM users),'services',(SELECT count(*) FROM services),'service_grants',(SELECT count(*) FROM service_grants),'routes',(SELECT count(*) FROM routes),'ingest_tokens',(SELECT count(*) FROM ingest_tokens),'request_events',(SELECT count(*) FROM request_events),'alert_rules',(SELECT count(*) FROM alert_rules),'incidents',(SELECT count(*) FROM incidents),'incident_events',(SELECT count(*) FROM incident_events),'deletion_jobs',(SELECT count(*) FROM deletion_jobs))")
[ "$(echo "$COUNTS" | jq -S .)" = "$(jq -S .counts "$MAN")" ] && ok "row counts match manifest" || { bad "row counts differ"; echo "$COUNTS"; jq -c .counts "$MAN"; }
AGG=$(q "SELECT coalesce(json_object_agg(service_id, json_build_object('events', n, 'p95_ms', p95)), '{}'::json) FROM (SELECT service_id, count(*) AS n, round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 3) AS p95 FROM request_events GROUP BY service_id) a")
[ "$(echo "$AGG" | jq -S .)" = "$(jq -S .aggregates "$MAN")" ] && ok "per-service event counts and p95 match" || bad "aggregates differ"
[ "$(q "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND NOT convalidated")" = 0 ] && ok "all foreign keys validated" || bad "unvalidated foreign keys"
ORPHANS=$(q "SELECT (SELECT count(*) FROM request_events e LEFT JOIN routes r ON r.service_id = e.service_id AND r.id = e.route_id WHERE r.id IS NULL) + (SELECT count(*) FROM service_grants g LEFT JOIN users u ON u.id = g.user_id WHERE u.id IS NULL)")
[ "$ORPHANS" = 0 ] && ok "no orphaned rows" || bad "orphans: $ORPHANS"

# Post-restore reconciliation (before any access is reopened).
$APP pulseops-admin restore-reconcile --ledger-dir /ledger > "$TMP/reconcile.json"
[ "$(q "SELECT count(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > now()")" = 0 ] && ok "all restored sessions invalidated" || bad "active sessions remain"
[ "$(q "SELECT count(*) FROM ingest_tokens WHERE revoked_at IS NULL")" = 0 ] && ok "all restored ingest credentials revoked" || bad "active ingest tokens remain"
REAPPLIED=$(jq .deletionsReapplied "$TMP/reconcile.json")
MISSED=0
for sid in $(jq -r .serviceId "$TMP/ledger/deletions.jsonl" 2>/dev/null); do
  [ "$(q "SELECT count(*) FROM services WHERE id = '$sid' AND deleted_at IS NULL")" = 0 ] || MISSED=$((MISSED + 1))
done
[ "$MISSED" = 0 ] && ok "ledger deletions reapplied ($REAPPLIED re-marked deleted)" || bad "$MISSED ledger deletions not applied"
[ "$(q "SELECT count(*) FROM request_events WHERE occurred_at < now() - (SELECT event_retention_days FROM app_settings) * interval '1 day'")" = 0 ] && ok "retention enforced on restored copy" || bad "expired events remain"

# Stale-permission review: grants in the restored snapshot vs the live system.
q "SELECT user_id || ':' || service_id FROM service_grants g JOIN services s ON s.id = g.service_id WHERE s.deleted_at IS NULL ORDER BY 1" > "$TMP/restored.grants"
docker compose exec -T postgres psql -U postgres -d pulseops -At -c "SELECT user_id || ':' || service_id FROM service_grants ORDER BY 1" > "$TMP/live.grants" 2>/dev/null || : > "$TMP/live.grants"
STALE=$(comm -23 "$TMP/restored.grants" "$TMP/live.grants" | wc -l | tr -d ' ')
NEWER=$(comm -13 "$TMP/restored.grants" "$TMP/live.grants" | wc -l | tr -d ' ')
echo "INFO  grant review: $STALE stale grant(s) present only in the restored snapshot, $NEWER only in live"
[ "$STALE" = 0 ] || echo "INFO  keep access disabled until stale grants are removed or re-provisioned (docs/OPERATIONS.md)"

TOTAL_S=$(($(date +%s) - T0))
REPORT="${FILE%.dump.age}.restore-check.json"
jq -n --arg file "$(basename "$FILE")" --arg backup_time "$(jq -r .backup_time "$MAN")" --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson restore_s "$RESTORE_S" --argjson total_s "$TOTAL_S" --argjson pass "$PASS" --argjson fail "$FAIL" \
  --argjson stale "$STALE" --argjson newer "$NEWER" --argjson reconcile "$(jq '{sessionsRevoked, ingestTokensRevoked, deletionsReapplied, expiredEventsPurged}' "$TMP/reconcile.json")" \
  '{file:$file, backup_time:$backup_time, checked_at:$checked_at, restore_duration_s:$restore_s, total_duration_s:$total_s, passed:$pass, failed:$fail, stale_grants:$stale, newer_grants:$newer, reconcile:$reconcile}' > "$REPORT"
echo "restore-check: $PASS passed, $FAIL failed (restore ${RESTORE_S}s, total ${TOTAL_S}s) -> $REPORT"
[ "$FAIL" -eq 0 ]
