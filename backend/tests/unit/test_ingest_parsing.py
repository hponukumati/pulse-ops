"""Strict ingest parsing/validation: whole-batch rejection with safe, value-free errors."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pulseops.errors import Conflict, MalformedJson, ValidationFailed
from pulseops.services.ingest import dedupe_within_batch, parse_body, validate_batch

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
ROUTE = str(uuid.uuid4())
CANARY = "CANARY-SECRET-7f3a9c"


def ev(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "eventId": str(uuid.uuid4()),
        "timestamp": "2026-10-17T11:59:00Z",
        "method": "GET",
        "routeId": ROUTE,
        "statusCode": 200,
        "durationMs": 12.5,
    }
    base.update(over)
    return base


def codes(exc: ValidationFailed) -> list[tuple[int | None, str, str]]:
    return [(d.get("index"), d["field"], d["code"]) for d in exc.details]


def test_valid_batch_normalizes_to_utc() -> None:
    out = validate_batch([ev(timestamp="2026-10-17T17:29:00+05:30")], NOW)
    assert out[0].occurred_at == datetime(2026, 10, 17, 11, 59, tzinfo=UTC)
    assert out[0].duration_ms == 12.5


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"a":1,"a":2}]',  # duplicate keys
        b"[NaN]",
        b"[Infinity]",
        b"[-Infinity]",
        b"[1,]",
        b"\xff\xfe[]",  # not UTF-8
        b"",
    ],
)
def test_malformed_json_rejected(raw: bytes) -> None:
    with pytest.raises(MalformedJson):
        parse_body(raw)


def test_overflowing_float_is_not_finite() -> None:
    doc = parse_body(
        b'[{"eventId":"%s","timestamp":"2026-10-17T11:59:00Z","method":"GET","routeId":"%s","statusCode":200,"durationMs":1e400}]'
        % (str(uuid.uuid4()).encode(), ROUTE.encode())
    )
    with pytest.raises(ValidationFailed) as ei:
        validate_batch(doc, NOW)
    assert codes(ei.value) == [(0, "durationMs", "invalid_number")]


@pytest.mark.parametrize(
    ("override", "field", "code"),
    [
        ({"statusCode": True}, "statusCode", "invalid_type"),
        ({"statusCode": "200"}, "statusCode", "invalid_type"),
        ({"statusCode": 200.0}, "statusCode", "invalid_type"),
        ({"statusCode": 99}, "statusCode", "out_of_range"),
        ({"statusCode": 600}, "statusCode", "out_of_range"),
        ({"durationMs": False}, "durationMs", "invalid_type"),
        ({"durationMs": "12"}, "durationMs", "invalid_type"),
        ({"durationMs": -0.001}, "durationMs", "out_of_range"),
        ({"durationMs": 120000.001}, "durationMs", "out_of_range"),
        ({"method": "TRACE"}, "method", "invalid_value"),
        ({"method": "get"}, "method", "invalid_value"),
        ({"eventId": str(uuid.uuid1())}, "eventId", "invalid_format"),
        ({"eventId": "not-a-uuid"}, "eventId", "invalid_format"),
        ({"eventId": uuid.uuid4().hex}, "eventId", "invalid_format"),  # non-canonical form
        ({"routeId": 5}, "routeId", "invalid_type"),
        ({"timestamp": "2026-10-17T11:59:00"}, "timestamp", "invalid_format"),  # no offset
        ({"timestamp": "2026-10-17 11:59:00Z"}, "timestamp", "invalid_format"),
        ({"timestamp": 1760702340}, "timestamp", "invalid_type"),
        ({"timestamp": "2026-13-17T11:59:00Z"}, "timestamp", "invalid_format"),
    ],
)
def test_invalid_fields(override: dict[str, object], field: str, code: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        validate_batch([ev(), ev(**override)], NOW)
    assert (1, field, code) in codes(ei.value)


@pytest.mark.parametrize("extra", ["serviceId", "workspaceId", "route", "requestId", "headers", "body", "metadata", "ip", "url"])
def test_forbidden_fields_reject_whole_batch(extra: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        validate_batch([ev(), ev(**{extra: CANARY})], NOW)
    assert (1, "$", "unknown_field") in codes(ei.value)


def test_missing_field() -> None:
    item = ev()
    del item["durationMs"]
    with pytest.raises(ValidationFailed) as ei:
        validate_batch([item], NOW)
    assert codes(ei.value) == [(0, "durationMs", "required")]


@pytest.mark.parametrize(
    ("ts", "ok"),
    [
        ((NOW - timedelta(hours=24)).isoformat(), True),
        ((NOW - timedelta(hours=24, seconds=1)).isoformat(), False),
        ((NOW + timedelta(minutes=5)).isoformat(), True),
        ((NOW + timedelta(minutes=5, seconds=1)).isoformat(), False),
    ],
)
def test_timestamp_window_boundaries(ts: str, ok: bool) -> None:
    if ok:
        assert validate_batch([ev(timestamp=ts)], NOW)
    else:
        with pytest.raises(ValidationFailed) as ei:
            validate_batch([ev(timestamp=ts)], NOW)
        assert codes(ei.value) == [(0, "timestamp", "out_of_range")]


def test_duration_boundaries_accepted() -> None:
    assert len(validate_batch([ev(durationMs=0), ev(durationMs=120000), ev(durationMs=0.0)], NOW)) == 3


@pytest.mark.parametrize("doc", [{}, "x", 5, None, [], [ev()] * 501])
def test_batch_shape(doc: object) -> None:
    with pytest.raises(ValidationFailed):
        validate_batch(doc, NOW)


def test_non_object_item() -> None:
    with pytest.raises(ValidationFailed) as ei:
        validate_batch([ev(), [1, 2]], NOW)
    assert codes(ei.value) == [(1, "$", "expected_object")]


def test_errors_never_echo_input_values() -> None:
    bad = ev(method=CANARY, statusCode=CANARY, routeId=CANARY, eventId=CANARY, timestamp=CANARY, durationMs=CANARY, extra=CANARY)
    with pytest.raises(ValidationFailed) as ei:
        validate_batch([bad], NOW)
    rendered = json.dumps(ei.value.details)
    assert CANARY not in rendered
    assert "input" not in rendered


def test_digest_equal_for_equivalent_content() -> None:
    eid = str(uuid.uuid4())
    a = validate_batch([ev(eventId=eid, durationMs=12, timestamp="2026-10-17T11:59:00Z")], NOW)[0]
    b = validate_batch([ev(eventId=eid.upper(), durationMs=12.0, timestamp="2026-10-17T13:59:00+02:00")], NOW)[0]
    assert a.digest == b.digest


def test_in_batch_duplicates_and_conflicts() -> None:
    eid = str(uuid.uuid4())
    events = validate_batch([ev(eventId=eid), ev(eventId=eid), ev()], NOW)
    unique, dupes = dedupe_within_batch(events)
    assert (len(unique), dupes) == (2, 1)
    conflicting = validate_batch([ev(eventId=eid, statusCode=200), ev(eventId=eid, statusCode=500)], NOW)
    with pytest.raises(Conflict) as ei:
        dedupe_within_batch(conflicting)
    assert ei.value.details == [{"index": 1, "field": "eventId", "code": "conflicting_event_id"}]
