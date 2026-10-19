#!/usr/bin/env sh
# Encrypted logical backup of PostgreSQL.
#   ./scripts/backup.sh [DEST_DIR]      (default $PULSEOPS_BACKUP_DIR or ./backups)
# For non-disposable data, DEST must be a private location separate from the DB disk and this
# should run daily (cron/systemd timer). Output: pulseops-<UTC>.dump.age (age-encrypted pg_dump
# custom format) + pulseops-<UTC>.manifest.json (counts/aggregates/sha256; no row data).
# Copies older than 14 days are pruned. Status is recorded for scripts/health-report.sh.
set -eu
cd "$(dirname "$0")/.."
DEST=${1:-${PULSEOPS_BACKUP_DIR:-./backups}}
RECIPIENT=${PULSEOPS_BACKUP_RECIPIENT_FILE:-infra/backup-keys/recipient.txt}
[ -s "$RECIPIENT" ] || { echo "missing $RECIPIENT (run ./scripts/backup-keygen.sh)" >&2; exit 2; }
umask 077; mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ); OUT="$DEST/pulseops-$STAMP.dump.age"; MAN="$DEST/pulseops-$STAMP.manifest.json"
status() { printf '{"time":"%s","ok":%s,"file":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$(basename "$OUT")" > "$DEST/last-backup-status.json"; }
trap 'status false' EXIT
START=$(date +%s)
docker compose --profile tools build -q backup-tools >/dev/null
RUN="docker compose --profile tools run --rm -T --no-deps -v $(cd "$(dirname "$RECIPIENT")" && pwd)/$(basename "$RECIPIENT"):/keys/recipient.txt:ro backup-tools"
PGENV='printf "postgres:5432:pulseops:pulseops_migrator:%s\n" "$(cat /run/secrets/db_migrator_password)" > /tmp/.pgpass && chmod 600 /tmp/.pgpass && export PGPASSFILE=/tmp/.pgpass'
# Manifest first (counts/aggregates only), then the encrypted dump.
$RUN sh -c "$PGENV && psql -h postgres -U pulseops_migrator -d pulseops -At -v ON_ERROR_STOP=1 -f -" > "$MAN.tmp" <<'SQL'
SELECT json_build_object(
  'alembic', (SELECT version_num FROM alembic_version),
  'server_version', current_setting('server_version'),
  'counts', json_build_object(
     'users', (SELECT count(*) FROM users), 'services', (SELECT count(*) FROM services),
     'service_grants', (SELECT count(*) FROM service_grants), 'routes', (SELECT count(*) FROM routes),
     'ingest_tokens', (SELECT count(*) FROM ingest_tokens), 'request_events', (SELECT count(*) FROM request_events),
     'alert_rules', (SELECT count(*) FROM alert_rules), 'incidents', (SELECT count(*) FROM incidents),
     'incident_events', (SELECT count(*) FROM incident_events), 'deletion_jobs', (SELECT count(*) FROM deletion_jobs)),
  'aggregates', (SELECT coalesce(json_object_agg(service_id, json_build_object('events', n, 'p95_ms', p95)), '{}'::json) FROM (
     SELECT service_id, count(*) AS n, round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 3) AS p95
     FROM request_events GROUP BY service_id) a));
SQL
$RUN sh -c "$PGENV && pg_dump -h postgres -U pulseops_migrator -d pulseops -Fc | age -R /keys/recipient.txt" > "$OUT"
SHA=$(shasum -a 256 "$OUT" 2>/dev/null | cut -d' ' -f1 || sha256sum "$OUT" | cut -d' ' -f1)
jq --arg t "$STAMP" --arg sha "$SHA" --argjson s "$(($(date +%s) - START))" '. + {backup_time: $t, sha256: $sha, duration_s: $s}' "$MAN.tmp" > "$MAN"
rm -f "$MAN.tmp"
find "$DEST" -maxdepth 1 \( -name 'pulseops-*.dump.age' -o -name 'pulseops-*.manifest.json' \) -mtime +14 -exec rm -f {} +
trap - EXIT; status true
echo "backup: $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes, $(($(date +%s) - START)) s)"
