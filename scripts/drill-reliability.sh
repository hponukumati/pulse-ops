#!/usr/bin/env sh
# Reliability drills against the running Compose stack (synthetic data):
#  1. Redis outage: incident + outbox commit while Redis is down; dispatch recovers after restart.
#  2. Two worker instances + a SIGKILLed worker around schedule slots: one evaluation per
#     (rule, version, slot) and one stored transition per incident version.
#  3. Restart persistence: committed data survives `docker compose down` + `up` (named volume).
# Usage: ./scripts/demo-up.sh && ./scripts/drill-reliability.sh   Dependencies: sh, curl, jq, docker.
set -eu
cd "$(dirname "$0")/.."
BASE=${PULSEOPS_BASE_URL:-http://127.0.0.1:8080}; CREDS=${PULSEOPS_DEMO_CREDENTIALS:-.demo-credentials.json}
umask 077; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT INT TERM
. ./scripts/lib.sh

echo "== 1. Redis outage during an incident transition"
setup_breaching_service
[ -n "$RULE" ] && [ "$RULE" != null ] && ok "breaching service + rule created" || bad "setup"
docker compose stop redis >/dev/null 2>&1
echo "   redis stopped at $(now); waiting for two evaluation slots..."
i=0; until [ "$(psql_q "SELECT count(*) FROM incidents WHERE rule_id = '$RULE'")" = 1 ] || [ $i -ge 60 ]; do sleep 5; i=$((i + 1)); done
[ "$(psql_q "SELECT status FROM incidents WHERE rule_id = '$RULE'")" = OPEN ] && ok "incident committed while Redis was down" || bad "incident not opened"
PENDING=$(psql_q "SELECT count(*) FROM outbox_events o JOIN incidents i ON i.id = o.incident_id WHERE i.rule_id = '$RULE' AND o.sent_at IS NULL")
ATTEMPTS=$(psql_q "SELECT coalesce(max(attempts),0) FROM outbox_events o JOIN incidents i ON i.id = o.incident_id WHERE i.rule_id = '$RULE'")
[ "$PENDING" = 1 ] && [ "$ATTEMPTS" -ge 1 ] && ok "outbox record pending with $ATTEMPTS failed publish attempt(s)" || bad "outbox state pending=$PENDING attempts=$ATTEMPTS"
READY=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health/ready")
[ "$READY" = 503 ] && ok "readiness reports dependency failure (503)" || bad "readiness=$READY"
docker compose start redis >/dev/null 2>&1
echo "   redis restarted at $(now); waiting for dispatch (backoff <= 60 s)..."
i=0; until [ "$(psql_q "SELECT count(*) FROM outbox_events o JOIN incidents i ON i.id = o.incident_id WHERE i.rule_id = '$RULE' AND o.sent_at IS NULL")" = 0 ] || [ $i -ge 30 ]; do sleep 3; i=$((i + 1)); done
[ "$(psql_q "SELECT count(*) FROM outbox_events o JOIN incidents i ON i.id = o.incident_id WHERE i.rule_id = '$RULE' AND o.sent_at IS NOT NULL")" = 1 ] && ok "outbox dispatched after Redis recovery" || bad "outbox not dispatched"
[ "$(psql_q "SELECT count(*) FROM incident_events e JOIN incidents i ON i.id = e.incident_id WHERE i.rule_id = '$RULE'")" = 1 ] && ok "exactly one stored transition (OPENED)" || bad "transition count"

echo "== 2. Two workers + SIGKILL around schedule slots"
docker compose up -d --scale worker=2 --no-recreate worker >/dev/null 2>&1
sleep 75
docker compose kill -s SIGKILL worker >/dev/null 2>&1
docker compose up -d --scale worker=2 worker >/dev/null 2>&1
sleep 75
DUP_EVAL=$(psql_q "SELECT count(*) FROM (SELECT rule_id, rule_version, scheduled_at FROM rule_evaluations GROUP BY 1,2,3 HAVING count(*) > 1) d")
DUP_TRANS=$(psql_q "SELECT count(*) FROM (SELECT incident_id, version FROM incident_events GROUP BY 1,2 HAVING count(*) > 1) d")
ACTIVE=$(psql_q "SELECT count(*) FROM (SELECT rule_id FROM incidents WHERE status IN ('OPEN','ACKNOWLEDGED') GROUP BY 1 HAVING count(*) > 1) d")
WORKERS=$(psql_q "SELECT count(DISTINCT worker_id) FROM rule_evaluations WHERE rule_id = '$RULE'")
SLOTS=$(psql_q "SELECT count(*) FROM rule_evaluations WHERE rule_id = '$RULE'")
echo "   evaluations for drill rule: $SLOTS slots by $WORKERS distinct worker process(es)"
[ "$DUP_EVAL" = 0 ] && ok "no slot evaluated twice" || bad "duplicate evaluations: $DUP_EVAL"
[ "$DUP_TRANS" = 0 ] && ok "no duplicate transitions" || bad "duplicate transitions: $DUP_TRANS"
[ "$ACTIVE" = 0 ] && ok "at most one active incident per rule" || bad "multiple active incidents: $ACTIVE"
docker compose up -d --scale worker=1 worker >/dev/null 2>&1

echo "== 3. Restart persistence"
BEFORE=$(psql_q "SELECT (SELECT count(*) FROM request_events) || ':' || (SELECT count(*) FROM incidents) || ':' || (SELECT count(*) FROM users)")
docker compose down >/dev/null 2>&1
PULSEOPS_DEMO_MODE=true docker compose up -d >/dev/null 2>&1
i=0; until [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health/ready")" = 200 ] || [ $i -ge 60 ]; do sleep 2; i=$((i + 1)); done
AFTER=$(psql_q "SELECT (SELECT count(*) FROM request_events) || ':' || (SELECT count(*) FROM incidents) || ':' || (SELECT count(*) FROM users)")
[ "$BEFORE" = "$AFTER" ] && ok "events:incidents:users persisted across down/up ($AFTER)" || bad "persistence $BEFORE vs $AFTER"
login_as alice
[ "$(gql_as alice '{me{email}}' | jq -r .data.me.email)" = alice@pulseops.test ] && ok "login works after restart" || bad "login after restart"

echo "drills: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
