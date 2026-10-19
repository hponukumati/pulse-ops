"""Helpers used only by Alembic migrations."""

import re

from alembic import op

from pulseops.config import get_settings

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_PRIVS = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE|USAGE)(, (SELECT|INSERT|UPDATE|DELETE|USAGE))*$")


def role_name(kind: str) -> str:
    settings = get_settings()
    name = {"runtime": settings.db_user, "maint": settings.db_maint_user}[kind]
    if not _IDENT.match(name):
        raise ValueError("invalid role name")
    return name


def grant(kind: str, table: str, privileges: str, *, sequence: bool = False) -> None:
    if not _IDENT.match(table) or not _PRIVS.match(privileges):
        raise ValueError("invalid grant")
    obj = "SEQUENCE" if sequence else "TABLE"
    op.execute(f'GRANT {privileges} ON {obj} "{table}" TO "{role_name(kind)}"')


def run_sql(script: str) -> None:
    """Execute a DDL script statement by statement (asyncpg cannot run multi-statement strings).

    Scripts passed here must not contain semicolons inside literals or function bodies.
    """
    for stmt in script.split(";"):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            op.execute(body)
