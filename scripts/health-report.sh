#!/usr/bin/env sh
# Owner-facing health report: backup age/failures, readiness, worker and outbox state.
# Exit code 1 if the latest backup failed or is older than 26 hours (daily schedule + slack).
set -eu
cd "$(dirname "$0")/.."
BASE=${PULSEOPS_BASE_URL:-http://127.0.0.1:8080}; DEST=${PULSEOPS_BACKUP_DIR:-./backups}
STATUS=1
echo "PulseOps health report $(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ -f "$DEST/last-backup-status.json" ]; then
  OK=$(jq -r .ok "$DEST/last-backup-status.json"); WHEN=$(jq -r .time "$DEST/last-backup-status.json")
  LATEST=$(ls -t "$DEST"/pulseops-*.dump.age 2>/dev/null | head -1)
  AGE_H=$(( ( $(date +%s) - $(stat -f %m "$LATEST" 2>/dev/null || stat -c %Y "$LATEST") ) / 3600 ))
  echo "backup: last run $WHEN ok=$OK; newest file age ${AGE_H}h ($(basename "$LATEST"))"
  RC=$(ls -t "$DEST"/*.restore-check.json 2>/dev/null | head -1 || true)
  [ -n "$RC" ] && echo "restore check: $(jq -r '"\(.checked_at) passed=\(.passed) failed=\(.failed) restore=\(.restore_duration_s)s"' "$RC")"
  [ "$OK" = true ] && [ "$AGE_H" -lt 26 ] && STATUS=0
else
  echo "backup: NO BACKUP RECORDED"
fi
echo "readiness: HTTP $(curl -s -o /dev/null -w '%{http_code}' "$BASE/health/ready")"
docker compose exec -T postgres psql -U postgres -d pulseops -At -F' ' -c "SELECT 'worker heartbeat age(s):', coalesce(extract(epoch FROM now() - max(heartbeat_at))::int::text, 'none') FROM worker_heartbeats UNION ALL SELECT 'outbox pending:', count(*)::text FROM outbox_events WHERE sent_at IS NULL UNION ALL SELECT 'deletion jobs not done:', count(*)::text FROM deletion_jobs WHERE status <> 'DONE' UNION ALL SELECT 'retained events:', used::text FROM ingest_quota" 2>/dev/null || echo "database: unavailable"
exit $STATUS
