#!/usr/bin/env sh
# End-to-end smoke test through the nginx edge with synthetic data.
# Usage: ./scripts/demo-up.sh && ./scripts/smoke.sh
# Dependencies: POSIX sh, curl, jq, uuidgen (or /proc/sys/kernel/random/uuid). Windows: use WSL.
# Tokens are kept in a private temp dir and never printed.
set -eu
BASE=${PULSEOPS_BASE_URL:-http://127.0.0.1:8080}
CREDS=${PULSEOPS_DEMO_CREDENTIALS:-.demo-credentials.json}
cd "$(dirname "$0")/.."
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
[ -f "$CREDS" ] || { echo "missing $CREDS (run ./scripts/demo-up.sh)" >&2; exit 2; }
umask 077
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT INT TERM
PASS=0; FAIL=0
ok() { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1"; }
uuid() { if command -v uuidgen >/dev/null; then uuidgen | tr 'A-Z' 'a-z'; else cat /proc/sys/kernel/random/uuid; fi; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
auth_post() { curl -s -o "$TMP/out" -w '%{http_code}' -X POST "$BASE$1" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' "$@"; }
login() { # $1=account name -> writes token to $TMP/<name>.token
  jq -n --arg e "$(jq -r ".accounts.$1.email" "$CREDS")" --arg p "$(jq -r ".accounts.$1.password" "$CREDS")" '{email:$e,password:$p}' > "$TMP/login.json"
  code=$(curl -s -o "$TMP/out" -w '%{http_code}' -X POST "$BASE/api/v1/auth/login" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' -c "$TMP/$1.cookies" --data @"$TMP/login.json")
  rm -f "$TMP/login.json"
  [ "$code" = 200 ] && jq -r .accessToken "$TMP/out" > "$TMP/$1.token"
  echo "$code"
}
gql() { # $1=account, $2=query, $3=variables json
  vars=${3:-}; [ -n "$vars" ] || vars='{}'
  jq -n --arg q "$2" --argjson v "$vars" '{query:$q,variables:$v}' > "$TMP/q.json"
  curl -s -X POST "$BASE/graphql" -H "Authorization: Bearer $(cat "$TMP/$1.token")" -H 'Content-Type: application/json' --data @"$TMP/q.json"
}

[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health/ready")" = 200 ] && ok "readiness" || bad "readiness"
[ "$(login alice)" = 200 ] && ok "alice login" || bad "alice login"
[ "$(login victor)" = 200 ] && ok "viewer login" || bad "viewer login"
SVC_A=$(jq -r '.services["checkout-api"]' "$CREDS"); SVC_B=$(jq -r '.services["billing-api"]' "$CREDS")
ROUTE=$(gql alice 'query($s:ID!){routes(serviceId:$s){edges{node{id template}}}}' "{\"s\":\"$SVC_A\"}" | jq -r '.data.routes.edges[] | select(.node.template=="/checkout") | .node.id')
[ -n "$ROUTE" ] && ok "route lookup" || bad "route lookup"

# Reauthenticate (password from the credentials file, sent via a private temp file), then issue a token.
jq -n --arg p "$(jq -r '.accounts.alice.password' "$CREDS")" '{password:$p}' > "$TMP/reauth.json"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/reauthenticate" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' -H "Authorization: Bearer $(cat "$TMP/alice.token")" --data @"$TMP/reauth.json")
rm -f "$TMP/reauth.json"
[ "$code" = 200 ] && ok "reauthenticate" || bad "reauthenticate ($code)"
gql alice 'mutation($s:ID!){createIngestToken(serviceId:$s){token{id} secret}}' "{\"s\":\"$SVC_A\"}" > "$TMP/tok.json"
jq -r '.data.createIngestToken.secret' "$TMP/tok.json" > "$TMP/ingest.secret"; TOKEN_ID=$(jq -r '.data.createIngestToken.token.id' "$TMP/tok.json"); rm -f "$TMP/tok.json"
grep -q '^poi_' "$TMP/ingest.secret" && ok "issue ingest token" || bad "issue ingest token"

# Ingest 25 synthetic events (3 x 5xx).
i=0; printf '[' > "$TMP/batch.json"
while [ $i -lt 25 ]; do
  st=200; [ $i -lt 3 ] && st=503
  [ $i -gt 0 ] && printf ',' >> "$TMP/batch.json"
  printf '{"eventId":"%s","timestamp":"%s","method":"POST","routeId":"%s","statusCode":%s,"durationMs":%s}' "$(uuid)" "$(now)" "$ROUTE" "$st" "$((20 + i))" >> "$TMP/batch.json"
  i=$((i + 1))
done; printf ']' >> "$TMP/batch.json"
code=$(curl -s -o "$TMP/out" -w '%{http_code}' -X POST "$BASE/api/v1/ingest/events" -H "Authorization: Bearer $(cat "$TMP/ingest.secret")" -H 'Content-Type: application/json' --data @"$TMP/batch.json")
[ "$code" = 200 ] && [ "$(jq .accepted "$TMP/out")" = 25 ] && ok "ingest 25 events" || bad "ingest ($code)"
code=$(curl -s -o "$TMP/out" -w '%{http_code}' -X POST "$BASE/api/v1/ingest/events" -H "Authorization: Bearer $(cat "$TMP/ingest.secret")" -H 'Content-Type: application/json' --data @"$TMP/batch.json")
[ "$code" = 200 ] && [ "$(jq .duplicate "$TMP/out")" = 25 ] && ok "retry counted as duplicates" || bad "retry ($code)"

sleep 3  # metrics asOf is floored to 2 s
TOTAL=$(gql alice 'query($s:ID!){metrics(serviceId:$s,range:LAST_15M){totalRequests errorRate5xx p95Ms}}' "{\"s\":\"$SVC_A\"}" | jq '.data.metrics.totalRequests')
[ "${TOTAL:-0}" -ge 25 ] && ok "metrics reflect ingested events ($TOTAL)" || bad "metrics ($TOTAL)"

CODE=$(gql alice 'query($s:ID!){service(id:$s){id}}' "{\"s\":\"$SVC_B\"}" | jq -r '.errors[0].extensions.code')
[ "$CODE" = NOT_FOUND ] && ok "alice cannot read service B" || bad "cross-service read ($CODE)"
CODE=$(gql victor 'mutation($s:ID!){registerRoute(serviceId:$s,template:"/x"){id}}' "{\"s\":\"$SVC_A\"}" | jq -r '.errors[0].extensions.code')
[ "$CODE" = FORBIDDEN ] && ok "viewer mutation denied" || bad "viewer mutation ($CODE)"

gql alice 'mutation($id:ID!){revokeIngestToken(id:$id){revokedAt}}' "{\"id\":\"$TOKEN_ID\"}" > /dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/ingest/events" -H "Authorization: Bearer $(cat "$TMP/ingest.secret")" -H 'Content-Type: application/json' --data @"$TMP/batch.json")
[ "$code" = 401 ] && ok "revoked token rejected" || bad "revoked token ($code)"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/logout" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' -b "$TMP/alice.cookies" --data '{}')
[ "$code" = 204 ] && ok "logout" || bad "logout ($code)"
CODE=$(gql alice '{me{id}}' | jq -r '.errors[0].extensions.code')
[ "$CODE" = UNAUTHENTICATED ] && ok "token rejected after logout" || bad "post-logout ($CODE)"

echo "smoke: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
