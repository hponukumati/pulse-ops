"""Service, route and ingest-token lifecycle. Every entry point authorizes via ``policy``."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, text, update

from pulseops.container import Container
from pulseops.errors import Conflict, NotFound, ValidationFailed
from pulseops.logging import log_event
from pulseops.models import AlertRule, DeletionJob, IngestToken, Route, Service, ServiceGrant
from pulseops.policy import Action, Principal, Role, authorize, readable_service_ids
from pulseops.repositories import catalog as repo
from pulseops.repositories.audit import actor_fields, insert_audit
from pulseops.security.tokens import ingest_digest, new_ingest_secret
from pulseops.services.validation import environment_violations, service_name_violations, template_violations

MAX_SERVICES = 20
OPERATOR_SERVICE_QUOTA = 5
MAX_ACTIVE_ROUTES = 100
MAX_ACTIVE_TOKENS = 2
TOKEN_TTL = timedelta(days=90)


@dataclass(frozen=True)
class IssuedToken:
    token: IngestToken
    plaintext: str  # shown once; never persisted or logged


class CatalogService:
    def __init__(self, c: Container) -> None:
        self.c = c

    # ---- services ----

    async def get_service(self, principal: Principal, service_id: uuid.UUID) -> Service:
        authorize(principal, Action.READ_SERVICE, service_id)
        async with self.c.db.session() as s:
            svc = await repo.get_service(s, service_id)
        if svc is None:
            raise NotFound()
        return svc

    async def list_services(self, principal: Principal, *, after: tuple[str, uuid.UUID] | None, limit: int) -> tuple[list[Service], int]:
        readable = readable_service_ids(principal)
        async with self.c.db.session() as s:
            items = await repo.list_services(s, readable=readable, after=after, limit=limit + 1)
            total = await repo.count_services(s, readable=readable)
        return items, total

    async def create_service(self, principal: Principal, name: str, environment: str) -> Service:
        authorize(principal, Action.CREATE_SERVICE)
        errors = [{"field": "name", "code": c} for c in service_name_violations(name)]
        errors += [{"field": "environment", "code": c} for c in environment_violations(environment)]
        if errors:
            raise ValidationFailed(errors)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            # Serialize quota checks across concurrent creators.
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('pulseops:service-quota'))"))
            if await repo.count_active_services(s) >= MAX_SERVICES:
                raise Conflict([{"code": "service_limit"}])
            if (
                principal.role == Role.OPERATOR
                and await repo.count_active_services(s, created_by=principal.user_id) >= OPERATOR_SERVICE_QUOTA
            ):
                raise Conflict([{"code": "operator_service_quota"}])
            exists = await s.execute(
                select(Service.id).where(Service.name == name, Service.environment == environment, Service.deleted_at.is_(None))
            )
            if exists.first():
                raise Conflict([{"field": "name", "code": "duplicate"}])
            svc = Service(id=uuid.uuid4(), name=name, environment=environment, created_by=principal.user_id, created_at=now, updated_at=now)
            s.add(svc)
            await s.flush()
            # Operator-created services grant the creator access in the same transaction.
            if principal.role == Role.OPERATOR and principal.user_id is not None:
                s.add(ServiceGrant(user_id=principal.user_id, service_id=svc.id, granted_by=principal.user_id, created_at=now))
            await insert_audit(
                s, now=now, action="SERVICE_CREATE", result="SUCCESS", target_type="SERVICE", target_id=svc.id, **actor_fields(principal)
            )
        log_event("service.created", service_id=svc.id)
        return svc

    async def update_service(self, principal: Principal, service_id: uuid.UUID, name: str | None, environment: str | None) -> Service:
        authorize(principal, Action.UPDATE_SERVICE, service_id)
        errors = [{"field": "name", "code": c} for c in (service_name_violations(name) if name is not None else [])]
        errors += [{"field": "environment", "code": c} for c in (environment_violations(environment) if environment is not None else [])]
        if errors:
            raise ValidationFailed(errors)
        async with self.c.db.transaction() as s:
            svc = await repo.get_service(s, service_id, for_update=True)
            if svc is None:
                raise NotFound()
            new_name, new_env = name or svc.name, environment or svc.environment
            clash = await s.execute(
                select(Service.id).where(
                    Service.name == new_name, Service.environment == new_env, Service.deleted_at.is_(None), Service.id != svc.id
                )
            )
            if clash.first():
                raise Conflict([{"field": "name", "code": "duplicate"}])
            svc.name, svc.environment, svc.updated_at = new_name, new_env, self.c.now()
        return svc

    async def delete_service(self, principal: Principal, service_id: uuid.UUID) -> uuid.UUID:
        """Immediately disables reads/ingest and revokes tokens; a retryable job purges data."""
        authorize(principal, Action.DELETE_SERVICE, service_id)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            svc = await repo.get_service(s, service_id, for_update=True)
            if svc is None:
                raise NotFound()
            svc.deleted_at, svc.updated_at = now, now
            await repo.revoke_service_tokens(s, service_id, now)
            await s.execute(update(AlertRule).where(AlertRule.service_id == service_id).values(enabled=False, updated_at=now))
            job = DeletionJob(
                id=uuid.uuid4(),
                service_id=service_id,
                requested_at=now,
                requested_by=principal.user_id,
                status="PENDING",
                attempts=0,
                next_attempt_at=now,
            )
            s.add(job)
            await insert_audit(
                s,
                now=now,
                action="SERVICE_DELETE",
                result="SUCCESS",
                target_type="SERVICE",
                target_id=service_id,
                **actor_fields(principal),
            )
        log_event("service.deleted", service_id=service_id, job_id=job.id)
        return job.id

    # ---- routes ----

    async def list_routes(
        self, principal: Principal, service_id: uuid.UUID, *, include_retired: bool, after: tuple[str, uuid.UUID] | None, limit: int
    ) -> tuple[list[Route], int]:
        authorize(principal, Action.READ_SERVICE, service_id)
        async with self.c.db.session() as s:
            if await repo.get_service(s, service_id) is None:
                raise NotFound()
            items = await repo.list_routes(s, service_id, include_retired=include_retired, after=after, limit=limit + 1)
            total = await repo.count_routes(s, service_id, include_retired=include_retired)
        return items, total

    async def register_route(self, principal: Principal, service_id: uuid.UUID, template: str) -> Route:
        authorize(principal, Action.MANAGE_ROUTES, service_id)
        violations = template_violations(template)
        if violations:
            raise ValidationFailed([{"field": "template", "code": c} for c in violations])
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await repo.get_service(s, service_id, for_update=True) is None:
                raise NotFound()
            if await repo.find_active_template(s, service_id, template):
                raise Conflict([{"field": "template", "code": "duplicate"}])
            if await repo.count_routes(s, service_id, include_retired=False) >= MAX_ACTIVE_ROUTES:
                raise Conflict([{"code": "route_limit"}])
            route = Route(id=uuid.uuid4(), service_id=service_id, template=template, created_at=now)
            s.add(route)
        return route

    async def retire_route(self, principal: Principal, route_id: uuid.UUID) -> Route:
        async with self.c.db.session() as s:
            route = await repo.get_route(s, route_id)
        if route is None:
            raise NotFound()
        authorize(principal, Action.MANAGE_ROUTES, route.service_id)
        async with self.c.db.transaction() as s:
            locked = await repo.get_route(s, route_id, for_update=True)
            if locked is None:
                raise NotFound()
            if locked.retired_at is None:
                locked.retired_at = self.c.now()
        return locked

    # ---- ingest tokens ----

    async def list_tokens(self, principal: Principal, service_id: uuid.UUID) -> list[IngestToken]:
        authorize(principal, Action.READ_TOKENS, service_id)
        async with self.c.db.session() as s:
            if await repo.get_service(s, service_id) is None:
                raise NotFound()
            return await repo.list_tokens(s, service_id)

    def _new_token_row(self, service_id: uuid.UUID, principal: Principal) -> tuple[IngestToken, str]:
        keyring = self.c.settings.ingest_keyring
        secret = new_ingest_secret()
        digest = ingest_digest(keyring, keyring.current, secret.secret)
        assert digest is not None
        now = self.c.now()
        row = IngestToken(
            id=uuid.uuid4(),
            public_id=secret.public_id,
            service_id=service_id,
            digest=digest,
            key_version=keyring.current,
            created_by=principal.user_id,
            created_at=now,
            expires_at=now + TOKEN_TTL,
        )
        return row, secret.plaintext

    async def create_token(self, principal: Principal, service_id: uuid.UUID) -> IssuedToken:
        authorize(principal, Action.MANAGE_TOKENS, service_id)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await repo.get_service(s, service_id, for_update=True) is None:
                raise NotFound()
            if await repo.count_active_tokens(s, service_id, now) >= MAX_ACTIVE_TOKENS:
                raise Conflict([{"code": "token_limit"}])
            row, plaintext = self._new_token_row(service_id, principal)
            s.add(row)
            await insert_audit(
                s, now=now, action="TOKEN_ISSUE", result="SUCCESS", target_type="INGEST_TOKEN", target_id=row.id, **actor_fields(principal)
            )
        log_event("token.issued", service_id=service_id)
        return IssuedToken(row, plaintext)

    async def _resolve_token(self, principal: Principal, token_id: uuid.UUID, action: Action) -> IngestToken:
        async with self.c.db.session() as s:
            token = await repo.get_token(s, token_id)
        if token is None:
            raise NotFound()
        authorize(principal, action, token.service_id)
        return token

    async def rotate_token(self, principal: Principal, token_id: uuid.UUID) -> IssuedToken:
        """Atomically issue a replacement and revoke the old token (no grace period)."""
        token = await self._resolve_token(principal, token_id, Action.MANAGE_TOKENS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await repo.get_service(s, token.service_id, for_update=True) is None:
                raise NotFound()
            old = await repo.get_token(s, token_id, for_update=True)
            if old is None:
                raise NotFound()
            if old.revoked_at is not None or old.expires_at <= now:
                raise Conflict([{"code": "token_inactive"}])
            old.revoked_at = now
            row, plaintext = self._new_token_row(old.service_id, principal)
            s.add(row)
            await insert_audit(
                s,
                now=now,
                action="TOKEN_REVOKE",
                result="SUCCESS",
                target_type="INGEST_TOKEN",
                target_id=old.id,
                **actor_fields(principal),
            )
            await insert_audit(
                s, now=now, action="TOKEN_ISSUE", result="SUCCESS", target_type="INGEST_TOKEN", target_id=row.id, **actor_fields(principal)
            )
        return IssuedToken(row, plaintext)

    async def revoke_token(self, principal: Principal, token_id: uuid.UUID) -> IngestToken:
        await self._resolve_token(principal, token_id, Action.MANAGE_TOKENS)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            # Row lock: waits for in-flight ingests holding FOR SHARE, then later ingests see revocation.
            locked = await repo.get_token(s, token_id, for_update=True)
            if locked is None:
                raise NotFound()
            if locked.revoked_at is None:
                locked.revoked_at = now
                await insert_audit(
                    s,
                    now=now,
                    action="TOKEN_REVOKE",
                    result="SUCCESS",
                    target_type="INGEST_TOKEN",
                    target_id=locked.id,
                    **actor_fields(principal),
                )
        return locked
