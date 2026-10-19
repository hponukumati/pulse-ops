# Shared helpers for the drill scripts (POSIX sh). Source after setting BASE/CREDS/TMP.
# Secrets are kept in $TMP (0700, removed on exit) and never printed.
PASS=${PASS:-0}; FAIL=${FAIL:-0}
ok() { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1"; }
uuid() { if command -v uuidgen >/dev/null; then uuidgen | tr 'A-Z' 'a-z'; else cat /proc/sys/kernel/random/uuid; fi; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
psql_q() { docker compose exec -T postgres psql -U postgres -d pulseops -At -c "$1"; }
login_as() { # $1 = account name in the credentials file
  jq -n --arg e "$(jq -r ".accounts.$1.email" "$CREDS")" --arg p "$(jq -r ".accounts.$1.password" "$CREDS")" '{email:$e,password:$p}' > "$TMP/l.json"
  curl -s -X POST "$BASE/api/v1/auth/login" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' --data @"$TMP/l.json" | jq -r .accessToken > "$TMP/$1.token"
  rm -f "$TMP/l.json"
}
reauth_as() {
  jq -n --arg p "$(jq -r ".accounts.$1.password" "$CREDS")" '{password:$p}' > "$TMP/r.json"
  curl -s -o /dev/null -X POST "$BASE/api/v1/auth/reauthenticate" -H "Origin: $BASE" -H 'X-PulseOps-CSRF: 1' -H 'Content-Type: application/json' -H "Authorization: Bearer $(cat "$TMP/$1.token")" --data @"$TMP/r.json"
  rm -f "$TMP/r.json"
}
gql_as() { # $1 account, $2 query, $3 variables
  vars=${3:-}; [ -n "$vars" ] || vars='{}'
  jq -n --arg q "$2" --argjson v "$vars" '{query:$q,variables:$v}' > "$TMP/q.json"
  curl -s -X POST "$BASE/graphql" -H "Authorization: Bearer $(cat "$TMP/$1.token")" -H 'Content-Type: application/json' --data @"$TMP/q.json"
}
batch_file() { # $1 route, $2 count, $3 count of 5xx -> writes $TMP/batch.json
  i=0; printf '[' > "$TMP/batch.json"
  while [ "$i" -lt "$2" ]; do
    st=200; [ "$i" -lt "$3" ] && st=503
    [ "$i" -gt 0 ] && printf ',' >> "$TMP/batch.json"
    printf '{"eventId":"%s","timestamp":"%s","method":"GET","routeId":"%s","statusCode":%s,"durationMs":%s}' "$(uuid)" "$(now)" "$1" "$st" "$((10 + i))" >> "$TMP/batch.json"
    i=$((i + 1))
  done; printf ']' >> "$TMP/batch.json"
}
setup_breaching_service() { # creates service+route+token as admin, ingests 40 events (75% 5xx), creates rule; sets SVC, RULE
  login_as admin; reauth_as admin
  name="drill-$(date +%s | tr '0-9' 'a-j')"
  SVC=$(gql_as admin 'mutation($n:String!){createService(input:{name:$n,environment:"synthetic-drill"}){id}}' "{\"n\":\"$name\"}" | jq -r .data.createService.id)
  ROUTE=$(gql_as admin 'mutation($s:ID!){registerRoute(serviceId:$s,template:"/drill/{id}"){id}}' "{\"s\":\"$SVC\"}" | jq -r .data.registerRoute.id)
  gql_as admin 'mutation($s:ID!){createIngestToken(serviceId:$s){secret}}' "{\"s\":\"$SVC\"}" | jq -r .data.createIngestToken.secret > "$TMP/drill.secret"
  batch_file "$ROUTE" 40 30
  curl -s -o /dev/null -X POST "$BASE/api/v1/ingest/events" -H "Authorization: Bearer $(cat "$TMP/drill.secret")" -H 'Content-Type: application/json' --data @"$TMP/batch.json"
  RULE=$(gql_as admin 'mutation($s:ID!){createAlertRule(serviceId:$s,input:{name:"drill 5xx",metric:ERROR_RATE_5XX,comparator:GT,threshold:0.1,windowMinutes:5,minSamples:5}){id}}' "{\"s\":\"$SVC\"}" | jq -r .data.createAlertRule.id)
}
