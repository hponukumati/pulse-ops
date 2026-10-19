"""Users, sessions, refresh tokens and service grants."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from pulseops.models import RefreshToken, Service, ServiceGrant, Session, User


async def get_user_by_email(s: AsyncSession, email: str) -> User | None:
    return (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()


async def get_user(s: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
    q = select(User).where(User.id == user_id)
    if for_update:
        q = q.with_for_update()
    return (await s.execute(q)).scalar_one_or_none()


async def principal_row(s: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> tuple[User, Session] | None:
    row = (
        await s.execute(select(User, Session).join(Session, Session.user_id == User.id).where(Session.id == session_id, User.id == user_id))
    ).first()
    return (row[0], row[1]) if row else None


async def granted_service_ids(s: AsyncSession, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    rows = await s.execute(
        select(ServiceGrant.service_id)
        .join(Service, Service.id == ServiceGrant.service_id)
        .where(ServiceGrant.user_id == user_id, Service.deleted_at.is_(None))
    )
    return frozenset(rows.scalars())


async def insert_session(s: AsyncSession, *, user_id: uuid.UUID, now: datetime, expires_at: datetime) -> Session:
    sess = Session(id=uuid.uuid4(), user_id=user_id, created_at=now, expires_at=expires_at)
    s.add(sess)
    await s.flush()
    return sess


async def revoke_session(s: AsyncSession, session_id: uuid.UUID, *, now: datetime, reason: str) -> None:
    await s.execute(
        update(Session).where(Session.id == session_id, Session.revoked_at.is_(None)).values(revoked_at=now, revoke_reason=reason)
    )


async def revoke_user_sessions(s: AsyncSession, user_id: uuid.UUID, *, now: datetime, reason: str, keep: uuid.UUID | None = None) -> int:
    cond = and_(Session.user_id == user_id, Session.revoked_at.is_(None))
    if keep is not None:
        cond = and_(cond, Session.id != keep)
    res = await s.execute(update(Session).where(cond).values(revoked_at=now, revoke_reason=reason))
    return res.rowcount or 0  # type: ignore[attr-defined]


async def revoke_all_sessions(s: AsyncSession, *, now: datetime, reason: str) -> int:
    res = await s.execute(update(Session).where(Session.revoked_at.is_(None)).values(revoked_at=now, revoke_reason=reason))
    return res.rowcount or 0  # type: ignore[attr-defined]


async def set_reauth(s: AsyncSession, session_id: uuid.UUID, now: datetime) -> None:
    await s.execute(update(Session).where(Session.id == session_id).values(reauth_at=now))


async def insert_refresh(s: AsyncSession, *, session_id: uuid.UUID, digest: bytes, now: datetime) -> None:
    s.add(RefreshToken(id=uuid.uuid4(), session_id=session_id, digest=digest, key_version=1, created_at=now))
    await s.flush()


async def refresh_for_update(s: AsyncSession, digest: bytes) -> tuple[RefreshToken, Session] | None:
    """Lock the refresh token and its session family so concurrent refreshes serialize."""
    row = (
        await s.execute(
            select(RefreshToken, Session)
            .join(Session, Session.id == RefreshToken.session_id)
            .where(RefreshToken.digest == digest)
            .with_for_update()
        )
    ).first()
    return (row[0], row[1]) if row else None


async def list_users(s: AsyncSession, *, after: tuple[str, uuid.UUID] | None, limit: int) -> list[User]:
    q = select(User)
    if after:
        q = q.where(text("(users.email, users.id) > (:a_email, :a_id)").bindparams(a_email=after[0], a_id=after[1]))
    return list((await s.execute(q.order_by(User.email, User.id).limit(limit))).scalars())


async def count_users(s: AsyncSession) -> int:
    return int((await s.execute(select(func.count()).select_from(User))).scalar_one())


async def lock_active_admin_ids(s: AsyncSession) -> list[uuid.UUID]:
    rows = await s.execute(select(User.id).where(User.role == "ADMIN", User.disabled_at.is_(None)).order_by(User.id).with_for_update())
    return list(rows.scalars())


async def list_grants(
    s: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    service_id: uuid.UUID | None,
    after: tuple[str, uuid.UUID] | None,
    limit: int,
) -> list[ServiceGrant]:
    q = select(ServiceGrant).join(Service, Service.id == ServiceGrant.service_id).where(Service.deleted_at.is_(None))
    if user_id:
        q = q.where(ServiceGrant.user_id == user_id)
    if service_id:
        q = q.where(ServiceGrant.service_id == service_id)
    # Keyset on the composite primary key (user_id, service_id).
    if after:
        q = q.where(
            text("(service_grants.user_id, service_grants.service_id) > (CAST(:a_user AS uuid), :a_svc)").bindparams(
                a_user=after[0], a_svc=after[1]
            )
        )
    q = q.order_by(ServiceGrant.user_id, ServiceGrant.service_id).limit(limit)
    return list((await s.execute(q)).scalars())


async def count_grants(s: AsyncSession, *, user_id: uuid.UUID | None, service_id: uuid.UUID | None) -> int:
    q = (
        select(func.count())
        .select_from(ServiceGrant)
        .join(Service, Service.id == ServiceGrant.service_id)
        .where(Service.deleted_at.is_(None))
    )
    if user_id:
        q = q.where(ServiceGrant.user_id == user_id)
    if service_id:
        q = q.where(ServiceGrant.service_id == service_id)
    return int((await s.execute(q)).scalar_one())
