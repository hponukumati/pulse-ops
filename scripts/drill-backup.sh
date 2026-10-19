#!/usr/bin/env sh
# Backup/restore drill with synthetic data: backup → change live state (delete a service,
# revoke a grant) → isolated restore-check must reapply the deletion and flag the stale grant.
set -eu
cd "$(dirname "$0")/.."
BASE=${PULSEOPS_BASE_URL:-http://127.0.0.1:8080}; CREDS=${PULSEOPS_DEMO_CREDENTIALS:-.demo-credentials.json}
umask 077; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT INT TERM
. ./scripts/lib.sh
[ -f "${PULSEOPS_BACKUP_KEY_DIR:-$HOME/.pulseops-backup-keys}/identity.txt" ] || ./scripts/backup-keygen.sh
login_as admin; reauth_as admin
DOOMED=$(gql_as admin 'mutation{createService(input:{name:"drill-doomed",environment:"synthetic-drill"}){id}}' | jq -r .data.createService.id)
VICTOR=$(jq -r .accounts.victor.id "$CREDS"); CHECKOUT=$(jq -r '.services["checkout-api"]' "$CREDS")
./scripts/backup.sh
gql_as admin 'mutation($id:ID!){deleteService(id:$id)}' "{\"id\":\"$DOOMED\"}" >/dev/null
gql_as admin 'mutation($u:ID!,$s:ID!){revokeServiceAccess(userId:$u,serviceId:$s)}' "{\"u\":\"$VICTOR\",\"s\":\"$CHECKOUT\"}" >/dev/null
i=0; until [ "$(psql_q "SELECT status FROM deletion_jobs WHERE service_id = '$DOOMED'")" = DONE ] || [ $i -ge 30 ]; do sleep 2; i=$((i + 1)); done
echo "live: service $DOOMED purged after backup; viewer grant revoked after backup"
./scripts/restore-check.sh | tee "$TMP/rc.txt"
REPORT=$(ls -t backups/*.restore-check.json | head -1)
[ "$(jq .reconcile.deletionsReapplied "$REPORT")" -ge 1 ] && ok "post-backup deletion reapplied on restore" || bad "deletion not reapplied"
[ "$(jq .stale_grants "$REPORT")" -ge 1 ] && ok "stale grant detected for review" || bad "stale grant not detected"
gql_as admin 'mutation($u:ID!,$s:ID!){grantServiceAccess(userId:$u,serviceId:$s){userId}}' "{\"u\":\"$VICTOR\",\"s\":\"$CHECKOUT\"}" >/dev/null
echo "drill: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
