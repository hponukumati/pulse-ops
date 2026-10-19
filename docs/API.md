# API

REST is used for authentication and ingestion; GraphQL (`POST /graphql`, `graphql-transport-ws` on the same path) for everything else. The full schema is exported to [`schema.graphql`](schema.graphql) (`pulseops-admin export-schema`); CI fails if it drifts or if frontend operations do not validate against it. Introspection and GraphiQL are disabled.

All private responses carry `Cache-Control: no-store`. Error bodies contain only fixed messages, codes, and (for validation) batch indices / field names — never input values, SQL, tokens or stack traces:

```json
{"error": {"code": "VALIDATION_FAILED", "message": "Invalid fields", "requestId": "…", "details": [{"index": 3, "field": "statusCode", "code": "invalid_type"}]}}
```

## Ingestion — `POST /api/v1/ingest/events`

`Authorization: Bearer poi_<public_id>_<secret>` (service-scoped ingest token; never stored or logged). Body: JSON array of 1–500 events, ≤ 1,048,576 bytes (enforced on the stream, including chunked bodies), `Content-Type: application/json`, no `Content-Encoding`.

| Field | Rule |
|---|---|
| `eventId` | canonical UUIDv4 string |
| `timestamp` | RFC 3339 with `Z` or `±hh:mm`; normalized to UTC (µs); ≤ 24 h old, ≤ 5 min ahead |
| `method` | `GET POST PUT PATCH DELETE HEAD OPTIONS` |
| `routeId` | active route of the token's service (absent/retired/other-service → same `unknown_route`) |
| `statusCode` | integer 100–599 (no bool/string/float) |
| `durationMs` | finite number 0–120000 (no bool/NaN/Infinity) |

Unknown properties, duplicate keys or any invalid item reject the whole batch. Success (only after COMMIT): `200 {"accepted": n, "duplicate": m, "requestId": "…"}` with `accepted + duplicate = submitted`. Exact retries are duplicates; the same `eventId` with different content returns 409 and rolls back the batch.

| Status | Meaning |
|---|---|
| 400 `MALFORMED_JSON` | not UTF-8 JSON, duplicate keys, NaN/Infinity |
| 401 `INVALID_CREDENTIAL` | missing/invalid/revoked/expired token, deleted service, user JWT |
| 409 `CONFLICT` | conflicting event ID (`details[].code = conflicting_event_id`) |
| 413 `PAYLOAD_TOO_LARGE` | body over the cap |
| 415 `UNSUPPORTED_MEDIA_TYPE` | wrong content type or compressed body |
| 422 `VALIDATION_FAILED` | invalid fields / batch shape / unknown route |
| 429 `RATE_LIMITED` / `QUOTA_EXCEEDED` | request/event budgets or 1,000,000 retained events (`Retry-After` set) |
| 503 `SERVICE_UNAVAILABLE` / `TIMEOUT` | PostgreSQL or Redis unavailable, statement timeout — nothing acknowledged |

Emitters should retry network failures with the **same event IDs** using exponential backoff with jitter.

## Authentication — `/api/v1/auth/*`

All are `POST` with `Content-Type: application/json`, an exact allowed `Origin`, and `X-PulseOps-CSRF: 1`; otherwise 403 `ORIGIN_REJECTED` / 415. Body limit 4 KiB, unknown fields rejected.

| Endpoint | Body | Notes |
|---|---|---|
| `login` | `{email, password}` | 200 `{accessToken, accessTokenExpiresAt, sessionExpiresAt, user{id, role}}` + refresh cookie; generic 401 for every failure; 429 after 5 failures/account/15 min or 30 attempts/IP/5 min |
| `refresh` | `{}` + cookie | rotates the cookie; spent-token reuse revokes the session family (401, cookie cleared) |
| `logout` | `{}` + cookie and/or bearer | revokes the session; clears the cookie (works even if Redis is down) |
| `password` | `{currentPassword, newPassword}` + bearer | 15–128 chars, blocklist; revokes the user's other sessions |
| `reauthenticate` | `{password}` + bearer | marks the session reauthenticated for 5 minutes (required for tokens, deletions, permissions, retention) |

