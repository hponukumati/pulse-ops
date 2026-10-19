# PulseOps

A small, security-focused API observability app (personal portfolio project). Emitters send **minimal** request measurements (six fields, no payloads/headers/URLs); PulseOps shows latency/error/traffic metrics, evaluates alert rules in a dedicated worker, and pushes incident changes live — with role-based, per-service isolation.

Stack: Python 3.13 · FastAPI · Strawberry GraphQL · SQLAlchemy 2 (async, asyncpg) · Alembic · PostgreSQL 18 · Redis 8 · React 19 + TypeScript + Vite · Apollo Client 4 · Recharts · nginx · Docker Compose · pytest/testcontainers · Vitest · Playwright · GitHub Actions.

> Scope: one personal workspace, ≤ 20 services, a few provisioned users, synthetic or personal non-sensitive data. Not a multi-tenant SaaS and not a claim of enterprise readiness. See [SECURITY.md](SECURITY.md).

## Quick start (local, synthetic demo)

Requires Docker with Compose v2 (no host Python/Node needed). Windows: use WSL.

```bash
./scripts/gen-secrets.sh
```

```bash
./scripts/demo-up.sh
```

Open <http://127.0.0.1:8080> and sign in with an account from `.demo-credentials.json` (random per run, local only, gitignored): `alice` (OPERATOR, checkout-api), `bob` (OPERATOR, billing-api), `victor` (VIEWER, checkout-api), `admin`.

```bash
./scripts/smoke.sh
```

`smoke.sh` (POSIX sh + curl + jq) exercises login → token → ingest → metrics → isolation → revocation → logout through the nginx edge.

Without demo mode, start with `docker compose up --build -d` and create accounts with the protected CLI (`docs/OPERATIONS.md`).

## What you can demo

1. **Real data round trip** — Settings → register a route → create an ingest token (password confirmation, shown once) → send events (`curl` example below) → Metrics shows totals, error rates, exact p50/p95/p99, buckets and top endpoints.
2. **Isolation** — Alice cannot see billing-api by direct ID, nested field, cursor reuse or subscription; Victor sees checkout-api but every mutation is denied in the UI and the API.
3. **Reliable alerting** — create a 5xx-rate rule, send failing traffic; after two consecutive breaching 60 s slots the worker opens an incident that appears live; acknowledge, then resolve.
4. **Failure recovery** — `./scripts/drill-reliability.sh`: Redis stopped during a transition (incident + outbox commit, dispatch recovers), two workers + SIGKILL (no duplicate slot/transition), restart persistence.
5. **Backups** — `./scripts/drill-backup.sh`: encrypted backup → post-backup deletion and grant change → isolated restore check that reapplies the deletion and flags the stale grant.

```bash
curl -s http://127.0.0.1:8080/api/v1/ingest/events \
  -H "Authorization: Bearer $PULSEOPS_INGEST_TOKEN" -H 'Content-Type: application/json' \
  -d '[{"eventId":"'"$(uuidgen | tr A-Z a-z)"'","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'","method":"GET","routeId":"<route-id>","statusCode":200,"durationMs":42.5}]'
```

## Try it with a live service

[`examples/fastapi-emitter/demo_app.py`](examples/fastapi-emitter/demo_app.py) is a small FastAPI service that reports every request it handles. Its middleware looks up the matched route template (never the raw URL), skips routes that are not registered, and sends batches every 2 s; on network errors, 429 or 503 it backs off and retries with the same event IDs. It needs [uv](https://docs.astral.sh/uv/) and must run on the same host as PulseOps.

1. Sign in as an operator, open the service → Settings, copy the IDs of two routes (in the demo, `checkout-api`'s `/health` and `/orders/{orderId}`), and create an ingest token.
2. Configure it (the token prompt does not echo) and start it on port 9000:

```bash
printf 'Ingest token: '; stty -echo; read -r PULSEOPS_INGEST_TOKEN; stty echo; echo; export PULSEOPS_INGEST_TOKEN
```

```bash
export HEALTH_ROUTE_ID=<health-route-id> ORDERS_ROUTE_ID=<orders-route-id>
```

```bash
uv run examples/fastapi-emitter/demo_app.py
```

3. Send it traffic and watch the Metrics tab. To trigger a 5xx-rate rule, restart it with `FAIL_RATE=0.3` (default 0.02).

```bash
while true; do curl -s -o /dev/null "http://127.0.0.1:9000/orders/$RANDOM"; sleep 0.2; done
```

To instrument your own service, copy the middleware and sender. Report only registered routes and the seven accepted methods, because one invalid event rejects the whole batch.

## Documentation

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data model, key flows (ingest, auth, metrics, worker, outbox) |
| [docs/API.md](docs/API.md) · [docs/schema.graphql](docs/schema.graphql) | REST/GraphQL/WebSocket contract, limits, error codes |
| [SECURITY.md](SECURITY.md) | Controls, one-page threat model, scan exceptions |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Secrets, accounts CLI, backups/restore, key rotation, migrations, retention, hosting requirements |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | Requirement → check map, commands run, PASS/FAIL/BLOCKED/NOT RUN results |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Workload, hardware, measured latency/throughput/errors, limitations |

Demo walkthrough: the Playwright spec [`frontend/e2e/journey.spec.ts`](frontend/e2e/journey.spec.ts) is the scripted walkthrough of the product journey (no video is included in this repository).

## Development

```bash
cd backend && uv sync && uv run pytest
```

```bash
cd frontend && npm ci && npm run codegen:check && npm run typecheck && npm test
```

```bash
cd frontend && npx playwright install chromium && npx playwright test
```

Integration tests start PostgreSQL and Redis with testcontainers (Docker required). CI: [.github/workflows](.github/workflows).
