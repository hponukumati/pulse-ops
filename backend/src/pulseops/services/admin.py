"""ADMIN-only account, role, grant, retention and audit operations (also used by the CLI)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text

from pulseops.container import Container
from pulseops.errors import Conflict, NotFound, ValidationFailed
from pulseops.models import AuditEvent, ServiceGrant, User
from pulseops.policy import Action, Principal, Role, authorize
from pulseops.repositories import accounts as repo
from pulseops.repositories import catalog as catalog_repo
from pulseops.repositories.audit import actor_fields, insert_audit, list_audit
from pulseops.security.passwords import policy_violations
from pulseops.services.auth import normalize_email

AUDIT_RETENTION = timedelta(days=30)


class AdminService:
    def __init__(self, c: Container) -> None:
        self.c = c

    async def list_users(self, principal: Principal, *, after: tuple[str, uuid.UUID] | None, limit: int) -> tuple[list[User], int]:
        authorize(principal, Action.VIEW_USERS)
        async with self.c.db.session() as s:
            return await repo.list_users(s, after=after, limit=limit + 1), await repo.count_users(s)

    async def get_user(self, principal: Principal, user_id: uuid.UUID) -> User:
        authorize(principal, Action.VIEW_USERS)
        async with self.c.db.session() as s:
            user = await repo.get_user(s, user_id)
        if user is None:
            raise NotFound()
        return user

    async def create_user(self, principal: Principal, email: str, password: str, role: Role) -> User:
        """Account creation is reachable only from the protected host CLI."""
        if not principal.is_cli:
            raise NotFound()
        email_n = normalize_email(email)
        if not (3 <= len(email_n) <= 254) or "@" not in email_n or any(ch.isspace() for ch in email_n):
            raise ValidationFailed([{"field": "email", "code": "invalid_format"}])
        violations = policy_violations(password, email=email_n)
        if violations:
            raise ValidationFailed([{"field": "password", "code": v} for v in violations])
        encoded = await self.c.hasher.hash(password)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await repo.get_user_by_email(s, email_n):
                raise Conflict([{"field": "email", "code": "duplicate"}])
            user = User(
                id=uuid.uuid4(),
                email=email_n,
                password_hash=encoded,
                role=role.value,
                password_changed_at=now,
                created_at=now,
                updated_at=now,
            )
            s.add(user)
            await s.flush()
            await insert_audit(
                s, now=now, action="USER_CREATE", result="SUCCESS", target_type="USER", target_id=user.id, **actor_fields(principal)
            )
        return user

    async def reset_password(self, principal: Principal, email: str, password: str) -> None:
        """Host-operator password reset (CLI only): revokes every session of the account."""
        if not principal.is_cli:
            raise NotFound()
        email_n = normalize_email(email)
        violations = policy_violations(password, email=email_n)
        if violations:
            raise ValidationFailed([{"field": "password", "code": v} for v in violations])
        encoded = await self.c.hasher.hash(password)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            user = await repo.get_user_by_email(s, email_n)
            if user is None:
                raise NotFound()
            locked = await repo.get_user(s, user.id, for_update=True)
            assert locked is not None
            locked.password_hash, locked.password_changed_at, locked.updated_at = encoded, now, now
            await repo.revoke_user_sessions(s, locked.id, now=now, reason="PASSWORD_RESET")
            await insert_audit(
                s, now=now, action="PASSWORD_RESET", result="SUCCESS", target_type="USER", target_id=locked.id, **actor_fields(principal)
            )

    async def _guard_last_admin(self, s, target: User, *, new_role: Role | None = None, disabling: bool = False) -> None:  # type: ignore[no-untyped-def]
        demoting = target.role == Role.ADMIN.value and ((new_role is not None and new_role != Role.ADMIN) or disabling)
        if not demoting or target.disabled_at is not None:
            return
        admins = await repo.lock_active_admin_ids(s)  # row locks serialize concurrent demotions
        if admins == [target.id]:
            raise Conflict([{"code": "last_active_admin"}])

    async def set_role(self, principal: Principal, user_id: uuid.UUID, role: Role) -> User:
        authorize(principal, Action.MANAGE_USERS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            user = await repo.get_user(s, user_id, for_update=True)
            if user is None:
                raise NotFound()
            await self._guard_last_admin(s, user, new_role=role)
            if user.role != role.value:
                user.role, user.updated_at = role.value, now
                await insert_audit(
                    s, now=now, action="ROLE_CHANGE", result="SUCCESS", target_type="USER", target_id=user.id, **actor_fields(principal)
                )
        return user

    async def set_disabled(self, principal: Principal, user_id: uuid.UUID, disabled: bool) -> User:
        authorize(principal, Action.MANAGE_USERS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            user = await repo.get_user(s, user_id, for_update=True)
            if user is None:
                raise NotFound()
            if disabled and user.disabled_at is None:
                await self._guard_last_admin(s, user, disabling=True)
                user.disabled_at, user.updated_at = now, now
                await repo.revoke_user_sessions(s, user.id, now=now, reason="ACCOUNT_DISABLED")
                await insert_audit(
                    s, now=now, action="USER_DISABLE", result="SUCCESS", target_type="USER", target_id=user.id, **actor_fields(principal)
                )
            elif not disabled and user.disabled_at is not None:
                user.disabled_at, user.updated_at = None, now
                await insert_audit(
                    s, now=now, action="USER_ENABLE", result="SUCCESS", target_type="USER", target_id=user.id, **actor_fields(principal)
                )
        return user

    async def grant(self, principal: Principal, user_id: uuid.UUID, service_id: uuid.UUID) -> ServiceGrant:
        authorize(principal, Action.MANAGE_USERS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await repo.get_user(s, user_id) is None or await catalog_repo.get_service(s, service_id, for_share=True) is None:
                raise NotFound()
            existing = (
                await s.execute(select(ServiceGrant).where(ServiceGrant.user_id == user_id, ServiceGrant.service_id == service_id))
            ).scalar_one_or_none()
            if existing:
                return existing
            g = ServiceGrant(user_id=user_id, service_id=service_id, granted_by=principal.user_id, created_at=now)
            s.add(g)
            await insert_audit(
                s, now=now, action="GRANT_ADD", result="SUCCESS", target_type="USER", target_id=user_id, **actor_fields(principal)
            )
        return g

    async def revoke_grant(self, principal: Principal, user_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        authorize(principal, Action.MANAGE_USERS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            res = await s.execute(delete(ServiceGrant).where(ServiceGrant.user_id == user_id, ServiceGrant.service_id == service_id))
            removed = bool(res.rowcount)  # type: ignore[attr-defined]
            if removed:
                await insert_audit(
                    s, now=now, action="GRANT_REMOVE", result="SUCCESS", target_type="USER", target_id=user_id, **actor_fields(principal)
                )
        return removed

    async def list_grants(
        self,
        principal: Principal,
        *,
        user_id: uuid.UUID | None,
        service_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> tuple[list[ServiceGrant], int]:
        authorize(principal, Action.VIEW_USERS)
        async with self.c.db.session() as s:
            items = await repo.list_grants(s, user_id=user_id, service_id=service_id, after=after, limit=limit + 1)
            total = await repo.count_grants(s, user_id=user_id, service_id=service_id)
        return items, total

    async def list_audit(
        self, principal: Principal, *, after: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> tuple[list[AuditEvent], int]:
        authorize(principal, Action.VIEW_AUDIT)
        since = self.c.now() - AUDIT_RETENTION
        async with self.c.db.session() as s:
            items = await list_audit(s, since=since, after=after, limit=limit + 1)
            total = int((await s.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.occurred_at >= since))).scalar_one())
        return items, total

    async def retention_days(self, principal: Principal) -> int:
        authorize(principal, Action.VIEW_DIAGNOSTICS)
        async with self.c.db.session() as s:
            return int((await s.execute(text("SELECT event_retention_days FROM app_settings WHERE id = 1"))).scalar_one())

    async def set_retention_days(self, principal: Principal, days: int) -> int:
        authorize(principal, Action.MANAGE_RETENTION)
        if not 1 <= days <= 7:
            raise ValidationFailed([{"field": "eventRetentionDays", "code": "out_of_range"}])
        now = self.c.now()
        async with self.c.db.transaction() as s:
            await s.execute(
                text("UPDATE app_settings SET event_retention_days = :d, updated_at = :now WHERE id = 1"), {"d": days, "now": now}
            )
            await insert_audit(s, now=now, action="RETENTION_CHANGE", result="SUCCESS", target_type="SETTINGS", **actor_fields(principal))
        return days