Cookie: `__Host-pulseops_refresh` (Secure) when every allowed origin is HTTPS; `pulseops_refresh_dev` (not Secure) only for loopback HTTP in local mode. Always `HttpOnly; SameSite=Strict; Path=/`, no `Domain`, expiry ≤ session expiry. GraphQL never accepts the cookie.

Account creation and forgotten-password resets are **not** HTTP endpoints; they use the host CLI (`docs/OPERATIONS.md`).

## GraphQL

`POST /graphql`, `Authorization: Bearer <access JWT>`, body `{query, variables?, operationName?}` — one operation per request; arrays (batching), GET and uploads are rejected.

| Limit | Value |
|---|---|
| body | 64 KiB |
| depth / aliases / fragments | 8 / 5 / 20 |
| page size | default 20, max 100 |
| cost | ≤ 1000, computed with variable values before execution |
| response | ≤ 2 MiB |
| rate | 120 operations/min/user, 5 concurrent/user, 30 concurrent globally |

**Cost model:** each field costs 1, except `metrics` 50, `endpoints` 20, `diagnostics` 20, `health` 5, `transitions` 5, `ingestTokens` 5. For connection fields (`services`, `routes`, `alertRules`, `incidents`, `endpoints`, `users`, `serviceGrants`, `auditEvents`) the `edges` subtree is multiplied by `first` (default 20); `totalCount`/`pageInfo` are not.

Error `extensions.code` values: `UNAUTHENTICATED`, `FORBIDDEN` (role cannot perform the action on a service it can read), `NOT_FOUND` (absent **or** inaccessible — indistinguishable), `REAUTH_REQUIRED`, `VALIDATION_FAILED`, `CONFLICT`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE`, `TIMEOUT`, `QUERY_TOO_COMPLEX`, `QUERY_TOO_DEEP`, `TOO_MANY_ALIASES`, `INTROSPECTION_DISABLED`, `SINGLE_OPERATION_REQUIRED`, `GRAPHQL_VALIDATION_FAILED`, `INTERNAL`. If Redis is unavailable, mutations fail with 503 and reads fall back to 60 operations/min/user per process.

Operations: queries `me`, `systemStatus`, `services`, `service(id)`, `routes`, `alertRules`, `incidents`, `incident(id)`, `endpoints`, `metrics(serviceId, range, routeId)`; ADMIN `users`, `serviceGrants`, `auditEvents`, `diagnostics`. Mutations for services, routes, ingest tokens, rules, incident acknowledge/resolve, and ADMIN role/disable/grant/retention management. Subscription `incidentChanged(serviceId)`.

Metric ranges: `LAST_15M` (1 min buckets), `LAST_1H` (1 min), `LAST_24H` (15 min), `LAST_7D` (1 h). Rates are fractions; RPM uses the full window length; percentiles are exact `percentile_cont` over raw events and `null` when there is no data.

## WebSocket subscriptions

Subprotocol `graphql-transport-ws`, exact `Origin` required (otherwise the handshake is refused). Send `connection_init` with `{"authorization": "Bearer <JWT>"}` within 5 s (the token is never accepted in the URL or from cookies).

| Limit / behaviour | Value |
|---|---|
| connections per user | 2 (close 4429) |
| subscriptions per connection | 5 (`TOO_MANY_SUBSCRIPTIONS` error) |
| frame size | 16 KiB (close 1009) |
| client messages | 60/min (close 4429) |
| outbound buffer | 100 notifications or 1 MiB, then close 4499 (reconnect and refetch) |
| authorization | on subscribe and before every delivery; idle recheck every 30 s |
| JWT expiry | close 4498 (refresh the access token, reconnect) |
| revoked session/account | close 4403; revoked grant → subscription `error` `NOT_FOUND` |

## Health

`GET /health/live` → `{"status": "ok"}` (process responsiveness). `GET /health/ready` → 200 `{"status": "ready"}` or 503 `{"status": "not_ready"}` (PostgreSQL and Redis). Detailed diagnostics are the ADMIN-only `diagnostics` query.

## Edge limits (nginx)

Per address: auth 10 r/s (burst 20), ingest/GraphQL 50 r/s (burst 100), 50 concurrent connections; global 300 r/s (burst 600). nginx answers 429/413 with JSON bodies.
