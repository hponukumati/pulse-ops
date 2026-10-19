# Verification record

Local run on 2026-10-18 (Apple M3, Docker Desktop 29.6.2). Statuses: **PASS** (command run, evidence below), **FAIL**, **BLOCKED**, **NOT RUN**. Raw logs are in `artifacts/` (generated, gitignored). No real credentials or data were used; all accounts and telemetry are synthetic.

| Area (PRD) | Check | Command | Status |
|---|---|---|---|
| Backend lint/format/types | ruff, ruff format (incl. `examples/`), mypy (51 files) | `cd backend && uv run ruff check … && uv run mypy` | PASS |
| Backend unit (228) | validation, tokens/JWT, policy matrix, streaks, buckets, config, logging, passwords, cost limits, middleware, deadlock mapping, example emitter | `uv run pytest tests/unit` | PASS |
| Backend integration (88, real PostgreSQL 18.6 + Redis 8.10.2) | migrations (empty + upgrade) and model drift, least-privilege roles, auth, ingest, access control, metrics, worker/outbox, WebSocket, canaries, timeouts, example emitter → real ingest | `uv run pytest tests/integration` | PASS (316 total, 0 skipped) |
| Canary negative control | deliberately leaky error formatting must fail the canary test | manual mutation, reverted | PASS (test failed as expected) |
| Frontend | codegen check vs `docs/schema.graphql`, `tsc` strict, Vitest (13), production build | `npm run codegen:check && npm run typecheck && npm test && npm run build` | PASS |
| Clean setup + smoke | `down -v` → `demo-up.sh` → 14 checks through nginx | `./scripts/smoke.sh` | PASS (14/14) |
| E2E walkthrough | Playwright: journey (route → token → ingest → metrics → rule → worker-opened incident → ack → resolve), viewer denial UI+API, logout/identity switch, storage, 375 px | `npx playwright test` | PASS (5/5) |
| Reliability | Redis outage during transition; two workers + SIGKILL; restart persistence | `./scripts/drill-reliability.sh` | PASS (11/11) |
| Backup/restore | encrypted backup, isolated restore, migrations/counts/aggregates/FKs, session+token revocation, deletion replay, stale-grant detection | `./scripts/drill-backup.sh` | PASS (restore-check 11/11, drill 2/2) |
| Owner health report | backup age/failures, readiness, worker/outbox | `./scripts/health-report.sh` | PASS |
| Data minimization in artifacts | credential/canary patterns in compose logs, Playwright results, pytest output | `./scripts/scan-artifacts.sh …` | PASS |
| Secret scan | gitleaks 8.30.1 over repository files (gitignored secrets excluded; negative control on the secrets dir detected) | see `.github/workflows/security.yml` | PASS (after replacing 3 synthetic-fixture false positives) |
| Python advisories | pip-audit 2.9.0 over the full lockfile | `uvx pip-audit --strict -r …` | PASS (no known vulnerabilities) |
| npm advisories | production deps / all deps | `npm audit --omit=dev` / `npm audit` | PASS / documented dev-only exception (SECURITY.md) |
| Container scan | trivy 0.76.0, HIGH/CRITICAL with fixes, on built images | `trivy image …` | PASS after hardening (pip removed, `apk upgrade`, gosu removed); postgres base image exception documented |
| Workflow lint | actionlint | `docker run rhysd/actionlint` | PASS |
| Benchmark | PRD §12 workload | `uv run scripts/benchmark.py` | PASS (see BENCHMARK.md) |
| GitHub Actions on GitHub | CI workflow, first run on commit `85324d3` | push to `main` | **FAIL** — backend and frontend PASS; e2e: smoke, Playwright, reliability drills PASS, backup drill FAIL (key files unreadable by the container user on Linux; image scan skipped). Fixed (see below); re-run pending the next push |
| Hosted deployment | HTTPS/WSS, certificates, hosted config on a server | — | **NOT RUN** (config validation tested only) |
| Demo video | — | — | **NOT RUN** (Playwright spec is the scripted walkthrough) |

## Fixes made because of verification/review

Duplicate dialog IDs (a11y), WebSocket buffer accounting on unsubscribe, swallowed errors after reauthentication, inaccurate `totalCount` for grants/audit, nginx entrypoint log noise, uvicorn logs bypassing the allowlist, raw `OSError` reaching the 500 handler, crude GraphQL cost model, **ingest/rotation lock-order deadlock risk** (ingest now locks service before token; deadlock/serialization SQLSTATEs map to 503), image CVEs.

## Final authoritative run

After the last change (ingest lock order): backend 307 passed; images rebuilt; trivy clean on all three built images; clean setup → smoke 14/14, Playwright 5/5, reliability drill 11/11, backup drill 2/2 (restore-check 11/11), health report OK, artifact scan clean (`artifacts/final-verification-3.log`, 2026-10-18 09:03–09:12 UTC).

Added afterwards (no application code changed): the example emitter `examples/fastapi-emitter` with 8 unit tests and 1 integration test (its batches stored by the real ingest endpoint); a negative control removing its method filter failed the unit test as expected. Re-run: backend 316 passed, ruff/format (now including `examples/`), mypy and actionlint clean, and the README `uv run` command started the example and served requests. The stack-level checks above were not re-run for this addition.

First GitHub CI run ([run 39188133324](https://github.com/hponukumati/pulse-ops/actions/runs/39188133324)): the backup drill failed on the Linux runner because `backup-keygen.sh` created the public recipient file 0600 for the runner user, while the backup container runs as `postgres` (Docker Desktop on macOS does not enforce bind-mount ownership, so local runs passed). Reproduced on a Linux filesystem with the backup image, including the same failure for the private identity in `restore-check.sh`. Fix: the recipient (public) is made 0644; the decrypt container runs as the identity's owner (`--user $(id -u):$(id -g)`), so the identity stays 0600. Local re-run: keygen permissions checked (recipient 0644, identity 0600, directory 0700); backup drill 2/2 with restore-check 11/11. Confirmation on Linux needs the next CI run.

## Known limitations

See README and SECURITY.md. Notably: single API process; benchmark client on the same host; container logs are size-rotated (not 7-day time-based) locally; JS bundle ~900 KB unsplit; `ingest_tokens` "max 2 active" enforced by row locks, not a DB constraint; SQL cancellation on request cancellation verified at the session level, not via client disconnect; manifest counts in backups are captured just before the dump (not the same snapshot).
