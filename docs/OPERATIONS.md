# Operations

All commands run from the repository root with Docker Compose v2. Windows users: run the shell scripts from WSL.

## Secrets

`./scripts/gen-secrets.sh` creates `infra/secrets/` (directory 0700, gitignored): bootstrap superuser, migrator, runtime and maintenance DB passwords; Redis ACL user; RSA-3072 JWT signing key; refresh HMAC key; rate-limit key; versioned ingest HMAC keyring. Compose mounts them as file secrets under `/run/secrets`; the app reads only `*_FILE` paths. Nothing secret is placed in source, images, command arguments, environment values or frontend build variables. Hosted deployments should use the host's secret facility with the same file layout.

## Accounts (protected host CLI)

There is no public signup or password-reset endpoint. Passwords are read without echo (or from stdin with `--password-stdin`), never from arguments.

```bash
docker compose exec api pulseops-admin create-user --email owner@example.org --role ADMIN
docker compose exec api pulseops-admin reset-password --email someone@example.org   # revokes all sessions, audited
docker compose exec api pulseops-admin set-role --email someone@example.org --role VIEWER
docker compose exec api pulseops-admin disable-user --email someone@example.org     # --enable to re-enable
docker compose exec api pulseops-admin grant --email someone@example.org --service-id <uuid> [--revoke]
```

**Demo mode** (`./scripts/demo-up.sh`) sets `PULSEOPS_DEMO_MODE=true` and seeds synthetic accounts with random passwords written to `.demo-credentials.json` (0600, gitignored). Seeding refuses to run unless `PULSEOPS_ENV=local` and demo mode is on; hosted startup refuses demo mode.

## Route template review

Syntax checks reject URLs, query/fragment markers, escapes, emails, UUIDs, long numeric or hex identifiers, and token-like segments, but cannot prove free text contains no sensitive data. Review registered templates and service names periodically (Settings → Routes). Retire templates instead of editing them.

## Backups and restore

```bash
./scripts/backup-keygen.sh          # once: identity -> ~/.pulseops-backup-keys/identity.txt (keep separate!)
./scripts/backup.sh /path/to/private/backup-dir
./scripts/restore-check.sh /path/to/private/backup-dir/pulseops-<UTC>.dump.age
./scripts/health-report.sh          # backup age/failures, readiness, worker/outbox state
```

- Backups are `pg_dump` custom format encrypted with `age` to a public recipient; only the identity (kept outside the repo and away from the backups) can decrypt. A manifest stores counts, per-service aggregates and the SHA-256 — no row data.
- **Schedule** for non-disposable data: daily to a destination separate from the database disk, e.g. cron `15 3 * * * cd /srv/pulseops && ./scripts/backup.sh /mnt/backups >> /var/log/pulseops-backup.log 2>&1`. Copies older than 14 days are pruned. Daily backups imply up to about one day of data loss; there is no 15-minute RPO or HA promise. Local synthetic demos may skip scheduling.
- **Restore checks** run in an isolated PostgreSQL container on an internal Docker network. Repeat after schema or backup changes and monthly while meaningful data is retained.
- Manifest counts are captured just before the dump; concurrent writes can make them differ slightly — run checks when ingestion is quiet.

### Restoring for real (recovery)

1. Keep public access disabled (stop `frontend`, or block the edge).
2. Restore into a fresh volume: `restore-check.sh` proves the backup; then restore the same file into the production database with `pg_restore` as the schema owner.
3. Run `docker compose exec worker pulseops-admin restore-reconcile` (the worker mounts the deletion-ledger volume) — revokes **all** sessions and ingest tokens, enforces retention, and reapplies service deletions recorded in the deletion ledger (`ledger` volume, kept ≥ 15 days, outside DB backups). It prints users and grants for review.
4. Compare grants with your known-good configuration (`restore-check.sh` prints a stale-grant diff against the live system). If permissions or deletions cannot be reconciled, keep access disabled and re-provision accounts/grants from the owner's known-good configuration.
5. Issue new ingest tokens to emitters, then reopen access. Never expose a restored older snapshot directly.

