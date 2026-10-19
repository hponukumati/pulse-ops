#!/usr/bin/env sh
# Fail if logs/test artifacts contain credentials or sensitive-data canaries:
# ingest tokens, JWTs, private keys, refresh cookie values, demo passwords, CANARY markers.
# Usage: ./scripts/scan-artifacts.sh PATH...
set -eu
cd "$(dirname "$0")/.." 2>/dev/null || true
FOUND=0
PATTERNS='poi_[a-z2-7]{20}_[A-Za-z0-9_-]{43}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.|-----BEGIN [A-Z ]*PRIVATE KEY-----|pulseops_refresh(_dev)?=[A-Za-z0-9_-]{20,}|CANARY-(SECRET|BODY)-|AGE-SECRET-KEY-'
for p in "$@"; do
  [ -e "$p" ] || continue
  if grep -rEIl "$PATTERNS" "$p" >/dev/null 2>&1; then
    echo "credential-like content found in: $(grep -rEIl "$PATTERNS" "$p" | tr '\n' ' ')" >&2
    FOUND=1
  fi
  if [ -f .demo-credentials.json ]; then
    for pw in $(jq -r '.accounts[].password' .demo-credentials.json 2>/dev/null); do
      if grep -rIlF -- "$pw" "$p" >/dev/null 2>&1; then echo "demo password found in $p" >&2; FOUND=1; fi
    done
  fi
done
[ "$FOUND" -eq 0 ] && echo "scan-artifacts: clean ($*)"
exit "$FOUND"
