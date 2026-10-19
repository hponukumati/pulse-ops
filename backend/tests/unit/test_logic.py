"""Pure logic: streaks, metric helpers, bucket layout, route validation, cost limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pulseops.gql.schema import MAX_COST, QueryRejected, prepare
from pulseops.services.incident_state import next_streaks
from pulseops.services.metrics import bucket_layout, operation_as_of
from pulseops.services.validation import environment_violations, service_name_violations, template_violations
from pulseops.workers.evaluator import breached, metric_value, slot_for

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def step(
    state: tuple[int, int, datetime | None], slot: datetime, status: str, active: bool
) -> tuple[tuple[int, int, datetime], bool, bool]:
    u = next_streaks(breach=state[0], ok=state[1], last_slot=state[2], slot=slot, slot_seconds=60, status=status, has_active=active)
    return (u.breach, u.ok, slot), u.open_incident, u.resolve_incident


def test_two_consecutive_breaches_open() -> None:
    s, opened, _ = step((0, 0, None), T0, "BREACH", False)
    assert not opened
    s, opened, _ = step(s, T0 + M, "BREACH", False)
    assert opened


def test_gap_resets_streak_no_replay() -> None:
    s, _, _ = step((0, 0, None), T0, "BREACH", False)
    s, opened, _ = step(s, T0 + 3 * M, "BREACH", False)  # missed slots in between
    assert not opened and s[0] == 1


@pytest.mark.parametrize("status", ["INSUFFICIENT_DATA", "UNKNOWN"])
def test_ineligible_resets_and_keeps_incident(status: str) -> None:
    s, _, _ = step((1, 0, T0), T0 + M, status, True)
    assert s[:2] == (0, 0)
    _, opened, resolved = step(s, T0 + 2 * M, "OK", True)
    assert not opened and not resolved  # needs two fresh OKs


def test_two_oks_resolve_active() -> None:
    s, _, resolved = step((5, 0, T0), T0 + M, "OK", True)
    assert not resolved
    _, _, resolved = step(s, T0 + 2 * M, "OK", True)
    assert resolved


def test_no_reopen_while_active() -> None:
    _, opened, _ = step((3, 0, T0), T0 + M, "BREACH", True)
    assert not opened


def test_metric_values() -> None:
    assert metric_value("ERROR_RATE_5XX", 5, 10, 3, 1.0) == 0.3
    assert metric_value("P95_LATENCY_MS", 5, 10, 0, 123.4) == 123.4
    assert metric_value("REQUEST_RATE", 5, 50, 0, None) == 10.0
    assert metric_value("REQUEST_RATE", 5, 0, 0, None) is None
    assert breached("GT", 1.0, 1.0) is False and breached("GTE", 1.0, 1.0) is True


def test_slot_alignment() -> None:
    assert slot_for(T0 + timedelta(seconds=59.9)) == T0
    assert operation_as_of(T0 + timedelta(seconds=3.7)) == T0 + timedelta(seconds=2)


def test_bucket_layout_clips_partial_edges() -> None:
    start, end = T0 + timedelta(seconds=30), T0 + timedelta(minutes=3, seconds=10)
    layout = bucket_layout(start, end, M)
    assert (layout[0][1], layout[0][2]) == (start, T0 + M)
    assert layout[-1][2] == end and (layout[-1][2] - layout[-1][1]) == timedelta(seconds=10)
    assert sum((b[2] - b[1]).total_seconds() for b in layout) == (end - start).total_seconds()
    assert len(bucket_layout(T0 - timedelta(days=7), T0, timedelta(hours=1))) == 168


@pytest.mark.parametrize(
    "template", ["/", "/users/{id}", "/v1/orders/{orderId}/items", "/files/{name}.{ext}", "/v1/{name}:cancel", "/2024/reports"]
)
def test_templates_accepted(template: str) -> None:
    assert template_violations(template) == []


@pytest.mark.parametrize(
    ("template", "code"),
    [
        ("https://example.com/users", "absolute_url"),
        ("//evil", "absolute_url"),
        ("/users?id=1", "query_or_escape"),
        ("/users#x", "query_or_escape"),
        ("/users/%7Bid%7D", "query_or_escape"),
        ("/users/john@example.com", "contains_email"),
        ("/users/550e8400-e29b-41d4-a716-446655440000", "contains_uuid"),
        ("/accounts/1234567", "contains_numeric_id"),
        ("/t/deadbeefdeadbeefdeadbeef", "contains_identifier"),
        ("/k/tok9abcdefghijklmnop1234xyz", "contains_identifier"),  # token-like segment (synthetic)
        ("/users/{id", "invalid_parameter"),
        ("/users/{1d}", "invalid_parameter"),
        ("/a b", "invalid_characters"),
        ("/über", "invalid_characters"),
        ("/x\x00", "invalid_characters"),
        ("users", "invalid_characters"),
        ("/" + "/".join(["a"] * 21), "too_many_segments"),
        ("/" + "a" * 200, "too_long"),
    ],
)
def test_templates_rejected(template: str, code: str) -> None:
    assert code in template_violations(template)


def test_service_metadata() -> None:
    assert service_name_violations("checkout-api (EU)") == []
    assert service_name_violations("bob@example.com") == ["invalid_characters"]
    assert service_name_violations(" padded") == ["invalid_characters"]
    assert service_name_violations("x" * 81) == ["too_long"]
    assert environment_violations("production") == []
    assert environment_violations("Prod Env") == ["invalid_characters"]
    assert environment_violations("x" * 33) == ["too_long"]


def test_cost_limits() -> None:
    ok = prepare("query { services(first: 10) { edges { node { id name } } } }", None, {})
    assert ok.cost < MAX_COST
    heavy = "query { services(first: 100) { edges { node { incidents(first: 100) { edges { node { id } } } } } } }"
    with pytest.raises(QueryRejected) as ei:
        prepare(heavy, None, {})
    assert ei.value.code == "QUERY_TOO_COMPLEX"
    # Variables are taken into account.
    with pytest.raises(QueryRejected):
        prepare(
            "query Q($n: Int) { services(first: $n) { edges { node { incidents(first: $n) { edges { node { id } } } } } } }",
            None,
            {"n": 100},
        )


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("query A { me { id } } query B { me { id } }", "SINGLE_OPERATION_REQUIRED"),
        ("{ me { id ", "GRAPHQL_PARSE_FAILED"),
        ("", "BAD_REQUEST"),
        ("query { me { ...F } } " + " ".join(f"fragment F{i} on Me {{ id }}" for i in range(21)), "TOO_MANY_FRAGMENTS"),
    ],
)
def test_document_limits(query: str, code: str) -> None:
    with pytest.raises(QueryRejected) as ei:
        prepare(query, None, {})
    assert ei.value.code == code
