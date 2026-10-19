# Architecture

PulseOps is one backend codebase (API + dedicated worker + admin CLI), one React frontend, PostgreSQL (source of truth) and Redis (rate limits and incident notifications; disposable). Scope: one personal workspace, up to 20 services, a few provisioned users.

```
browser ──HTTPS(hosted)/HTTP(local)──▶ nginx edge (frontend container, :8080 on 127.0.0.1)
                                         │  static SPA, security headers, edge rate limits, URL-free logs
                                         ├──▶ /api/v1/auth/*  ─┐
                                         ├──▶ /api/v1/ingest/events ─┤  FastAPI (api container)
                                         ├──▶ /graphql (POST + WebSocket)┤  ── policy layer ── services ── repositories
                                         └──▶ /health/*      ─┘           │               │
emitters ──Bearer poi_…──▶ ingest ─────────────────────────────────────────┤        PostgreSQL 18 (runtime role)
                                                                           │        Redis 8 (limits, pub/sub)
worker container: evaluator · outbox dispatcher · deletion jobs · retention ┘
```

Networks: `backend` is internal (PostgreSQL, Redis, API, worker, migrations); `edge` connects nginx to the API. Only nginx publishes a port, bound to `127.0.0.1`.

## Backend layout (`backend/src/pulseops`)

| Module | Responsibility |
|---|---|
| `config.py` | Settings; secrets only from `*_FILE` paths; `validate_for_startup` rejects unsafe hosted config (debug, demo mode, http origins, missing/placeholder/short/reused keys, weak Argon2 params). |
| `policy.py` | **The** authorization layer: roles × actions × explicit service grants, `NotFound` for absent/inaccessible, `Forbidden` for role denial, 5-minute reauthentication gate. Called by REST, every resolver, loaders, subscriptions and the CLI. |
| `api/` | FastAPI app, pure-ASGI middleware (request ID, 5 s timeout, security headers, allowlisted access log), auth routes, ingest route, custom GraphQL POST endpoint, own `graphql-transport-ws` handler. |
| `gql/` | Strawberry types/operations, per-operation context with DataLoaders, query limits (single operation, depth 8, 5 aliases, 20 fragments, variable-aware cost ≤ 1000), safe error formatting. |
| `services/` | Business logic and transactions: auth, catalog (services/routes/tokens), ingest, metrics, alerts, incident state machine, admin. |
| `repositories/` | SQL access (SQLAlchemy 2 async, asyncpg). Parameterized queries only. |
| `workers/` | Evaluator, outbox dispatcher, deletion jobs, retention, worker main loop with graceful shutdown. |
| `cli.py` | Protected host-operator CLI (`pulseops-admin`): accounts, password reset, grants, demo/benchmark seeding (local demo mode only), post-restore reconciliation, schema export. |
| `logging.py` | Allowlisted JSON logging; third-party messages dropped; exceptions reduced to class names. |

Each resolver and each DataLoader batch opens its own `AsyncSession`; sessions are never shared across concurrently executing sibling resolvers or held in subscription state. Migrations run as a separate schema-owner role; the runtime role is not superuser or owner and cannot modify or delete audit rows. Audit retention uses a third, maintenance role.

## Key flows

**Ingestion** (`services/ingest.py`). Order: 415 content type/encoding → 401 credential (HMAC-SHA-256 digest, constant-time compare, key-versioned) → per-token request bucket → streamed 1,048,576-byte cap (413, also for chunked bodies) → strict JSON (duplicate keys, NaN/Infinity rejected → 400) → Pydantic strict whole-batch validation (422 with index/field/code only) → per-service and global event buckets (429 + `Retry-After`) → one transaction: `FOR SHARE` on token and service rows (revocation/deletion take `FOR UPDATE`, giving a defined order), route check, `INSERT … ON CONFLICT DO NOTHING RETURNING`, digest comparison for existing IDs (409 rolls back the batch), quota counter update (429 rolls back) → `COMMIT` → 200 `{accepted, duplicate, requestId}`. Inserts are sorted by event ID to avoid deadlocks between overlapping batches.

