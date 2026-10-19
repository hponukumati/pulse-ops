# Security

PulseOps is a personal portfolio project for one workspace with a few provisioned users and synthetic or personal non-sensitive telemetry. It makes **no claim of enterprise readiness, certification, or freedom from vulnerabilities**. Host administrators (anyone who can read the runtime secrets or run the admin CLI) are trusted.

## Reporting

Please report suspected vulnerabilities privately to the repository owner (GitHub security advisory / private report) rather than in a public issue. Include steps to reproduce; do not include real credentials or personal data.

## Controls (summary)

- **Minimal telemetry:** six allowed fields; no bodies, headers, credentials, URLs, query strings, IPs, user IDs or free-form attributes are accepted or stored. Route templates are registered, never derived from requests.
- **Credentials:** Argon2id passwords; RS256 access JWTs (15 min, pinned keys, all claims validated); rotating opaque refresh secrets (keyed digests only, replay revokes the family); service-scoped ingest tokens (256-bit, HMAC-SHA-256 digest with a separate versioned key, shown once, max 2 active, 90-day expiry). Password reauthentication (5 min, server-side) before issuing tokens, deleting services or changing permissions.
- **Authorization:** one policy module used by REST, every resolver, loaders, subscriptions and the CLI; explicit service grants; indistinguishable `NOT_FOUND`; current account/session/grants reloaded from PostgreSQL on every protected operation and before every subscription delivery.
- **Browser:** access token in memory only; HttpOnly SameSite=Strict refresh cookie accepted only by auth endpoints; exact-Origin + custom-header CSRF defence; strict CSP (no inline/eval scripts), `frame-ancestors 'none'`, `nosniff`, `no-referrer`; per-session in-memory Apollo cache discarded on logout/identity change.
- **Abuse limits:** Redis-backed atomic limits (fail closed for ingest/login/refresh/mutations), bounded queries, body/response caps, timeouts, edge limits in nginx.
- **Data handling:** allowlisted structured logs (no URLs, headers, bodies, emails, variables, frames, bind values); 7-day event retention with read-time filtering; encrypted backups; deletion ledger outside backups.
- **Deployment:** secrets from runtime-mounted files only; non-root, read-only containers with dropped capabilities; private DB/Redis network; least-privilege DB roles (runtime cannot modify audit rows).

## Threat model (one page)

| Threat | Example | Mitigations | Residual risk |
|---|---|---|---|
| **Stolen ingest token** | Token leaked from an emitter's config or CI logs | Write-only and scoped to one service (cannot read metrics, change rules, or act as a user); only six bounded fields accepted, so it cannot exfiltrate or inject markup; per-token/service/global rate limits and a deployment-wide quota bound damage; immediate revocation/rotation with a defined ordering against in-flight requests (row locks), audit entries for issuance/revocation; secrets never logged, stored only as HMAC digests, shown once | Until revoked, an attacker can submit plausible fake telemetry for that service (polluting metrics or triggering/suppressing alerts within that service). Detection relies on the owner noticing anomalies. |
| **Object-access bypass (IDOR)** | Operator for service A queries B's incident by ID, via a nested field, a reused cursor, or a live subscription | Deny-by-default central policy; every object resolved through its authorized service; composite `(service_id, id)` foreign keys; loaders and lists filter by grants; cursors carry position only; authorization rechecked before cache reads and before every WebSocket delivery; tested for direct IDs, nested fields, lists/counts, cursors, subscriptions and caches | A policy bug in new code. Mitigated by the role/action matrix tests and cross-service negative tests that must keep passing. |
| **Telemetry / data leakage** | Secrets or personal data pasted into payloads, headers, URLs or error messages ending up in logs, DB, Redis or test artifacts | Payload minimization (strict allowlist, whole-batch rejection, no raw-payload quarantine); errors contain only codes/indices; allowlisted logging that drops third-party messages; proxy logs without URLs/IPs; canary tests across responses, logs, DB and Redis; CI scans logs/test output for credential patterns; Playwright traces/screenshots disabled | Free text the owner registers (service names, route templates) cannot be proven free of sensitive data by syntax checks — manual review is required. Backups retain data for up to 14 days after deletion. |
| **API exhaustion** | Credential stuffing, oversized or deeply nested queries, ingest floods, many WebSockets | Login/reauth throttles (account and address, temporary only); body caps; GraphQL depth/alias/fragment/cost/page/response limits; per-user ops and concurrency limits; global concurrency; SQL statement timeouts; WebSocket connection/subscription/frame/message limits and bounded buffers; nginx edge limits; fail-closed behaviour when Redis is down | Single-host deployment with no HA; a determined volumetric attack against a hosted instance needs upstream protection. Limits are project defaults, not measured capacity. |

Out of scope (deferred by the PRD): MFA/WebAuthn, SSO, multi-tenancy, HA, immutable audit archives, formal penetration testing.

## Key rotation and compromise response

See [docs/OPERATIONS.md](docs/OPERATIONS.md#key-rotation-and-compromise). Rotating a key alone is not a complete response: revoke affected sessions/credentials as well.

## Dependency and scan exceptions

Findings are investigated rather than assumed safe. Current documented exceptions:

| Finding | Scope | Rationale | Review |
|---|---|---|---|
| 25 HIGH/CRITICAL Go standard-library CVEs in `/usr/local/bin/gosu` inside the official `postgres:18.6-alpine` image (trivy 0.76.0, 2026-10-18) | Third-party database image only; our built images (`pulseops-backend`, `pulseops-frontend`, `pulseops-backup-tools`) scan clean for fixable HIGH/CRITICAL | `gosu` runs once at container start to drop root privileges and `exec` PostgreSQL; it does not use the affected network/crypto code paths. The database has no published ports and sits on an internal network. Our backup-tools image deletes `gosu`. | Track upstream image updates weekly (Dependabot docker); switch to a patched tag as soon as one is published. |
| GHSA-vfj7-8cjw-p6xm (`braces`, stack-exhaustion DoS from deeply nested glob patterns) via `micromatch` → `fast-glob` → `globby` → `@graphql-codegen/cli` (10 npm "high" entries, one advisory chain) | **Dev-only** build tool; not in the production bundle (`npm audit --omit=dev`: 0 vulnerabilities) | Codegen only expands glob patterns hard-coded in `frontend/codegen.ts`; no untrusted input reaches `braces`. CI blocks on production-dependency highs and reports dev findings as warnings. | Re-check weekly (Dependabot/security workflow); remove when a fixed `braces`/`micromatch` is available. |

Run the same checks locally with the commands in [docs/VERIFICATION.md](docs/VERIFICATION.md).