Deletion is not instant erasure from backups: copies made before a deletion exist until they expire (14 days).

## Key rotation and compromise

| Key | Rotation | If compromised |
|---|---|---|
| JWT signing key | Generate a new key, set `PULSEOPS_JWT_KID` (e.g. `k2`), move the old public key to `PULSEOPS_JWT_PREVIOUS_PUBLIC_KEY_FILE`/`_KID`, restart; remove the previous key after 15 minutes | Rotate **without** keeping the old key, then revoke all sessions (`UPDATE sessions SET revoked_at = now()` as the schema owner, or `restore-reconcile`) |
| Refresh HMAC key | Replace and restart: all refresh tokens become invalid (users log in again) | Same, plus revoke sessions |
| Ingest HMAC keyring | Add version `n+1`, set `"current": n+1`, restart; new tokens use it, old tokens keep verifying; rotate each service's token, then remove the old version | Remove the compromised version (its tokens stop verifying) and issue new tokens; review audit for token use |
| DB / Redis passwords | `ALTER ROLE … PASSWORD` / update the ACL file, update secret files, restart | Same, then review access |
| Backup identity | Generate a new pair; re-encrypt or let old backups expire | Treat existing backups as exposed; generate new keys |

Lost keys: a lost JWT or HMAC key is recovered by generating a new one (sessions/tokens are reissued); a lost backup identity makes existing backups unrecoverable — keep a protected copy.

## Migrations

`migrate` runs `alembic upgrade head` with the migrator role before the API/worker start. Migrations are tested on an empty database and as an upgrade from the prior revision. Downgrades are deliberately not implemented (they would be destructive); to roll back, restore the pre-upgrade backup into a fresh volume and run the previous image. Take a backup before every upgrade.

## Retention and storage

| Data | Retention |
|---|---|
| Events | 7 days by event time (ADMIN may lower to 1–7); hourly chunked purge; read-time filtering hides expired rows immediately |
| Resolved incidents/transitions, audit | 30 days (audit purge uses the maintenance role) |
| Rule evaluations | 7 days |
| Sent outbox | 24 hours |
| Expired sessions + refresh replay records | purged hourly after absolute expiry |
| Deletion ledger | 30 days |
| Container logs | json-file driver, 3 × 10 MB per container (size-bounded). For a 7-day policy on a host, use journald (`MaxRetentionSec=7day`). |
| Backups | 14 days |

Retained events are capped at 1,000,000 per deployment; ingestion returns 429 `QUOTA_EXCEEDED` rather than evicting unexpired data.

## Reliability drills

`./scripts/drill-reliability.sh` (Redis outage during a transition, two workers + SIGKILL, restart persistence) and `./scripts/drill-backup.sh` (backup → post-backup deletion and grant change → isolated restore and reconciliation). Both run in CI.

## Hosted mode (not verified in this repository)

Hosting was **not** deployed or tested here. Requirements before any hosted use: HTTPS/WSS with automatic certificate renewal and TLS ≥ 1.2 in front of the nginx container; one origin for frontend and API; `PULSEOPS_ENV=hosted` with `https://` allowed origins (startup refuses debug, demo mode, HTTP origins, missing/placeholder secrets); HSTS enabled in `frontend/nginx/security-headers.conf` once HTTPS works; `PULSEOPS_TRUSTED_PROXIES` set to the actual proxy address; TLS with certificate verification (`PULSEOPS_DB_SSL`, `PULSEOPS_REDIS_SSL`) if DB/Redis cross hosts; encrypted host volumes and backups; scheduled backups with alerting on `health-report.sh` failures. A reviewer instance should use an isolated synthetic dataset and individually provisioned VIEWER accounts; never publish administrator or ingest credentials.
