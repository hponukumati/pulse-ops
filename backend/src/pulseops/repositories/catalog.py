"""Services, routes and ingest tokens. All reads exclude deleted services."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from pulseops.models import IngestToken, Route, Service


def _readable(q, service_ids: frozenset[uuid.UUID] | None):  # type: ignore[no-untyped-def]
    q = q.where(Service.deleted_at.is_(None))
    if service_ids is not None:
        q = q.where(Service.id.in_(service_ids) if service_ids else text("false"))
    return q


async def get_service(s: AsyncSession, service_id: uuid.UUID, *, for_update: bool = False, for_share: bool = False) -> Service | None:
    q = select(Service).where(Service.id == service_id, Service.deleted_at.is_(None))
    if for_update:
        q = q.with_for_update()
    elif for_share:
        q = q.with_for_update(read=True)
    return (await s.execute(q)).scalar_one_or_none()


async def services_by_ids(s: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Service]:
    rows = await s.execute(select(Service).where(Service.id.in_(list(ids)), Service.deleted_at.is_(None)))
    return {svc.id: svc for svc in rows.scalars()}


async def list_services(
    s: AsyncSession, *, readable: frozenset[uuid.UUID] | None, after: tuple[str, uuid.UUID] | None, limit: int
) -> list[Service]:
    q = _readable(select(Service), readable)
    if after:
        q = q.where(text("(services.name, services.id) > (:a_name, :a_id)").bindparams(a_name=after[0], a_id=after[1]))
    return list((await s.execute(q.order_by(Service.name, Service.id).limit(limit))).scalars())


async def count_services(s: AsyncSession, *, readable: frozenset[uuid.UUID] | None) -> int:
    q = _readable(select(func.count()).select_from(Service), readable)
    return int((await s.execute(q)).scalar_one())


async def count_active_services(s: AsyncSession, *, created_by: uuid.UUID | None = None) -> int:
    q = select(func.count()).select_from(Service).where(Service.deleted_at.is_(None))
    if created_by is not None:
        q = q.where(Service.created_by == created_by)
    return int((await s.execute(q)).scalar_one())


# ---- routes ----


async def get_route(s: AsyncSession, route_id: uuid.UUID, *, for_update: bool = False) -> Route | None:
    q = select(Route).join(Service, Service.id == Route.service_id).where(Route.id == route_id, Service.deleted_at.is_(None))
    if for_update:
        q = q.with_for_update(of=Route)
    return (await s.execute(q)).scalar_one_or_none()


async def list_routes(
    s: AsyncSession, service_id: uuid.UUID, *, include_retired: bool, after: tuple[str, uuid.UUID] | None, limit: int
) -> list[Route]:
    q = select(Route).where(Route.service_id == service_id)
    if not include_retired:
        q = q.where(Route.retired_at.is_(None))
    if after:
        q = q.where(text("(routes.template, routes.id) > (:a_t, :a_id)").bindparams(a_t=after[0], a_id=after[1]))
    return list((await s.execute(q.order_by(Route.template, Route.id).limit(limit))).scalars())


async def count_routes(s: AsyncSession, service_id: uuid.UUID, *, include_retired: bool) -> int:
    q = select(func.count()).select_from(Route).where(Route.service_id == service_id)
    if not include_retired:
        q = q.where(Route.retired_at.is_(None))
    return int((await s.execute(q)).scalar_one())


async def find_active_template(s: AsyncSession, service_id: uuid.UUID, template: str) -> Route | None:
    q = select(Route).where(Route.service_id == service_id, Route.template == template, Route.retired_at.is_(None))
    return (await s.execute(q)).scalar_one_or_none()


async def routes_by_ids(s: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Route]:
    rows = await s.execute(select(Route).where(Route.id.in_(list(ids))))
    return {r.id: r for r in rows.scalars()}


async def active_route_ids(s: AsyncSession, service_id: uuid.UUID, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    rows = await s.execute(select(Route.id).where(Route.service_id == service_id, Route.id.in_(ids), Route.retired_at.is_(None)))
    return set(rows.scalars())


# ---- ingest tokens ----


async def get_token(s: AsyncSession, token_id: uuid.UUID, *, for_update: bool = False) -> IngestToken | None:
    q = (
        select(IngestToken)
        .join(Service, Service.id == IngestToken.service_id)
        .where(IngestToken.id == token_id, Service.deleted_at.is_(None))
    )
    if for_update:
        q = q.with_for_update(of=IngestToken)
    return (await s.execute(q)).scalar_one_or_none()


async def get_token_by_public_id(s: AsyncSession, public_id: str, *, for_share: bool = False) -> IngestToken | None:
    q = select(IngestToken).where(IngestToken.public_id == public_id)
    if for_share:
        q = q.with_for_update(read=True)
    return (await s.execute(q)).scalar_one_or_none()


async def count_active_tokens(s: AsyncSession, service_id: uuid.UUID, now: datetime) -> int:
    q = (
        select(func.count())
        .select_from(IngestToken)
        .where(IngestToken.service_id == service_id, IngestToken.revoked_at.is_(None), IngestToken.expires_at > now)
    )
    return int((await s.execute(q)).scalar_one())


async def list_tokens(s: AsyncSession, service_id: uuid.UUID) -> list[IngestToken]:
    q = select(IngestToken).where(IngestToken.service_id == service_id).order_by(IngestToken.created_at.desc()).limit(50)
    return list((await s.execute(q)).scalars())


async def revoke_service_tokens(s: AsyncSession, service_id: uuid.UUID, now: datetime) -> None:
    await s.execute(
        update(IngestToken).where(IngestToken.service_id == service_id, IngestToken.revoked_at.is_(None)).values(revoked_at=now)
    )


async def revoke_all_tokens(s: AsyncSession, now: datetime) -> int:
    res = await s.execute(update(IngestToken).where(IngestToken.revoked_at.is_(None)).values(revoked_at=now))
    return res.rowcount or 0  # type: ignore[attr-defined]
