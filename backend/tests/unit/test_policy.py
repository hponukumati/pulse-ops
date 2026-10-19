"""Exhaustive role × action × grant matrix for the central policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pulseops.errors import Forbidden, NotFound, ReauthRequired
from pulseops.policy import SERVICE_SCOPED, Action, Principal, Role, authorize

A, B = uuid.uuid4(), uuid.uuid4()
NOW = datetime.now(UTC)


def p(role: Role, grants: set[uuid.UUID] | None = None, reauth: bool = True) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        role=role,
        session_id=uuid.uuid4(),
        granted_service_ids=frozenset(grants or set()),
        reauth_at=NOW if reauth else None,
    )


# Expected outcome with a grant on A and recent reauthentication.
EXPECTED: dict[Role, set[Action]] = {
    Role.ADMIN: set(Action),
    Role.OPERATOR: {
        Action.READ_SERVICE,
        Action.CREATE_SERVICE,
        Action.UPDATE_SERVICE,
        Action.MANAGE_ROUTES,
        Action.MANAGE_RULES,
        Action.MANAGE_INCIDENTS,
        Action.READ_TOKENS,
        Action.MANAGE_TOKENS,
    },
    Role.VIEWER: {Action.READ_SERVICE},
}


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("action", list(Action))
def test_matrix_with_grant(role: Role, action: Action) -> None:
    principal = p(role, {A})
    target = A if action in SERVICE_SCOPED else None
    if action in EXPECTED[role]:
        authorize(principal, action, target)
    else:
        with pytest.raises(Forbidden):
            authorize(principal, action, target)


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.VIEWER])
@pytest.mark.parametrize("action", sorted(SERVICE_SCOPED))
def test_no_grant_is_not_found(role: Role, action: Action) -> None:
    # A known ID gives no implied access; absence and inaccessibility look the same.
    with pytest.raises(NotFound):
        authorize(p(role, {A}), action, B)


@pytest.mark.parametrize("action", sorted(SERVICE_SCOPED))
def test_admin_reaches_any_service(action: Action) -> None:
    authorize(p(Role.ADMIN), action, B)


@pytest.mark.parametrize(
    ("role", "action"),
    [
        (Role.OPERATOR, Action.MANAGE_TOKENS),
        (Role.ADMIN, Action.MANAGE_TOKENS),
        (Role.ADMIN, Action.DELETE_SERVICE),
        (Role.ADMIN, Action.MANAGE_USERS),
        (Role.ADMIN, Action.MANAGE_RETENTION),
    ],
)
def test_reauth_required(role: Role, action: Action) -> None:
    principal = p(role, {A}, reauth=False)
    with pytest.raises(ReauthRequired):
        authorize(principal, action, A if action in SERVICE_SCOPED else None)


def test_reauth_expires_after_five_minutes() -> None:
    stale = Principal(user_id=uuid.uuid4(), role=Role.ADMIN, reauth_at=NOW - timedelta(minutes=5, seconds=1))
    with pytest.raises(ReauthRequired):
        authorize(stale, Action.MANAGE_USERS)


def test_viewer_token_action_is_forbidden_not_reauth() -> None:
    with pytest.raises(Forbidden):
        authorize(p(Role.VIEWER, {A}, reauth=False), Action.MANAGE_TOKENS, A)


def test_cli_principal_is_trusted_admin() -> None:
    authorize(Principal.cli(), Action.MANAGE_USERS)
