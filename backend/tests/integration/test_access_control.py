"""Service isolation and role enforcement across GraphQL queries, nested fields, lists,
counts, cursors and mutations; GraphQL request limits; no secrets in the schema."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from pulseops.container import Container
from pulseops.gql.schema import export_sdl
from pulseops.policy import Principal
from pulseops.services.admin import AdminService
from pulseops.services.alerts import AlertService, Comparator, Metric, RuleInput
from tests.helpers import World, build_world, error_codes, gql, reauth

pytestmark = pytest.mark.integration


@pytest.fixture
async def world(container: Container, client: httpx.AsyncClient) -> World:
    return await build_world(container, client)


async def _incident_in(container: Container, service_id: str) -> tuple[str, str]:
    """Create a rule and an OPEN incident directly through the state machine (synthetic)."""
    from pulseops.services import incident_state as st

    rule = await AlertService(container).create_rule(
        Principal.cli(), uuid.UUID(service_id), RuleInput("errors", Metric.ERROR_RATE_5XX, Comparator.GT, 0.1, 5)
    )
    async with container.db.transaction() as s:
        locked = await st.lock_rule(s, rule.id)
        assert locked
        inc = await st.open_incident(s, locked, container.now(), value=0.5, slot=None)
    return str(rule.id), str(inc.id)


DIRECT_QUERIES = [
    ("query($id: ID!) { service(id: $id) { id name } }", "service"),
    ("query($id: ID!) { routes(serviceId: $id) { totalCount } }", "routes"),
    ("query($id: ID!) { alertRules(serviceId: $id) { totalCount } }", "alertRules"),
    ("query($id: ID!) { incidents(serviceId: $id) { totalCount } }", "incidents"),
    ("query($id: ID!) { metrics(serviceId: $id, range: LAST_1H) { totalRequests } }", "metrics"),
    ("query($id: ID!) { endpoints(serviceId: $id, range: LAST_1H) { totalCount } }", "endpoints"),
]


@pytest.mark.parametrize(("query", "field"), DIRECT_QUERIES)
async def test_alice_cannot_read_b_by_direct_id(client: httpx.AsyncClient, world: World, query: str, field: str) -> None:
    denied = await gql(client, world.alice, query, {"id": world.service_b})
    absent = await gql(client, world.alice, query, {"id": str(uuid.uuid4())})
    assert error_codes(denied) == error_codes(absent) == ["NOT_FOUND"]
    strip = lambda b: json.dumps({k: v for k, v in b.items() if k != "_status"}, sort_keys=True)  # noqa: E731
    assert strip(denied) == strip(absent)  # indistinguishable responses
    assert "errors" not in await gql(client, world.alice, query, {"id": world.service_a})


async def test_lists_counts_and_cursor_reuse(container: Container, client: httpx.AsyncClient, world: World) -> None:
    q = "query($after: String) { services(first: 1, after: $after) { totalCount edges { cursor node { id name } } pageInfo { endCursor hasNextPage } } }"
    alice = (await gql(client, world.alice, q))["data"]["services"]
    assert alice["totalCount"] == 1 and [e["node"]["id"] for e in alice["edges"]] == [world.service_a]
    bob_page = (await gql(client, world.bob, q))["data"]["services"]
    reused = (await gql(client, world.alice, q, {"after": bob_page["edges"][0]["cursor"]}))["data"]["services"]
    assert all(e["node"]["id"] != world.service_b for e in reused["edges"])
    assert reused["totalCount"] == 1
    forged = await gql(client, world.alice, q, {"after": "not-a-cursor"})
    assert error_codes(forged) == ["VALIDATION_FAILED"]
    admin = (await gql(client, world.admin, "query { services { totalCount } }"))["data"]["services"]
    assert admin["totalCount"] == 2


async def test_nested_fields_respect_grants(container: Container, client: httpx.AsyncClient, world: World) -> None:
    _, inc_a = await _incident_in(container, world.service_a)
    _, inc_b = await _incident_in(container, world.service_b)
    nested = "query($id: ID!) { incident(id: $id) { id service { id name } rule { id name } transitions { kind } } }"
    own = await gql(client, world.alice, nested, {"id": inc_a})
    assert own["data"]["incident"]["service"]["id"] == world.service_a and own["data"]["incident"]["rule"]["name"] == "errors"
    assert error_codes(await gql(client, world.alice, nested, {"id": inc_b})) == ["NOT_FOUND"]
    via_service = await gql(
        client, world.alice, "query { services { edges { node { id incidents(first: 5) { totalCount edges { node { id } } } } } } }"
    )
    ids = [e["node"]["id"] for svc in via_service["data"]["services"]["edges"] for e in svc["node"]["incidents"]["edges"]]
    assert ids == [inc_a]


async def test_viewer_cannot_mutate(container: Container, client: httpx.AsyncClient, world: World) -> None:
    rule_id, inc_id = await _incident_in(container, world.service_a)
    await reauth(client, world.victor)
    rule = {"name": "r", "metric": "P95_LATENCY_MS", "comparator": "GT", "threshold": 100, "windowMinutes": 5}
    mutations: list[tuple[str, dict[str, Any]]] = [
        ('mutation($id: ID!) { updateService(id: $id, input: {name: "x"}) { id } }', {"id": world.service_a}),
        ('mutation($id: ID!) { registerRoute(serviceId: $id, template: "/x") { id } }', {"id": world.service_a}),
        ("mutation($id: ID!) { retireRoute(id: $id) { id } }", {"id": world.route_a}),
        ("mutation($id: ID!) { createIngestToken(serviceId: $id) { secret } }", {"id": world.service_a}),
        (
            "mutation($id: ID!, $r: AlertRuleInput!) { createAlertRule(serviceId: $id, input: $r) { id } }",
            {"id": world.service_a, "r": rule},
        ),
        ("mutation($id: ID!, $r: AlertRuleInput!) { updateAlertRule(id: $id, input: $r) { id } }", {"id": rule_id, "r": rule}),
        ("mutation($id: ID!) { deleteAlertRule(id: $id) { id } }", {"id": rule_id}),
        ("mutation($id: ID!) { acknowledgeIncident(id: $id) { id } }", {"id": inc_id}),
        ("mutation($id: ID!) { resolveIncident(id: $id) { id } }", {"id": inc_id}),
        ("mutation($id: ID!) { deleteService(id: $id) }", {"id": world.service_a}),
        ('mutation { createService(input: {name: "v", environment: "x"}) { id } }', {}),
        (
            "mutation($u: ID!, $s: ID!) { grantServiceAccess(userId: $u, serviceId: $s) { userId } }",
            {"u": str(world.ids["victor"]), "s": world.service_b},
        ),
        ("mutation($u: ID!) { setUserRole(userId: $u, role: ADMIN) { id } }", {"u": str(world.ids["victor"])}),
        ("mutation { setEventRetentionDays(days: 1) }", {}),
    ]
    for query, variables in mutations:
        body = await gql(client, world.victor, query, variables)
        assert error_codes(body) == ["FORBIDDEN"], (query, body)
    # Nothing changed.
    assert (await gql(client, world.alice, "query($id: ID!) { incident(id: $id) { status } }", {"id": inc_id}))["data"]["incident"][
        "status"
    ] == "OPEN"


async def test_operator_limits(client: httpx.AsyncClient, world: World) -> None:
    await reauth(client, world.alice)
    for query in (
        "mutation($id: ID!) { deleteService(id: $id) }",
        "mutation($id: ID!) { setUserDisabled(userId: $id, disabled: true) { id } }",
    ):
        variables = {"id": world.service_a} if "deleteService" in query else {"id": str(world.ids["bob"])}
        assert error_codes(await gql(client, world.alice, query, variables)) == ["FORBIDDEN"]
    for query in (
        "query { users { totalCount } }",
        "query { auditEvents { edges { node { id } } } }",
        "query { diagnostics { outboxPending } }",
        "query { serviceGrants { totalCount } }",
    ):
        assert error_codes(await gql(client, world.alice, query)) == ["FORBIDDEN"]
    # Operators cannot touch another operator's service even with a guessed ID.
    assert error_codes(
        await gql(
            client, world.alice, 'mutation($id: ID!) { registerRoute(serviceId: $id, template: "/x") { id } }', {"id": world.service_b}
        )
    ) == ["NOT_FOUND"]


async def test_operator_created_service_grants_creator(client: httpx.AsyncClient, world: World) -> None:
    body = await gql(client, world.alice, 'mutation { createService(input: {name: "alice-two", environment: "synthetic-test"}) { id } }')
    sid = body["data"]["createService"]["id"]
    me = await gql(client, world.alice, "query { me { serviceIds } }")
    assert sid in me["data"]["me"]["serviceIds"]
    assert error_codes(await gql(client, world.bob, "query($id: ID!) { service(id: $id) { id } }", {"id": sid})) == ["NOT_FOUND"]


async def test_grant_revocation_and_admin_paths(container: Container, client: httpx.AsyncClient, world: World) -> None:
    q = "query($id: ID!) { service(id: $id) { id } }"
    assert "errors" not in await gql(client, world.victor, q, {"id": world.service_a})
    revoke = "mutation($u: ID!, $s: ID!) { revokeServiceAccess(userId: $u, serviceId: $s) }"
    assert error_codes(await gql(client, world.admin, revoke, {"u": str(world.ids["victor"]), "s": world.service_a})) == ["REAUTH_REQUIRED"]
    await reauth(client, world.admin)
    assert (await gql(client, world.admin, revoke, {"u": str(world.ids["victor"]), "s": world.service_a}))["data"][
        "revokeServiceAccess"
    ] is True
    assert error_codes(await gql(client, world.victor, q, {"id": world.service_a})) == ["NOT_FOUND"]  # next request
    audit = await gql(client, world.admin, "query { auditEvents(first: 50) { edges { node { action result actorId targetId } } } }")
    actions = [e["node"]["action"] for e in audit["data"]["auditEvents"]["edges"]]
    assert "GRANT_REMOVE" in actions and "SERVICE_CREATE" in actions and "LOGIN" in actions
    assert "@" not in json.dumps(audit)  # no emails in the audit trail


async def test_last_active_admin_protected(container: Container, client: httpx.AsyncClient, world: World) -> None:
    await reauth(client, world.admin)
    body = await gql(
        client, world.admin, "mutation($u: ID!) { setUserRole(userId: $u, role: VIEWER) { id } }", {"u": str(world.ids["admin"])}
    )
    assert body["errors"][0]["extensions"] == {"code": "CONFLICT", "details": [{"code": "last_active_admin"}]}
    body = await gql(
        client, world.admin, "mutation($u: ID!) { setUserDisabled(userId: $u, disabled: true) { id } }", {"u": str(world.ids["admin"])}
    )
    assert body["errors"][0]["extensions"]["code"] == "CONFLICT"
    await AdminService(container).set_role(Principal.cli(), world.ids["bob"], __import__("pulseops.policy", fromlist=["Role"]).Role.ADMIN)
    assert "errors" not in await gql(
        client, world.admin, "mutation($u: ID!) { setUserRole(userId: $u, role: OPERATOR) { id } }", {"u": str(world.ids["admin"])}
    )


async def test_graphql_transport_limits(client: httpx.AsyncClient, world: World) -> None:
    h = {"Authorization": f"Bearer {world.admin.token}", "Content-Type": "application/json"}
    assert (await client.get("/graphql", params={"query": "{ me { id } }"}, headers=h)).status_code == 405
    batch = await client.post("/graphql", headers=h, content=json.dumps([{"query": "{ me { id } }"}, {"query": "{ me { id } }"}]))
    assert batch.status_code == 400
    two_ops = await client.post(
        "/graphql", headers=h, content=json.dumps({"query": "query A { me { id } } query B { me { id } }", "operationName": "A"})
    )
    assert two_ops.json()["errors"][0]["extensions"]["code"] == "SINGLE_OPERATION_REQUIRED"
    big = await client.post("/graphql", headers=h, content=json.dumps({"query": "{ me { id } }" + " " * 70000}))
    assert big.status_code == 413
    intro = await gql(client, world.admin, "query { __schema { types { name } } }")
    assert set(error_codes(intro)) == {"INTROSPECTION_DISABLED"}
    deep = 'query { incident(id: "x") { service { incidents(first: 1) { edges { node { service { incidents(first: 1) { edges { node { id } } } } } } } } } }'
    assert error_codes(await gql(client, world.admin, deep)) == ["QUERY_TOO_DEEP"]
    aliases = "query { " + " ".join(f"a{i}: me {{ id }}" for i in range(6)) + " }"
    assert error_codes(await gql(client, world.admin, aliases)) == ["TOO_MANY_ALIASES"]
    assert error_codes(await gql(client, world.admin, "query { services(first: 101) { totalCount } }")) == ["VALIDATION_FAILED"]
    sub = await gql(client, world.admin, 'subscription { incidentChanged(serviceId: "x") { version } }')
    assert sub["_status"] == 400
    suggestion = await gql(client, world.admin, "query { servics { totalCount } }")
    assert "Did you mean" not in json.dumps(suggestion) and "services" not in json.dumps(suggestion)


async def test_schema_exposes_no_secrets() -> None:
    sdl = export_sdl().lower()
    for forbidden in ("passwordhash", "password_hash", "digest", "privatekey", "refreshtoken", "keyversion", "sessionsecret"):
        assert forbidden not in sdl
