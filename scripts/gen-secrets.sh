#!/usr/bin/env sh
# Generate local runtime secrets for Docker Compose into infra/secrets/ (gitignored).
# Dependencies: POSIX sh, openssl. Windows: run from WSL.
# Never commit the output. Re-running refuses to overwrite unless --force is given.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR="$ROOT/infra/secrets"
FORCE=${1:-}

if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && [ "$FORCE" != "--force" ]; then
  echo "infra/secrets already exists; refusing to overwrite (use --force to regenerate)." >&2
  exit 1
fi

umask 077
mkdir -p "$DIR"
chmod 700 "$DIR"

rand_pw() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-40; }
rand_key() { openssl rand -base64 32; }

for name in postgres_superuser_password db_migrator_password db_app_password db_maint_password redis_password; do
  rand_pw > "$DIR/$name"
done
rand_key > "$DIR/refresh_hmac_key"
rand_key > "$DIR/rate_limit_key"
printf '{"current": 1, "keys": {"1": "%s"}}\n' "$(rand_key)" > "$DIR/ingest_hmac_keys.json"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$DIR/jwt_private_key.pem" 2>/dev/null

# Redis ACL: disable the default user; the app user gets only the command groups it needs.
printf 'user default off\nuser pulseops on >%s ~* &* -@all +@read +@write +@scripting +@pubsub +@connection +time -@dangerous -script|flush -script|kill\n' \
  "$(cat "$DIR/redis_password")" > "$DIR/redis_users.acl"

# Docker Desktop bind-mounts secrets with host permissions; containers run as non-root.
# The directory stays 0700 on the host; files are readable inside the containers only.
chmod 644 "$DIR"/*
echo "Generated secrets in infra/secrets/ (do not commit)."