**Metrics** (`services/metrics.py`). One `asOf` per operation (floored to 2 s). Windows `[start, end)` with read-time retention (`start = max(asOf − range, asOf − retention)`). Exact `percentile_cont` over raw events; buckets via `date_bin`, clipped at partial edges with their durations returned. Aggregates may be cached in Redis for 10 s under a key containing service, range, route and `asOf`, and only after the caller has been authorized and the service/route existence rechecked.

**Authentication** (`services/auth.py`). Argon2id (46 MiB, t=2, p=1 by default; minimum enforced 19 MiB/t=2/p=1), NFKC normalization, dummy-hash verification for unknown users, per-account failure and per-IP attempt windows in Redis (keys are HMACs, never raw emails/IPs). Access JWT: RS256, 15 min (never beyond the 12 h session), pinned `kid` keyring, all registered claims required, 30 s skew. Refresh: 256-bit opaque secret, keyed digest stored, rotated under a row lock; reuse of a spent token revokes the session family. Every protected operation reloads account, session and grants from PostgreSQL.

**Alert worker** (`workers/evaluator.py`). For slot `S` the window ends at `S − 60 s`. Per rule, one transaction: lock rule row → skip if `last_slot ≥ S` → compute metric → streak update (gap or ineligible data resets) → insert unique `(rule_id, rule_version, scheduled_at)` evaluation → transition + `incident_events` row + `outbox_events` row → commit. A partial unique index enforces one active incident per rule. After downtime only the latest slot is evaluated. If ≥ 10,000 outbox rows are pending, evaluations pause with an explicit stale state.

**Outbox → live updates** (`workers/dispatcher.py`, `api/ws.py`). The dispatcher claims rows with `FOR UPDATE SKIP LOCKED`, publishes `{serviceId, incidentId, eventId, version}` to Redis, and marks them sent; failures back off exponentially with jitter (≤ 60 s). Delivery is at least once. Each API process runs one Redis subscriber that fans out to WebSocket subscriptions; before every delivery the principal is reloaded from PostgreSQL and the incident refetched; duplicates (same or older version) are dropped. Clients refetch on reconnect and every 30 s because Pub/Sub can miss messages.

**Service deletion**: immediately marks the service deleted, revokes tokens, disables rules and enqueues a retryable job; the worker appends `{serviceId, deletedAt}` to a ledger on a separate volume (outside DB backups), then purges events (chunked, quota-adjusted), evaluations, outbox, transitions, incidents, rules, tokens, routes, grants, the service row and cached aggregates.

## Frontend (`frontend/src`)

React 19 + strict TypeScript + Vite, Apollo Client 4 (in-memory cache only, one client per session, discarded on logout/identity change), graphql-ws for subscriptions, Recharts. The access token lives only in module memory; the refresh secret is an HttpOnly SameSite=Strict cookie; refreshes are serialized across tabs with the Web Locks API and logout is broadcast to other tabs. Dashboards poll every 5 s while visible, pause when hidden, refetch on reconnect, and never render data from a previous selection. GraphQL operations are type-generated and validated against `docs/schema.graphql`.

## Data model

Tables (migrations `0001_core`, `0002_alerting`): `users`, `sessions`, `refresh_tokens`, `services`, `service_grants`, `routes`, `ingest_tokens`, `request_events`, `audit_events`, `deletion_jobs`, `alert_rules`, `rule_evaluations`, `incidents`, `incident_events`, `outbox_events`, plus small support tables `ingest_quota` (retained-event counter), `app_settings` (retention days) and `worker_heartbeats`. Service-scoped children reference parents through composite `(service_id, id)` foreign keys. Indexes include `(service_id, occurred_at)`, `(service_id, route_id, occurred_at)` and `(occurred_at)` on events.
