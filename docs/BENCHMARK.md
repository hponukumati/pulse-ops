# Benchmark

One reproducible run of the PRD §12 workload against the local Compose stack, through the nginx edge, with synthetic data. These are measurements of this workload on this machine — not a capacity guarantee, SLO, or scale claim.

**Run:** 2026-10-18, stack reset at 08:25:35 UTC, warmup 30 s, measured window 600.0 s, then a separate 120 s batch run and a 429 test. Raw output: `artifacts/benchmark.json` (generated, not committed).
**Source tree:** sha256/16 `a436050aaa915437` over `backend/src`, `backend/migrations`, `frontend/src`, `frontend/nginx`, `compose.yaml`, `scripts` (the repository had no commits at the time; see note below).
**Command:** `docker compose down -v && ./scripts/demo-up.sh && docker compose exec -T api pulseops-admin seed-benchmark --events 100000 --services 10 --seed 42 && uv run scripts/benchmark.py`

## Environment

| Item | Value |
|---|---|
| Host | Apple M3 (8 cores), 16 GB RAM, macOS 26.5.2, internal SSD (APFS) |
| Docker | Docker Desktop 29.6.2, Linux VM with 8 vCPU / 7.75 GiB |
| Container limits | postgres 2 CPU / 1 GiB · api 2 CPU / 768 MiB · worker 1 CPU / 512 MiB · redis 1 CPU / 256 MiB · frontend (nginx) 1 CPU / 128 MiB |
| Versions | PostgreSQL 18.6 · Redis 8.10.2 · nginx 1.29.8 · Python 3.13.16 (containers) · FastAPI 0.143.3 · Strawberry 0.336.0 · SQLAlchemy 2.1.5 · asyncpg 0.32.0 · uvicorn 0.54.0 · redis-py 8.1.0 · Pydantic 2.14.1 |
| Load generator | `scripts/benchmark.py` (httpx 0.28.1, Python 3.14.2 via uv) on the **same host** |
| API process | single uvicorn worker process |

## Workload

| Component | Shape |
|---|---|
| Dataset | 100,000 retained synthetic events across 10 services (`bench-01`…`bench-10`, 5 routes each), spread over 7 days, seed 42; 2% 5xx, ~5% 404, log-normal latency |
| Dashboard users | 5 concurrent VIEWER accounts; each cycle (every 2 s): `metrics` for one service with timeseries + top endpoints (range rotating 15m/1h/24h/7d) and the services overview with health |
| Steady ingest | 10 events/s for 600 s: 10 services × one single-event batch per second |
| Burst | +100 events/s for 30 s starting at +300 s: 10 services × one 10-event batch per second |
| Visibility probe | every 10 s: ingest one event on a dedicated route, then poll `metrics` (route-filtered) every 250 ms until it appears |
| Batch run (separate) | one 100-event batch per second for 120 s on one service |
| 429 test (separate) | 40 concurrent single-event requests on one token (limit 10/s, burst 20) |

Load stays below configured limits (per-user 120 ops/min, per-token 10 req/s, per-service 100 events/s, global 300 events/s, edge 50 r/s per address).

## Results

| Measurement | Requests | Success | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| Dashboard `metrics` query | 1,500 | 100% | 58.8 ms | **95.0 ms** | 121.3 ms | 168.7 ms |
| Dashboard overview query | 1,500 | 100% | 30.2 ms | 61.0 ms | 80.6 ms | 109.1 ms |
| Ingest, single-event batches | 5,990 | 100% | 36.0 ms | 57.9 ms | 85.7 ms | 169.6 ms |
| Ingest, burst 10-event batches | 300 | 100% | 43.9 ms | 56.8 ms | 59.4 ms | 63.3 ms |
| **Ingest, 100-event batches (1/s)** | 120 | 100% | 32.9 ms | **35.9 ms** | 38.6 ms | 46.1 ms |

- Events accepted during the measured window: **9,041** (5,990 steady + 3,000 burst + 51 probes) = **15.07 events/s** average; steady rate 9.98 requests/s.
- **Visibility** (ingest `200` → event counted by the metrics API): 51 samples, p50 1.95 s, p95 **2.04 s**, max 2.19 s. This is dominated by the 2-second `asOf` granularity and aggregate cache key; the browser dashboard adds up to its 5 s polling interval, so end-to-end visibility in the UI is ≤ ~7 s for this workload.
- **429 behaviour:** 21 accepted, 19 rejected with 429; every 429 carried `Retry-After` (expected: burst of 20 plus refill during the request fan-out).
- Errors/throttles in the normal workload: **0**.
- Resource peaks (docker stats, 5 s sampling): api 32.5% CPU / 100 MiB · postgres 13.4% / 139 MiB · worker 1.5% / 63 MiB · redis 3.1% / 13 MiB · nginx 1.7% / 9 MiB.

**Initial goals:** dashboard p95 < 1 s — met (95 ms); 100-event batch p95 < 2 s — met (35.9 ms); ≥ 99% success — met (100%); visible within 10 s — met at the API (p95 2.04 s) and within the UI's polling budget.

## Backup/restore on the benchmark dataset

`./scripts/backup.sh` then `./scripts/restore-check.sh` with 121,362 retained events: encrypted dump 9.8 MB, backup 2 s; decrypt + `pg_restore` into an isolated instance ~1 s (second-resolution timer); full isolated check including migrations, counts, aggregates, FK checks and reconciliation 6 s; 11/11 checks passed.

## What this does and does not show

- It shows the implemented query plans (exact `percentile_cont` over up to ~10k events/service in the 7-day window, indexed range scans) are comfortably fast for the PRD's personal-scale workload on a laptop, with zero errors under the specified load.
- It does **not** show behaviour at higher concurrency, on slower disks, across hosts, under network latency, with larger retained datasets (the cap is 1,000,000 events), or with multiple API processes. Client and server share one machine, so latencies exclude real network cost and include load-generator contention.
- Cache effect: aggregates are cached for 10 s per `(service, range, route, asOf)`; with 5 users rotating services/ranges on a 2 s cycle, repeat keys are rare, so dashboard numbers are close to uncached. The overview query (health) is never cached.
- Code changes made after this run (review fixes to admin list totals and WebSocket buffer accounting) do not touch the measured request paths; the run should be repeated after any query, index or ingest change, keeping this workload unchanged when comparing.
