"""Central authorization policy.

Every REST handler, GraphQL resolver, DataLoader, subscription delivery and privileged
CLI path calls into this module. Deny by default. Roles alone never grant access to a
service: OPERATOR and VIEWER need an explicit, current grant; only ADMIN sees all.

Absent and inaccessible objects both raise ``NotFound`` so IDs cannot be probed. A
principal who can read a service but lacks the role for an action gets ``Forbidden``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pulseops.errors import Forbidden, NotFound, ReauthRequired

REAUTH_WINDOW = timedelta(minutes=5)


class Role(StrEnum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


class Action(StrEnum):
    READ_SERVICE = "READ_SERVICE"  # metrics, routes, rules, incidents, subscribe
    CREATE_SERVICE = "CREATE_SERVICE"
    UPDATE_SERVICE = "UPDATE_SERVICE"
    MANAGE_ROUTES = "MANAGE_ROUTES"
    MANAGE_RULES = "MANAGE_RULES"
    MANAGE_INCIDENTS = "MANAGE_INCIDENTS"  # acknowledge / resolve
    READ_TOKENS = "READ_TOKENS"  # token metadata only; secrets are never readable
    MANAGE_TOKENS = "MANAGE_TOKENS"
    DELETE_SERVICE = "DELETE_SERVICE"
    VIEW_USERS = "VIEW_USERS"  # users and grants listing
    MANAGE_USERS = "MANAGE_USERS"  # roles, grants, disable
    MANAGE_RETENTION = "MANAGE_RETENTION"
    VIEW_AUDIT = "VIEW_AUDIT"
    VIEW_DIAGNOSTICS = "VIEW_DIAGNOSTICS"


SERVICE_SCOPED = frozenset(
    {
        Action.READ_SERVICE,
        Action.UPDATE_SERVICE,
        Action.MANAGE_ROUTES,
        Action.MANAGE_RULES,
        Action.MANAGE_INCIDENTS,
        Action.READ_TOKENS,
        Action.MANAGE_TOKENS,
        Action.DELETE_SERVICE,
    }
)

_ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.ADMIN: frozenset(Action),
    Role.OPERATOR: frozenset(
        {
            Action.READ_SERVICE,
            Action.CREATE_SERVICE,
            Action.UPDATE_SERVICE,
            Action.MANAGE_ROUTES,
            Action.MANAGE_RULES,
            Action.MANAGE_INCIDENTS,
            Action.READ_TOKENS,
            Action.MANAGE_TOKENS,
        }
    ),
    Role.VIEWER: frozenset({Action.READ_SERVICE}),
}

REAUTH_ACTIONS = frozenset({Action.MANAGE_TOKENS, Action.DELETE_SERVICE, Action.MANAGE_USERS, Action.MANAGE_RETENTION})


@dataclass(frozen=True)
class Principal:
    """The current authorization state, loaded from PostgreSQL for each protected operation."""

    user_id: uuid.UUID | None
    role: Role
    session_id: uuid.UUID | None = None
    granted_service_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    reauth_at: datetime | None = None
    is_cli: bool = False  # trusted host operator using the local admin CLI

    @classmethod
    def cli(cls) -> Principal:
        return cls(user_id=None, role=Role.ADMIN, is_cli=True)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def actor_kind(self) -> str:
        return "CLI" if self.is_cli else "USER"


def can_read_service(principal: Principal, service_id: uuid.UUID) -> bool:
    return principal.is_admin or service_id in principal.granted_service_ids


def readable_service_ids(principal: Principal) -> frozenset[uuid.UUID] | None:
    """None means every (non-deleted) service; otherwise the explicit grant set."""
    return None if principal.is_admin else principal.granted_service_ids


def has_recent_reauth(principal: Principal, now: datetime | None = None) -> bool:
    if principal.is_cli:
        return True
    if principal.reauth_at is None:
        return False
    now = now or datetime.now(UTC)
    return timedelta(0) <= now - principal.reauth_at <= REAUTH_WINDOW


def authorize(principal: Principal, action: Action, service_id: uuid.UUID | None = None) -> None:
    """Raise unless ``principal`` may perform ``action`` (on ``service_id`` when scoped)."""
    if action in SERVICE_SCOPED:
        if service_id is None:
            raise ValueError("service-scoped action requires a service id")
        if not can_read_service(principal, service_id):
            raise NotFound()
    if action not in _ROLE_ACTIONS[principal.role]:
        raise Forbidden()
    if action in REAUTH_ACTIONS and not has_recent_reauth(principal):
        raise ReauthRequired()


def allowed(principal: Principal, action: Action, service_id: uuid.UUID | None = None) -> bool:
    try:
        authorize(principal, action, service_id)
    except (NotFound, Forbidden, ReauthRequired):
        return False
    return True
