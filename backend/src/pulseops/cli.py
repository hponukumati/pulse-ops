"""Protected local administrator CLI (``pulseops-admin``).

Runs on the host/container with access to the runtime secrets; host administrators are
trusted. Passwords are read from a TTY without echo (``getpass``) or from stdin with
``--password-stdin``; never from command-line arguments. Demo/benchmark seeding is refused
unless the deployment is explicitly in local demo mode.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import random
import secrets
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, text

from pulseops.config import Env, get_settings, validate_for_startup
from pulseops.container import Container, build_container
from pulseops.errors import AppError
from pulseops.models import Route, Service, ServiceGrant, User
from pulseops.policy import Principal, Role
from pulseops.repositories import accounts as accounts_repo
from pulseops.repositories import catalog as catalog_repo
from pulseops.repositories.audit import insert_audit
from pulseops.services.admin import AdminService
from pulseops.services.ingest import _INSERT_SQL, MAX_RETAINED_EVENTS, event_digest
from pulseops.workers import maintenance

DEMO_SERVICES = {
    "checkout-api": ["/health", "/cart", "/cart/{itemId}", "/checkout", "/orders/{orderId}"],
    "billing-api": ["/health", "/invoices", "/invoices/{invoiceId}", "/payments", "/refunds/{refundId}"],
}
DEMO_ENV = "synthetic-demo"
BENCH_ENV = "synthetic-bench"


def _read_password(args: argparse.Namespace, prompt: str = "Password: ") -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("passwords do not match")
    return first


def _require_demo(c: Container) -> None:
    s = c.settings
    if s.env != Env.LOCAL or not s.demo_mode:
        raise SystemExit("refused: seeding requires PULSEOPS_ENV=local and PULSEOPS_DEMO_MODE=true")


async def _user_id(c: Container, email: str) -> uuid.UUID:
    async with c.db.session() as s:
        user = await accounts_repo.get_user_by_email(s, email.strip().lower())
    if user is None:
        raise SystemExit("no such user")
    return user.id


async def cmd_create_user(c: Container, args: argparse.Namespace) -> None:
    user = await AdminService(c).create_user(Principal.cli(), args.email, _read_password(args), Role(args.role))
    print(json.dumps({"id": str(user.id), "role": user.role}))


async def cmd_reset_password(c: Container, args: argparse.Namespace) -> None:
    await AdminService(c).reset_password(Principal.cli(), args.email, _read_password(args, "New password: "))
    print("password reset; all sessions for the account were revoked")


async def cmd_set_role(c: Container, args: argparse.Namespace) -> None:
    await AdminService(c).set_role(Principal.cli(), await _user_id(c, args.email), Role(args.role))
    print("role updated")


async def cmd_disable(c: Container, args: argparse.Namespace) -> None:
    await AdminService(c).set_disabled(Principal.cli(), await _user_id(c, args.email), not args.enable)
    print("account updated")


async def cmd_grant(c: Container, args: argparse.Namespace) -> None:
    admin, uid, sid = AdminService(c), await _user_id(c, args.email), uuid.UUID(args.service_id)
    if args.revoke:
        await admin.revoke_grant(Principal.cli(), uid, sid)
    else:
        await admin.grant(Principal.cli(), uid, sid)
    print("grant updated")


async def cmd_seed_demo(c: Container, args: argparse.Namespace) -> None:
    """Synthetic demo accounts/services. Passwords are random per run and printed once."""
    _require_demo(c)
    accounts = {
        "admin@pulseops.test": Role.ADMIN,
        "alice@pulseops.test": Role.OPERATOR,
        "bob@pulseops.test": Role.OPERATOR,
        "victor@pulseops.test": Role.VIEWER,
    }
    creds: dict[str, dict[str, str]] = {}
    ids: dict[str, uuid.UUID] = {}
    for email, role in accounts.items():
        pw = secrets.token_urlsafe(18)
        hashed = await c.hasher.hash(pw)
        now = c.now()
        async with c.db.transaction() as s:
            user = await accounts_repo.get_user_by_email(s, email)
            if user is None:
                user = User(
                    id=uuid.uuid4(),
                    email=email,
                    password_hash=hashed,
                    role=role.value,
                    password_changed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                s.add(user)
            else:
                user.password_hash, user.role, user.disabled_at, user.updated_at = hashed, role.value, None, now
                await accounts_repo.revoke_user_sessions(s, user.id, now=now, reason="PASSWORD_RESET")
            await s.flush()
            await insert_audit(
                s, now=now, action="DEMO_SEED_USER", result="SUCCESS", actor_kind="CLI", target_type="USER", target_id=user.id
            )
        ids[email] = user.id
        creds[email.split("@")[0]] = {"email": email, "password": pw, "role": role.value, "id": str(user.id)}
    services: dict[str, str] = {}
    for name, routes in DEMO_SERVICES.items():
        now = c.now()
        async with c.db.transaction() as s:
            svc = (
                await s.execute(select(Service).where(Service.name == name, Service.environment == DEMO_ENV, Service.deleted_at.is_(None)))
            ).scalar_one_or_none()
            if svc is None:
                svc = Service(id=uuid.uuid4(), name=name, environment=DEMO_ENV, created_by=None, created_at=now, updated_at=now)
                s.add(svc)
                await s.flush()
                for t in routes:
                    s.add(Route(id=uuid.uuid4(), service_id=svc.id, template=t, created_at=now))
            services[name] = str(svc.id)
    grants = {"alice@pulseops.test": "checkout-api", "victor@pulseops.test": "checkout-api", "bob@pulseops.test": "billing-api"}
    async with c.db.transaction() as s:
        for email, svc_name in grants.items():
            sid = uuid.UUID(services[svc_name])
            exists = (
                await s.execute(select(ServiceGrant).where(ServiceGrant.user_id == ids[email], ServiceGrant.service_id == sid))
            ).scalar_one_or_none()
            if exists is None:
                s.add(ServiceGrant(user_id=ids[email], service_id=sid, granted_by=None, created_at=c.now()))
    print(json.dumps({"synthetic": True, "accounts": creds, "services": services}, indent=2))


async def cmd_seed_benchmark(c: Container, args: argparse.Namespace) -> None:
    """Deterministic synthetic history (direct insert: ingest only accepts <= 24 h old events)."""
    _require_demo(c)
    rng = random.Random(args.seed)
    now = c.now()
    per_service = args.events // args.services
    service_ids: list[str] = []
    for i in range(args.services):
        name = f"bench-{i + 1:02d}"
        async with c.db.transaction() as s:
            svc = (
                await s.execute(select(Service).where(Service.name == name, Service.environment == BENCH_ENV, Service.deleted_at.is_(None)))
            ).scalar_one_or_none()
            if svc is None:
                svc = Service(id=uuid.uuid4(), name=name, environment=BENCH_ENV, created_by=None, created_at=now, updated_at=now)
                s.add(svc)
                await s.flush()
                for t in ("/health", "/items", "/items/{id}", "/search", "/users/{userId}/orders"):
                    s.add(Route(id=uuid.uuid4(), service_id=svc.id, template=t, created_at=now))
            await s.flush()
            routes = list((await s.execute(select(Route.id).where(Route.service_id == svc.id, Route.retired_at.is_(None)))).scalars())
        service_ids.append(str(svc.id))
        inserted = 0
        while inserted < per_service:
            batch = min(5000, per_service - inserted)
            rows = []
            for _ in range(batch):
                age = timedelta(seconds=rng.uniform(60, 7 * 24 * 3600 - 3600))
                status = 500 if rng.random() < 0.02 else (404 if rng.random() < 0.05 else 200)
                event_id = uuid.UUID(int=rng.getrandbits(128), version=4)
                rows.append(
                    (
                        event_id,
                        rng.choice(routes),
                        now - age,
                        rng.choice(["GET", "GET", "GET", "POST", "PUT"]),
                        status,
                        min(120000.0, round(rng.lognormvariate(3.5, 0.6), 3)),
                    )
                )
            async with c.db.transaction() as s:
                res = await s.execute(
                    _INSERT_SQL,
                    {
                        "service_id": svc.id,
                        "received_at": now,
                        "event_ids": [r[0] for r in rows],
                        "route_ids": [r[1] for r in rows],
                        "occurred": [r[2] for r in rows],
                        "methods": [r[3] for r in rows],
                        "statuses": [r[4] for r in rows],
                        "durations": [r[5] for r in rows],
                        "digests": [event_digest(r[0], r[2], r[3], r[1], r[4], r[5]) for r in rows],
                    },
                )
                n = len(list(res.scalars()))
                q = await s.execute(
                    text("UPDATE ingest_quota SET used = used + :n WHERE id = 1 AND used + :n <= :max RETURNING used"),
                    {"n": n, "max": MAX_RETAINED_EVENTS},
                )
                if q.first() is None:
                    raise SystemExit("retained-event quota would be exceeded")
            inserted += batch
    print(json.dumps({"synthetic": True, "seed": args.seed, "events": per_service * args.services, "services": service_ids}))


async def cmd_restore_reconcile(c: Container, args: argparse.Namespace) -> None:
    """Run after restoring a backup, before reopening access."""
    now = c.now()
    ledger = maintenance.DeletionLedger(args.ledger_dir or c.settings.deletion_ledger_dir)
    async with c.db.transaction() as s:
        sessions = await accounts_repo.revoke_all_sessions(s, now=now, reason="RESTORE")
        tokens = await catalog_repo.revoke_all_tokens(s, now)
        reapplied = 0
        for entry in ledger.entries():
            sid = uuid.UUID(entry["serviceId"])
            svc = (await s.execute(select(Service).where(Service.id == sid))).scalar_one_or_none()
            if svc is not None and svc.deleted_at is None:
                svc.deleted_at, svc.updated_at = now, now
                await s.execute(
                    text(
                        "INSERT INTO deletion_jobs (id, service_id, requested_at, status, attempts, next_attempt_at)"
                        " VALUES (:id, :sid, :now, 'PENDING', 0, :now) ON CONFLICT (service_id) DO UPDATE SET status = 'PENDING', next_attempt_at = :now"
                    ),
                    {"id": uuid.uuid4(), "sid": sid, "now": now},
                )
                reapplied += 1
        await insert_audit(s, now=now, action="RESTORE_RECONCILE", result="SUCCESS", actor_kind="CLI")
    purged = await maintenance.purge_events(c)
    misc = await maintenance.purge_misc(c)
    await maintenance.reconcile_quota(c)
    async with c.db.session() as s:
        users = [
            {"id": str(u.id), "role": u.role, "disabled": u.disabled_at is not None}
            for u in (await s.execute(select(User).order_by(User.created_at))).scalars()
        ]
        grants = [{"userId": str(g.user_id), "serviceId": str(g.service_id)} for g in (await s.execute(select(ServiceGrant))).scalars()]
    print(
        json.dumps(
            {
                "sessionsRevoked": sessions,
                "ingestTokensRevoked": tokens,
                "deletionsReapplied": reapplied,
                "expiredEventsPurged": purged,
                "otherPurged": misc,
                "reviewUsers": users,
                "reviewGrants": grants,
            },
            indent=2,
        )
    )


async def cmd_purge(c: Container, args: argparse.Namespace) -> None:
    await maintenance.run_hourly(c, maintenance.DeletionLedger(c.settings.deletion_ledger_dir))
    print("retention purge complete")


def cmd_export_schema(args: argparse.Namespace) -> None:
    from pulseops.gql.schema import export_sdl

    Path(args.output).write_text(export_sdl(), encoding="utf-8")
    print(f"wrote {args.output}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pulseops-admin", description="PulseOps protected administrator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--role", required=True, choices=[r.value for r in Role])
    cu.add_argument("--password-stdin", action="store_true")
    rp = sub.add_parser("reset-password")
    rp.add_argument("--email", required=True)
    rp.add_argument("--password-stdin", action="store_true")
    sr = sub.add_parser("set-role")
    sr.add_argument("--email", required=True)
    sr.add_argument("--role", required=True, choices=[r.value for r in Role])
    du = sub.add_parser("disable-user")
    du.add_argument("--email", required=True)
    du.add_argument("--enable", action="store_true")
    gr = sub.add_parser("grant")
    gr.add_argument("--email", required=True)
    gr.add_argument("--service-id", required=True)
    gr.add_argument("--revoke", action="store_true")
    sub.add_parser("seed-demo")
    sb = sub.add_parser("seed-benchmark")
    sb.add_argument("--events", type=int, default=100_000)
    sb.add_argument("--services", type=int, default=10)
    sb.add_argument("--seed", type=int, default=42)
    rr = sub.add_parser("restore-reconcile")
    rr.add_argument("--ledger-dir")
    sub.add_parser("purge")
    es = sub.add_parser("export-schema")
    es.add_argument("--output", default="docs/schema.graphql")
    return p


COMMANDS = {
    "create-user": cmd_create_user,
    "reset-password": cmd_reset_password,
    "set-role": cmd_set_role,
    "disable-user": cmd_disable,
    "grant": cmd_grant,
    "seed-demo": cmd_seed_demo,
    "seed-benchmark": cmd_seed_benchmark,
    "restore-reconcile": cmd_restore_reconcile,
    "purge": cmd_purge,
}


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    validate_for_startup(settings)
    c = build_container(settings, application_name="pulseops-cli", with_maint=args.cmd == "purge")
    try:
        await COMMANDS[args.cmd](c, args)
    except AppError as exc:
        detail = f" {exc.details}" if exc.details else ""
        raise SystemExit(f"error: {exc.code}{detail}") from None
    finally:
        await c.close()


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "export-schema":
        cmd_export_schema(args)
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
