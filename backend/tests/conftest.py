"""Test fixtures.

Integration tests run against real PostgreSQL and Redis containers (testcontainers) with
the same role separation as production: Alembic runs as the schema-owning migrator, the
app runs as a least-privilege runtime role, audit retention uses the maintenance role.
The API is served by a real in-process uvicorn server so streaming limits, WebSockets
and headers behave as deployed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import create_async_engine

from pulseops.config import Env, Settings, set_settings
from pulseops.container import Container, build_container

BACKEND = Path(__file__).resolve().parents[1]
PG_IMAGE = os.environ.get("PULSEOPS_TEST_PG_IMAGE", "postgres:18-alpine")
REDIS_IMAGE = os.environ.get("PULSEOPS_TEST_REDIS_IMAGE", "redis:8-alpine")

APP_TABLES = [
    "outbox_events",
    "incident_events",
    "incidents",
    "rule_evaluations",
    "alert_rules",
    "worker_heartbeats",
    "request_events",
    "ingest_tokens",
    "routes",
    "service_grants",
    "deletion_jobs",
    "audit_events",
    "refresh_tokens",
    "sessions",
    "services",
    "users",
]


def _pw() -> str:
    return secrets.token_hex(16)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def secrets_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("secrets")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (d / "jwt.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    for name in ("refresh", "ratelimit"):
        (d / name).write_text(base64.b64encode(secrets.token_bytes(32)).decode())
    (d / "ingest.json").write_text(json.dumps({"current": 1, "keys": {"1": base64.b64encode(secrets.token_bytes(32)).decode()}}))
    for name in ("app_pw", "maint_pw", "migrator_pw", "redis_pw"):
        (d / name).write_text(_pw())
    (d / "ledger").mkdir()
    return d


@pytest.fixture(scope="session")
def pg(secrets_dir: Path) -> Iterator[object]:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE, username="postgres", password=_pw(), dbname="postgres", driver=None)
    container.start()
    app_pw, maint_pw, mig_pw = ((secrets_dir / n).read_text() for n in ("app_pw", "maint_pw", "migrator_pw"))
    # Same statements as infra/postgres/init/01-roles.sh (passwords are random hex here).
    sql = f"""
    CREATE ROLE pulseops_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{mig_pw}';
    CREATE ROLE pulseops_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{app_pw}';
    CREATE ROLE pulseops_maint LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{maint_pw}';
    CREATE DATABASE pulseops OWNER pulseops_migrator;
    REVOKE ALL ON DATABASE pulseops FROM PUBLIC;
    GRANT CONNECT ON DATABASE pulseops TO pulseops_app, pulseops_maint;
    """
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        res = container.exec(["psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c", stmt])
        assert res.exit_code == 0, res.output
    for stmt in (
        "REVOKE ALL ON SCHEMA public FROM PUBLIC",
        "ALTER SCHEMA public OWNER TO pulseops_migrator",
        "GRANT USAGE ON SCHEMA public TO pulseops_app, pulseops_maint",
    ):
        res = container.exec(["psql", "-U", "postgres", "-d", "pulseops", "-v", "ON_ERROR_STOP=1", "-c", stmt])
        assert res.exit_code == 0, res.output
    yield container
    container.stop()


@pytest.fixture(scope="session")
def redis_container(secrets_dir: Path) -> Iterator[object]:
    from testcontainers.redis import RedisContainer

    container = RedisContainer(REDIS_IMAGE, password=(secrets_dir / "redis_pw").read_text())
    container.start()
    yield container
    container.stop()


def make_settings(secrets_dir: Path, pg: object, redis_container: object, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": Env.TEST,
        "allowed_origins": ["http://127.0.0.1:8080"],
        "db_host": pg.get_container_host_ip(),  # type: ignore[attr-defined]
        "db_port": int(pg.get_exposed_port(5432)),  # type: ignore[attr-defined]
        "db_password_file": str(secrets_dir / "app_pw"),
        "db_maint_password_file": str(secrets_dir / "maint_pw"),
        "db_migrator_password_file": str(secrets_dir / "migrator_pw"),
        "redis_host": redis_container.get_container_host_ip(),  # type: ignore[attr-defined]
        "redis_port": int(redis_container.get_exposed_port(6379)),  # type: ignore[attr-defined]
        "redis_user": "default",
        "redis_password_file": str(secrets_dir / "redis_pw"),
        "jwt_private_key_file": str(secrets_dir / "jwt.pem"),
        "refresh_hmac_key_file": str(secrets_dir / "refresh"),
        "rate_limit_key_file": str(secrets_dir / "ratelimit"),
        "ingest_hmac_keys_file": str(secrets_dir / "ingest.json"),
        "deletion_ledger_dir": str(secrets_dir / "ledger"),
        "argon2_memory_kib": 19456,
        "argon2_time_cost": 2,
        "db_pool_size": 15,
        "db_max_overflow": 10,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def settings(secrets_dir: Path, pg: object, redis_container: object) -> Settings:
    s = make_settings(secrets_dir, pg, redis_container)
    set_settings(s)
    return s


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.attributes["url"] = url
    return cfg


def run_alembic(url: str, target: str) -> None:
    """Alembic's env.py uses asyncio.run, so run it off the test event loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(command.upgrade, alembic_config(url), target).result()


@pytest.fixture(scope="session")
def migrated(settings: Settings) -> Settings:
    run_alembic(settings.migrator_db_url(), "head")
    return settings


@pytest.fixture(scope="session")
async def container(migrated: Settings) -> AsyncIterator[Container]:
    c = build_container(migrated, application_name="pulseops-test", with_maint=True)
    yield c
    await c.close()


@pytest.fixture(scope="session")
async def migrator_engine(migrated: Settings) -> AsyncIterator[object]:
    engine = create_async_engine(migrated.migrator_db_url())
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def server(container: Container) -> AsyncIterator[str]:
    from pulseops.api.app import create_app

    port = _free_port()
    config = uvicorn.Config(
        create_app(container=container), host="127.0.0.1", port=port, log_config=None, ws_max_size=16 * 1024, lifespan="on"
    )
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    await task


@pytest.fixture
async def client(server: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server, timeout=15.0) as c:
        yield c
