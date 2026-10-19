"""Human authentication: login, refresh rotation, logout, password change, reauthentication.

Every protected operation re-reads account, session and grants from PostgreSQL via
``load_principal``; a JWT alone never authorizes anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from pulseops.container import Container, current_request_id
from pulseops.errors import InvalidCredential, Unauthenticated, ValidationFailed
from pulseops.logging import log_event
from pulseops.models import User
from pulseops.policy import Principal, Role
from pulseops.repositories import accounts as repo
from pulseops.repositories.audit import insert_audit
from pulseops.security.passwords import MAX_LENGTH, policy_violations
from pulseops.security.tokens import TokenInvalid, is_well_formed_refresh, keyed_digest, new_refresh_secret

SESSION_TTL = timedelta(hours=12)
LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW_S = 5, 15 * 60
LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_S = 30, 5 * 60


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    access_expires_at: datetime
    refresh_secret: str
    session_expires_at: datetime
    user_id: uuid.UUID
    role: str


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, c: Container) -> None:
        self.c = c

    # ---- rate limits (fail closed: Redis errors raise DependencyUnavailable) ----

    async def _check_login_limits(self, account_key: str, client_key: str) -> None:
        lim = self.c.limiter
        await lim.window_check(f"rl:login:ip:{lim.opaque(client_key)}", LOGIN_IP_LIMIT, LOGIN_IP_WINDOW_S, increment=True)
        await lim.window_check(f"rl:login:fail:{lim.opaque(account_key)}", LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW_S, increment=False)

    async def _record_login_failure(self, account_key: str) -> None:
        lim = self.c.limiter
        await lim.window_increment(f"rl:login:fail:{lim.opaque(account_key)}", LOGIN_FAIL_WINDOW_S)

    # ---- login ----

    async def login(self, email: str, password: str, client_key: str) -> IssuedSession:
        email_n = normalize_email(email)
        await self._check_login_limits(email_n, client_key)
        if len(email_n) > 254 or len(password) > MAX_LENGTH * 4:
            email_n = ""  # still runs the dummy hash below
        async with self.c.db.session() as s:
            user = await repo.get_user_by_email(s, email_n) if email_n else None
        # Always verify against a hash (dummy for unknown users) to equalize timing.
        ok = await self.c.hasher.verify(password, user.password_hash if user else None)
        now = self.c.now()
        if not ok or user is None or user.disabled_at is not None:
            await self._record_login_failure(email_n)
            async with self.c.db.transaction() as s:
                await insert_audit(
                    s,
                    now=now,
                    action="LOGIN",
                    result="FAILURE",
                    actor_kind="ANONYMOUS" if user is None else "USER",
                    actor_id=None if user is None else user.id,
                    request_id=current_request_id(),
                    coalesce=True,
                )
            log_event("auth.login_failed", code="invalid_credentials")
            raise InvalidCredential()
        secret = new_refresh_secret()
        async with self.c.db.transaction() as s:
            sess = await repo.insert_session(s, user_id=user.id, now=now, expires_at=now + SESSION_TTL)
            await repo.insert_refresh(s, session_id=sess.id, digest=self._digest(secret), now=now)
            await insert_audit(
                s,
                now=now,
                action="LOGIN",
                result="SUCCESS",
                actor_kind="USER",
                actor_id=user.id,
                target_type="SESSION",
                target_id=sess.id,
                request_id=current_request_id(),
            )
        token, exp = self.c.jwt.issue(user.id, sess.id, sess.expires_at, now)
        log_event("auth.login", user_id=user.id)
        return IssuedSession(token, exp, secret, sess.expires_at, user.id, user.role)

    def _digest(self, secret: str) -> bytes:
        return keyed_digest(self.c.settings.refresh_hmac_key, secret)

    # ---- refresh rotation ----

    async def refresh(self, secret: str | None) -> IssuedSession:
        if not secret or not is_well_formed_refresh(secret):
            raise InvalidCredential()
        now = self.c.now()
        new_secret = new_refresh_secret()
        replayed = False
        user: User | None = None
        async with self.c.db.transaction() as s:
            found = await repo.refresh_for_update(s, self._digest(secret))
            if found is None:
                raise InvalidCredential()
            token, sess = found
            if sess.revoked_at is not None or sess.expires_at <= now:
                raise InvalidCredential()
            if token.spent_at is not None:
                # Reuse of a spent token: revoke the whole family (session). Commit, then deny.
                await repo.revoke_session(s, sess.id, now=now, reason="REFRESH_REPLAY")
                await insert_audit(
                    s,
                    now=now,
                    action="REFRESH_REPLAY",
                    result="DENIED",
                    actor_kind="USER",
                    actor_id=sess.user_id,
                    target_type="SESSION",
                    target_id=sess.id,
                    request_id=current_request_id(),
                )
                replayed = True
            else:
                user = await repo.get_user(s, sess.user_id)
                if user is None or user.disabled_at is not None:
                    raise InvalidCredential()
                token.spent_at = now
                await repo.insert_refresh(s, session_id=sess.id, digest=self._digest(new_secret), now=now)
        if replayed:
            log_event("auth.refresh_replay", code="family_revoked")
            raise InvalidCredential()
        assert user is not None
        access, exp = self.c.jwt.issue(sess.user_id, sess.id, sess.expires_at, now)
        return IssuedSession(access, exp, new_secret, sess.expires_at, sess.user_id, user.role)

    # ---- logout ----

    async def logout(self, refresh_secret: str | None, principal: Principal | None) -> None:
        now = self.c.now()
        async with self.c.db.transaction() as s:
            session_id: uuid.UUID | None = principal.session_id if principal else None
            actor = principal.user_id if principal else None
            if session_id is None and refresh_secret and is_well_formed_refresh(refresh_secret):
                found = await repo.refresh_for_update(s, self._digest(refresh_secret))
                if found:
                    session_id, actor = found[1].id, found[1].user_id
            if session_id is None:
                return
            await repo.revoke_session(s, session_id, now=now, reason="LOGOUT")
            await insert_audit(
                s,
                now=now,
                action="LOGOUT",
                result="SUCCESS",
                actor_kind="USER",
                actor_id=actor,
                target_type="SESSION",
                target_id=session_id,
                request_id=current_request_id(),
            )

    # ---- current principal ----

    async def authenticate_bearer(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated()
        token = authorization[7:].strip()
        if token.startswith("poi_"):
            raise Unauthenticated()  # ingest credentials never act as user tokens
        try:
            claims = self.c.jwt.validate(token)
        except TokenInvalid as exc:
            raise Unauthenticated() from exc
        return await self.load_principal(claims.user_id, claims.session_id)

    async def load_principal(self, user_id: uuid.UUID, session_id: uuid.UUID) -> Principal:
        """Current account, session and grants from PostgreSQL (DB failure denies access)."""
        now = self.c.now()
        async with self.c.db.session() as s:
            row = await repo.principal_row(s, user_id, session_id)
            if row is None:
                raise Unauthenticated()
            user, sess = row
            if user.disabled_at is not None or sess.revoked_at is not None or sess.expires_at <= now:
                raise Unauthenticated()
            grants = await repo.granted_service_ids(s, user.id)
        return Principal(user_id=user.id, role=Role(user.role), session_id=sess.id, granted_service_ids=grants, reauth_at=sess.reauth_at)

    async def current_user(self, principal: Principal) -> User:
        if principal.user_id is None:
            raise Unauthenticated()
        async with self.c.db.session() as s:
            user = await repo.get_user(s, principal.user_id)
        if user is None:
            raise Unauthenticated()
        return user

    # ---- reauthentication / password change ----

    async def _verify_current_password(self, principal: Principal, password: str, client_key: str, action: str) -> None:
        assert principal.user_id is not None
        async with self.c.db.session() as s:
            user = await repo.get_user(s, principal.user_id)
        if user is None:
            raise Unauthenticated()
        await self._check_login_limits(user.email, client_key)
        if not await self.c.hasher.verify(password, user.password_hash):
            await self._record_login_failure(user.email)
            async with self.c.db.transaction() as s:
                await insert_audit(
                    s,
                    now=self.c.now(),
                    action=action,
                    result="FAILURE",
                    actor_kind="USER",
                    actor_id=user.id,
                    request_id=current_request_id(),
                    coalesce=True,
                )
            raise InvalidCredential()

    async def reauthenticate(self, principal: Principal, password: str, client_key: str) -> datetime:
        await self._verify_current_password(principal, password, client_key, "REAUTH")
        now = self.c.now()
        assert principal.session_id is not None
        async with self.c.db.transaction() as s:
            await repo.set_reauth(s, principal.session_id, now)
            await insert_audit(
                s,
                now=now,
                action="REAUTH",
                result="SUCCESS",
                actor_kind="USER",
                actor_id=principal.user_id,
                target_type="SESSION",
                target_id=principal.session_id,
                request_id=current_request_id(),
            )
        return now

    async def change_password(self, principal: Principal, current: str, new: str, client_key: str) -> None:
        await self._verify_current_password(principal, current, client_key, "PASSWORD_CHANGE")
        assert principal.user_id is not None
        async with self.c.db.session() as s:
            user = await repo.get_user(s, principal.user_id)
        assert user is not None
        violations = policy_violations(new, email=user.email)
        if violations:
            raise ValidationFailed([{"field": "newPassword", "code": v} for v in violations])
        encoded = await self.c.hasher.hash(new)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            locked = await repo.get_user(s, principal.user_id, for_update=True)
            assert locked is not None
            locked.password_hash, locked.password_changed_at, locked.updated_at = encoded, now, now
            # Other sessions are revoked; the current one continues.
            await repo.revoke_user_sessions(s, principal.user_id, now=now, reason="PASSWORD_CHANGE", keep=principal.session_id)
            await insert_audit(
                s,
                now=now,
                action="PASSWORD_CHANGE",
                result="SUCCESS",
                actor_kind="USER",
                actor_id=principal.user_id,
                target_type="USER",
                target_id=principal.user_id,
                request_id=current_request_id(),
            )
