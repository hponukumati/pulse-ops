"""Migrations (empty DB and upgrade from prior schema), model drift and role privileges."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from pulseops.config import Settings
from pulseops.container import Container
from pulseops.models import Base
from tests.conftest import run_alembic

pytestmark = pytest.mark.integration


def _fresh_db(pg: object, name: str) -> None:
    for stmt in (f"CREATE DATABASE {name} OWNER pulseops_migrator", f"GRANT CONNECT ON DATABASE {name} TO pulseops_app, pulseops_maint"):
        res = pg.exec(["psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c", stmt])  # type: ignore[attr-defined]
        assert res.exit_code == 0, res.output
    for stmt in (
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        "ALTER SCHEMA public OWNER TO pulseops_migrator",
        "GRANT USAGE ON SCHEMA public TO pulseops_app, pulseops_maint",
    ):
        res = pg.exec(["psql", "-U", "postgres", "-d", name, "-v", "ON_ERROR_STOP=1", "-c", stmt])  # type: ignore[attr-defined]
        assert res.exit_code == 0, res.output


def _run(coro: object) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro).result()  # type: ignore[arg-type]


def _url(settings: Settings, db: str) -> str:
    return settings.migrator_db_url().rsplit("/", 1)[0] + f"/{db}"


def test_empty_db_and_upgrade_from_prior_schema(pg: object, migrated: Settings) -> None:
    name = f"mig_{uuid.uuid4().hex[:8]}"
    _fresh_db(pg, name)
    url = _url(migrated, name)
    run_alembic(url, "0001_core")  # prior schema

    async def seed_and_check() -> None:
        engine = create_async_engine(url)
        now = datetime.now(UTC)
        uid, sid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO users VALUES (:id, 'u@pulseops.test', 'h', 'ADMIN', NULL, :n, :n, :n)"), {"id": uid, "n": now}
            )
            await conn.execute(text("INSERT INTO services VALUES (:id, 'svc', 'test', :u, :n, :n, NULL)"), {"id": sid, "u": uid, "n": now})
            await conn.execute(text("INSERT INTO routes VALUES (:id, :s, '/x', :n, NULL)"), {"id": rid, "s": sid, "n": now})
        await engine.dispose()

    _run(seed_and_check())
    run_alembic(url, "head")

    async def verify() -> None:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM routes"))).scalar_one() == 1
            assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0002_alerting"
            diffs = await conn.run_sync(lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata))
        await engine.dispose()
        # Models and migrations describe the same schema (no drift).
        assert diffs == [], diffs

    _run(verify())


async def test_runtime_role_is_least_privilege(container: Container) -> None:
    async with container.db.session() as s:
        row = (await s.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"))).one()
        assert tuple(row) == (False, False, False)
        owner = (await s.execute(text("SELECT tableowner FROM pg_tables WHERE tablename = 'request_events'"))).scalar_one()
        assert owner == "pulseops_migrator"
    for sql in (
        "UPDATE audit_events SET action = 'X'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "DELETE FROM users",
        "CREATE TABLE evil (id int)",
        "DROP TABLE request_events",
    ):
        with pytest.raises(DBAPIError) as ei:
            async with container.db.transaction() as s:
                await s.execute(text(sql))
        assert getattr(ei.value.orig, "sqlstate", None) in ("42501", None) or "permission" in str(type(ei.value.orig)).lower()


async def test_maintenance_role_can_purge_audit(container: Container) -> None:
    assert container.maint_db is not None
    async with container.maint_db.transaction() as s:
        await s.execute(text("DELETE FROM audit_events WHERE occurred_at < now() - interval '30 days'"))
        with pytest.raises(DBAPIError):
            async with s.begin_nested():
                await s.execute(text("DELETE FROM request_events"))
