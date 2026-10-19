# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx==0.28.1"]
# ///
"""PulseOps reproducible benchmark (synthetic data only).

Workload (PRD §12), run through the nginx edge against the local Compose stack:
  * dataset: 100,000 seeded retained events across 10 services (`pulseops-admin seed-benchmark --seed 42`)
  * 5 concurrent dashboard users (distinct VIEWER accounts), each loading a service's metrics
    (rotating 15m/1h/24h/7d) and the services overview every 2 s
  * 10 incoming events/s for 10 minutes (10 services x 1 single-event batch/s)
  * plus a 100 events/s burst for 30 s (10 services x 1 ten-event batch/s) starting at t=300 s
  * visibility probe every 10 s: time from ingest 200 to the event appearing in metrics
Separately: one 100-event batch per second for 120 s, and a 429 behaviour test.
Load stays below configured limits; this is a benchmark workload, not a capacity guarantee.

Usage: ./scripts/demo-up.sh && docker compose exec -T api pulseops-admin seed-benchmark --seed 42
       uv run scripts/benchmark.py --out artifacts/benchmark.json
Credentials are generated per run, piped to the admin CLI over stdin and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import statistics
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

BASE = os.environ.get("PULSEOPS_BASE_URL", "http://127.0.0.1:8080")
AUTH = {"Origin": BASE, "X-PulseOps-CSRF": "1", "Content-Type": "application/json"}
ROOT = Path(__file__).resolve().parents[1]
RANGES = ["LAST_15M", "LAST_1H", "LAST_24H", "LAST_7D"]
METRICS_Q = """query($s: ID!, $r: MetricsRange!) { metrics(serviceId: $s, range: $r) { totalRequests requestsPerMinute errorRate5xx p50Ms p95Ms p99Ms
  timeseries { start requests p95Ms } topEndpoints { template method requests p95Ms } } }"""
SERVICES_Q = "query { services(first: 20) { totalCount edges { node { id name health { status sampleCount15m lastEventAt activeIncidents } } } } }"
PROBE_Q = "query($s: ID!, $r: ID!) { metrics(serviceId: $s, range: LAST_15M, routeId: $r) { totalRequests } }"


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def summarize(samples: list[tuple[float, int]], seconds: float) -> dict[str, object]:
    lat = [ms for ms, _ in samples]
    codes: dict[str, int] = {}
    for _, c in samples:
        codes[str(c)] = codes.get(str(c), 0) + 1
    ok = sum(1 for _, c in samples if c == 200)
    return {
        "requests": len(samples), "success": ok, "success_rate": round(ok / len(samples), 5) if samples else None,
        "status_counts": codes, "rate_per_s": round(len(samples) / seconds, 2) if seconds else None,
        "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95), "p99_ms": pct(lat, 0.99), "max_ms": round(max(lat), 2) if lat else None,
    }


def compose(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["docker", "compose", *args], cwd=ROOT, input=stdin, capture_output=True, text=True, check=True)
    return r.stdout


class Bench:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client
        self.ingest: list[tuple[float, int]] = []
        self.burst: list[tuple[float, int]] = []
        self.dash: list[tuple[float, int]] = []
        self.overview: list[tuple[float, int]] = []
        self.visibility: list[float] = []
        self.events_sent = 0
        self.stats: list[dict[str, object]] = []

    async def login(self, email: str, password: str) -> str:
        r = await self.c.post("/api/v1/auth/login", headers=AUTH, json={"email": email, "password": password})
        r.raise_for_status()
        return str(r.json()["accessToken"])

    async def gql(self, token: str, query: str, variables: dict[str, object] | None = None) -> tuple[float, int, dict]:
        t = time.perf_counter()
        r = await self.c.post("/graphql", headers={"Authorization": f"Bearer {token}"}, json={"query": query, "variables": variables or {}})
        ms = (time.perf_counter() - t) * 1000
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        status = r.status_code if not body.get("errors") else 599 if r.status_code == 200 else r.status_code
        return ms, status, body

    async def send(self, secret: str, events: list[dict], bucket: list[tuple[float, int]]) -> int:
        t = time.perf_counter()
        r = await self.c.post("/api/v1/ingest/events", headers={"Authorization": f"Bearer {secret}"}, json=events)
        bucket.append(((time.perf_counter() - t) * 1000, r.status_code))
        if r.status_code == 200:
            self.events_sent += len(events)
        return r.status_code


def make_events(route_ids: list[str], n: int, *, route: str | None = None) -> list[dict]:
    ts = (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    out = []
    for i in range(n):
        out.append({
            "eventId": str(uuid.uuid4()), "timestamp": ts, "method": "GET",
            "routeId": route or route_ids[i % len(route_ids)],
            "statusCode": 500 if secrets.randbelow(100) < 2 else 200, "durationMs": round(20 + secrets.randbelow(4000) / 100, 2),
        })
    return out


async def sample_docker(bench: Bench, stop: asyncio.Event) -> None:
    while not stop.is_set():
        proc = await asyncio.create_subprocess_exec("docker", "stats", "--no-stream", "--format", "{{json .}}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        now = time.time()
        for line in out.decode().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "pulse-ops" in d.get("Name", ""):
                bench.stats.append({"t": now, "name": d["Name"], "cpu": float(d["CPUPerc"].rstrip("%")), "mem": d["MemUsage"].split(" / ")[0]})
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


def mem_mib(text: str) -> float:
    units = {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0, "B": 1 / 1048576}
    for u, f in units.items():
        if text.endswith(u):
            return float(text[: -len(u)]) * f
    return 0.0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=600)
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--burst-at", type=int, default=300)
    ap.add_argument("--burst-seconds", type=int, default=30)
    ap.add_argument("--batch-seconds", type=int, default=120)
    ap.add_argument("--out", default="artifacts/benchmark.json")
    args = ap.parse_args()

    creds = json.loads((ROOT / ".demo-credentials.json").read_text())
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    async with httpx.AsyncClient(base_url=BASE, timeout=10.0, limits=limits) as client:
        b = Bench(client)
        admin = await b.login(creds["accounts"]["admin"]["email"], creds["accounts"]["admin"]["password"])
        r = await client.post("/api/v1/auth/reauthenticate", headers={**AUTH, "Authorization": f"Bearer {admin}"}, json={"password": creds["accounts"]["admin"]["password"]})
        r.raise_for_status()
        _, _, body = await b.gql(admin, SERVICES_Q)
        bench = sorted([e["node"] for e in body["data"]["services"]["edges"] if e["node"]["name"].startswith("bench-")], key=lambda n: n["name"])
        assert len(bench) == 10, "run `pulseops-admin seed-benchmark` first"
        services = []
        for svc in bench:
            _, _, rb = await b.gql(admin, "query($s: ID!) { routes(serviceId: $s) { edges { node { id template } } } }", {"s": svc["id"]})
            routes = [e["node"]["id"] for e in rb["data"]["routes"]["edges"] if not e["node"]["template"].startswith("/probe")]
            _, _, tb = await b.gql(admin, "mutation($s: ID!) { createIngestToken(serviceId: $s) { token { id } secret } }", {"s": svc["id"]})
            services.append({"id": svc["id"], "routes": routes, "secret": tb["data"]["createIngestToken"]["secret"], "token_id": tb["data"]["createIngestToken"]["token"]["id"]})
        _, _, pr = await b.gql(admin, "mutation($s: ID!) { registerRoute(serviceId: $s, template: \"/probe/{run}\") { id } }", {"s": services[0]["id"]})
        probe_route = pr["data"]["registerRoute"]["id"] if "data" in pr and pr["data"] else None
        if probe_route is None:
            _, _, rb = await b.gql(admin, "query($s: ID!) { routes(serviceId: $s) { edges { node { id template } } } }", {"s": services[0]["id"]})
            probe_route = next(e["node"]["id"] for e in rb["data"]["routes"]["edges"] if e["node"]["template"] == "/probe/{run}")

        # Five distinct dashboard users (VIEWER) so per-user API limits are not shared.
        users = []
        for i in range(5):
            email, pw = f"bench-viewer-{i + 1}@pulseops.test", secrets.token_urlsafe(18)
            try:
                compose("exec", "-T", "api", "pulseops-admin", "create-user", "--email", email, "--role", "VIEWER", "--password-stdin", stdin=pw + "\n")
            except subprocess.CalledProcessError:
                compose("exec", "-T", "api", "pulseops-admin", "reset-password", "--email", email, "--password-stdin", stdin=pw + "\n")
            for svc in services:
                compose("exec", "-T", "api", "pulseops-admin", "grant", "--email", email, "--service-id", svc["id"])
            users.append(await b.login(email, pw))

        stop = asyncio.Event()
        phase = {"measuring": False, "start": 0.0}

        async def ingest_loop(svc: dict) -> None:
            while not stop.is_set():
                t0 = time.perf_counter()
                bucket = b.ingest if phase["measuring"] else []
                await b.send(svc["secret"], make_events(svc["routes"], 1), bucket)
                await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))

        async def burst_loop(svc: dict) -> None:
            await asyncio.sleep(args.warmup + args.burst_at)
            end = time.perf_counter() + args.burst_seconds
            while time.perf_counter() < end and not stop.is_set():
                t0 = time.perf_counter()
                await b.send(svc["secret"], make_events(svc["routes"], 10), b.burst)
                await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))

        async def dashboard_user(i: int, token: str) -> None:
            n = 0
            while not stop.is_set():
                t0 = time.perf_counter()
                svc = services[(i + n) % len(services)]
                ms, st, _ = await b.gql(token, METRICS_Q, {"s": svc["id"], "r": RANGES[(i + n) % 4]})
                if phase["measuring"]:
                    b.dash.append((ms, st))
                ms, st, _ = await b.gql(token, SERVICES_Q)
                if phase["measuring"]:
                    b.overview.append((ms, st))
                n += 1
                await asyncio.sleep(max(0.0, 2.0 - (time.perf_counter() - t0)))

        async def visibility_probe() -> None:
            viewer = users[0]
            while not stop.is_set():
                await asyncio.sleep(10)
                if not phase["measuring"] or stop.is_set():
                    continue
                _, _, before = await b.gql(viewer, PROBE_Q, {"s": services[0]["id"], "r": probe_route})
                base = before["data"]["metrics"]["totalRequests"]
                if await b.send(services[0]["secret"], make_events([], 1, route=probe_route), []) != 200:
                    continue
                t_ack = time.perf_counter()
                while time.perf_counter() - t_ack < 30:
                    _, _, cur = await b.gql(viewer, PROBE_Q, {"s": services[0]["id"], "r": probe_route})
                    if cur.get("data") and cur["data"]["metrics"]["totalRequests"] > base:
                        b.visibility.append(round(time.perf_counter() - t_ack, 3))
                        break
                    await asyncio.sleep(0.25)

        sampler_stop = asyncio.Event()
        sampler = asyncio.create_task(sample_docker(b, sampler_stop))
        tasks = [asyncio.create_task(ingest_loop(s)) for s in services]
        tasks += [asyncio.create_task(burst_loop(s)) for s in services]
        tasks += [asyncio.create_task(dashboard_user(i, t)) for i, t in enumerate(users)]
        tasks.append(asyncio.create_task(visibility_probe()))
        print(f"warmup {args.warmup}s ...", flush=True)
        await asyncio.sleep(args.warmup)
        phase["measuring"], phase["start"] = True, time.perf_counter()
        events_at_start = b.events_sent
        print(f"measuring {args.duration}s (burst at +{args.burst_at}s for {args.burst_seconds}s) ...", flush=True)
        await asyncio.sleep(args.duration)
        measured_s = time.perf_counter() - phase["start"]
        events_measured = b.events_sent - events_at_start
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Separate run: one 100-event batch per second (explicit batch shape).
        print(f"batch run {args.batch_seconds}s ...", flush=True)
        batch: list[tuple[float, int]] = []
        svc = services[1]
        t_batch = time.perf_counter()
        for _ in range(args.batch_seconds):
            t0 = time.perf_counter()
            await b.send(svc["secret"], make_events(svc["routes"], 100), batch)
            await asyncio.sleep(max(0.0, 1.0 - (time.perf_counter() - t0)))
        batch_s = time.perf_counter() - t_batch

        # 429 behaviour (reported separately): 40 concurrent single-event requests on one token.
        await asyncio.sleep(3)
        flood: list[tuple[float, int]] = []
        responses = await asyncio.gather(*(client.post("/api/v1/ingest/events", headers={"Authorization": f"Bearer {services[2]['secret']}"}, json=make_events(services[2]["routes"], 1)) for _ in range(40)))
        retry_after_ok = all(r.headers.get("retry-after") for r in responses if r.status_code == 429)
        flood = [(0.0, r.status_code) for r in responses]
        sampler_stop.set()
        await sampler

        # Revoke the benchmark ingest tokens.
        r = await client.post("/api/v1/auth/reauthenticate", headers={**AUTH, "Authorization": f"Bearer {admin}"}, json={"password": creds["accounts"]["admin"]["password"]})
        for s in services:
            await b.gql(admin, "mutation($id: ID!) { revokeIngestToken(id: $id) { revokedAt } }", {"id": s["token_id"]})

    peaks: dict[str, dict[str, float]] = {}
    for s in b.stats:
        name = str(s["name"]).replace("pulse-ops-", "").rsplit("-", 1)[0]
        p = peaks.setdefault(name, {"cpu_pct_peak": 0.0, "mem_mib_peak": 0.0})
        p["cpu_pct_peak"] = max(p["cpu_pct_peak"], float(s["cpu"]))  # type: ignore[arg-type]
        p["mem_mib_peak"] = round(max(p["mem_mib_peak"], mem_mib(str(s["mem"]))), 1)
    result = {
        "started_at": datetime.now(UTC).isoformat(),
        "workload": {
            "dataset_events_seeded": 100_000, "services": 10, "seed": 42, "dashboard_users": 5, "dashboard_cycle_s": 2,
            "base_ingest_events_per_s": 10, "burst_events_per_s": 100, "burst_s": args.burst_seconds, "warmup_s": args.warmup,
            "duration_s": args.duration, "batch_run": {"events_per_batch": 100, "batches_per_s": 1, "seconds": args.batch_seconds},
        },
        "measured_duration_s": round(measured_s, 1),
        "ingest_events_accepted_during_measurement": events_measured,
        "actual_ingest_events_per_s": round(events_measured / measured_s, 2),
        "ingest_single_event_batches": summarize(b.ingest, measured_s),
        "ingest_burst_10_event_batches": summarize(b.burst, args.burst_seconds),
        "dashboard_metrics_query": summarize(b.dash, measured_s),
        "dashboard_overview_query": summarize(b.overview, measured_s),
        "visibility_s": {"samples": len(b.visibility), "p50": pct(b.visibility, 0.5), "p95": pct(b.visibility, 0.95), "max": max(b.visibility) if b.visibility else None},
        "batch_100_events": summarize(batch, batch_s),
        "rate_limit_test": {"requests": len(flood), "status_counts": summarize(flood, 1)["status_counts"], "retry_after_on_all_429": retry_after_ok},
        "container_peaks": peaks,
        "host": {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version()},
    }
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
