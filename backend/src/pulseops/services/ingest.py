"""Minimal-telemetry ingestion.

Only six fields are accepted. The token determines the service; nothing in the body can
choose it. The whole batch is validated before any insert and is committed atomically:
duplicates (same event id + same digest) are counted, conflicting ids (same id, other
content) roll back the batch with 409, and the response is sent only after COMMIT.

Ordering against revocation: the ingest transaction takes ``FOR SHARE`` on the service and
then the token row (the same service-then-token order that rotation and deletion use, so the
locks cannot deadlock) and rechecks them just before inserting. Revocation/deletion take
``FOR UPDATE`` on the same rows, so either the ingest commits first (and is acknowledged)
or the revocation commits first (and the ingest is rejected with 401).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, DOUBLE_PRECISION, SMALLINT, TIMESTAMP, UUID, VARCHAR

from pulseops.container import Container
from pulseops.errors import Conflict, InvalidCredential, MalformedJson, QuotaExceeded, ValidationFailed
from pulseops.logging import log_event
from pulseops.models import IngestQuota, IngestToken, RequestEvent
from pulseops.ratelimit import Bucket
from pulseops.repositories import catalog as catalog_repo
from pulseops.security.tokens import constant_time_equal, ingest_digest, parse_ingest_token

MAX_BODY_BYTES = 1_048_576
MAX_EVENTS = 500
MAX_REPORTED_ERRORS = 50
MAX_AGE = timedelta(hours=24)
MAX_FUTURE = timedelta(minutes=5)
MAX_RETAINED_EVENTS = 1_000_000
INGEST_STATEMENT_TIMEOUT_MS = 3000

# RFC 3339 date-time with mandatory explicit offset ('Z' or ±hh:mm) and 'T' separator.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")
_UUID_CANON = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class EventIn(BaseModel):
    """Strict event model: no coercion, no unknown properties."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    eventId: str
    timestamp: str
    method: Method
    routeId: str
    statusCode: int = Field(ge=100, le=599)
    durationMs: float = Field(ge=0, le=120000, allow_inf_nan=False)

    @field_validator("eventId")
    @classmethod
    def _uuid4(cls, v: str) -> str:
        if not _UUID_CANON.match(v):
            raise ValueError("uuid")
        u = uuid.UUID(v)
        if u.version != 4 or u.variant != uuid.RFC_4122:
            raise ValueError("uuid")
        return str(u)

    @field_validator("routeId")
    @classmethod
    def _route_uuid(cls, v: str) -> str:
        if not _UUID_CANON.match(v):
            raise ValueError("uuid")
        return str(uuid.UUID(v))

    @field_validator("timestamp")
    @classmethod
    def _rfc3339(cls, v: str) -> str:
        if not _RFC3339.match(v):
            raise ValueError("timestamp")
        datetime.fromisoformat(v)  # range-checks fields (e.g. month 13)
        return v


# Map Pydantic error types to stable public codes (never messages or input values).
_ERROR_CODES = {
    "missing": "required",
    "extra_forbidden": "unknown_field",
    "literal_error": "invalid_value",
    "int_type": "invalid_type",
    "float_type": "invalid_type",
    "string_type": "invalid_type",
    "finite_number": "invalid_number",
    "greater_than_equal": "out_of_range",
    "less_than_equal": "out_of_range",
    "model_type": "invalid_type",
    "value_error": "invalid_format",
}


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: uuid.UUID
    occurred_at: datetime
    method: str
    route_id: uuid.UUID
    status_code: int
    duration_ms: float
    digest: bytes


@dataclass(frozen=True)
class AuthenticatedToken:
    token_id: uuid.UUID
    service_id: uuid.UUID


@dataclass(frozen=True)
class IngestResult:
    accepted: int
    duplicate: int


class _DuplicateKey(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey()
        out[key] = value
    return out


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite constant")


def parse_body(raw: bytes) -> Any:
    """Strict JSON: UTF-8 only, no duplicate keys, no NaN/Infinity, no overflowed floats."""
    try:
        text_body = raw.decode("utf-8")
        doc = json.loads(text_body, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MalformedJson() from exc
    return doc


def event_digest(event_id: uuid.UUID, occurred_at: datetime, method: str, route_id: uuid.UUID, status: int, duration_ms: float) -> bytes:
    """SHA-256 over the normalized allowed fields; exact retries produce the same digest."""
    canonical = "|".join(
        [
            str(event_id),
            occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
            method,
            str(route_id),
            str(status),
            repr(float(duration_ms)),
        ]
    )
    return hashlib.sha256(canonical.encode()).digest()


def validate_batch(doc: Any, now: datetime) -> list[NormalizedEvent]:
    """Validate every item; any invalid item rejects the whole batch with safe codes."""
    if not isinstance(doc, list):
        raise ValidationFailed([{"field": "$", "code": "expected_array"}])
    if not 1 <= len(doc) <= MAX_EVENTS:
        raise ValidationFailed([{"field": "$", "code": "batch_size"}])
    errors: list[dict[str, Any]] = []
    events: list[NormalizedEvent] = []
    for index, item in enumerate(doc):
        if not isinstance(item, dict):
            errors.append({"index": index, "field": "$", "code": "expected_object"})
            continue
        try:
            e = EventIn.model_validate(item)
        except ValidationError as exc:
            for err in exc.errors(include_input=False, include_url=False, include_context=False):
                loc = err.get("loc") or ()
                field = str(loc[0]) if loc and str(loc[0]) in EventIn.model_fields else "$"
                errors.append({"index": index, "field": field, "code": _ERROR_CODES.get(err["type"], "invalid")})
            continue
        ts = datetime.fromisoformat(e.timestamp).astimezone(UTC)
        if ts < now - MAX_AGE or ts > now + MAX_FUTURE:
            errors.append({"index": index, "field": "timestamp", "code": "out_of_range"})
            continue
        if not math.isfinite(e.durationMs):  # defensive; Pydantic already enforces
            errors.append({"index": index, "field": "durationMs", "code": "invalid_number"})
            continue
        events.append(
            NormalizedEvent(
                event_id=uuid.UUID(e.eventId),
                occurred_at=ts,
                method=e.method,
                route_id=uuid.UUID(e.routeId),
                status_code=e.statusCode,
                duration_ms=float(e.durationMs),
                digest=event_digest(uuid.UUID(e.eventId), ts, e.method, uuid.UUID(e.routeId), e.statusCode, e.durationMs),
            )
        )
    if errors:
        raise ValidationFailed(errors[:MAX_REPORTED_ERRORS])
    return events


def dedupe_within_batch(events: list[NormalizedEvent]) -> tuple[list[NormalizedEvent], int]:
    """Collapse identical repeats inside one batch; a conflicting repeat rejects the batch."""
    seen: dict[uuid.UUID, tuple[int, NormalizedEvent]] = {}
    unique: list[NormalizedEvent] = []
    dupes = 0
    conflicts: list[dict[str, Any]] = []
    for index, e in enumerate(events):
        prev = seen.get(e.event_id)
        if prev is None:
            seen[e.event_id] = (index, e)
            unique.append(e)
        elif prev[1].digest == e.digest:
            dupes += 1
        else:
            conflicts.append({"index": index, "field": "eventId", "code": "conflicting_event_id"})
    if conflicts:
        raise Conflict(conflicts[:MAX_REPORTED_ERRORS])
    return unique, dupes


_INSERT_SQL = text(
    """
    INSERT INTO request_events (service_id, event_id, route_id, occurred_at, received_at, method, status_code, duration_ms, digest)
    SELECT :service_id, e.event_id, e.route_id, e.occurred_at, :received_at, e.method, e.status_code, e.duration_ms, e.digest
    FROM unnest(:event_ids, :route_ids, :occurred, :methods, :statuses, :durations, :digests)
         AS e(event_id, route_id, occurred_at, method, status_code, duration_ms, digest)
    ON CONFLICT (service_id, event_id) DO NOTHING
    RETURNING event_id
    """
).bindparams(
    bindparam("service_id", type_=UUID(as_uuid=True)),
    bindparam("received_at", type_=TIMESTAMP(timezone=True)),
    bindparam("event_ids", type_=ARRAY(UUID(as_uuid=True))),
    bindparam("route_ids", type_=ARRAY(UUID(as_uuid=True))),
    bindparam("occurred", type_=ARRAY(TIMESTAMP(timezone=True))),
    bindparam("methods", type_=ARRAY(VARCHAR(7))),
    bindparam("statuses", type_=ARRAY(SMALLINT)),
    bindparam("durations", type_=ARRAY(DOUBLE_PRECISION)),
    bindparam("digests", type_=ARRAY(BYTEA)),
)


class IngestService:
    def __init__(self, c: Container) -> None:
        self.c = c

    async def authenticate(self, authorization: str | None) -> AuthenticatedToken:
        """Validate the ingest credential. Every failure looks identical (401)."""
        if not authorization or not authorization.startswith("Bearer "):
            raise InvalidCredential()
        parsed = parse_ingest_token(authorization[7:].strip())
        if parsed is None:
            raise InvalidCredential()  # includes user JWTs: they cannot ingest
        now = self.c.now()
        async with self.c.db.session() as s:
            row = await catalog_repo.get_token_by_public_id(s, parsed.public_id)
            svc = await catalog_repo.get_service(s, row.service_id) if row else None
        expected = ingest_digest(self.c.settings.ingest_keyring, row.key_version, parsed.secret) if row else None
        valid = row is not None and expected is not None and constant_time_equal(expected, row.digest)
        if not valid or row is None or row.revoked_at is not None or row.expires_at <= now or svc is None:
            raise InvalidCredential()
        return AuthenticatedToken(token_id=row.id, service_id=row.service_id)

    async def check_request_rate(self, token: AuthenticatedToken) -> None:
        await self.c.limiter.consume([Bucket(f"rl:ingest:tok:{token.token_id}", 10, 20, 1)])

    async def check_event_rate(self, token: AuthenticatedToken, submitted: int) -> None:
        # Events (including duplicates) count against service and global budgets.
        await self.c.limiter.consume(
            [
                Bucket(f"rl:ingest:svc:{token.service_id}", 100, 500, submitted),
                Bucket("rl:ingest:global", 300, 1000, submitted),
            ]
        )

    async def ingest(self, token: AuthenticatedToken, events: list[NormalizedEvent], submitted: int) -> IngestResult:
        unique, _ = dedupe_within_batch(events)
        # Consistent insert order avoids deadlocks between batches sharing event ids.
        unique.sort(key=lambda e: e.event_id)
        now = self.c.now()
        route_ids = sorted({e.route_id for e in unique})
        async with self.c.db.transaction(statement_timeout_ms=INGEST_STATEMENT_TIMEOUT_MS) as s:
            # Recheck service and credential under row locks (defined ordering vs revocation/deletion).
            # Lock order everywhere: service row, then token row (rotation/deletion use the same order).
            if await catalog_repo.get_service(s, token.service_id, for_share=True) is None:
                raise InvalidCredential()
            tok = (
                await s.execute(select(IngestToken).where(IngestToken.id == token.token_id).with_for_update(read=True))
            ).scalar_one_or_none()
            if tok is None or tok.revoked_at is not None or tok.expires_at <= now:
                raise InvalidCredential()
            active = await catalog_repo.active_route_ids(s, token.service_id, route_ids)
            bad = [{"index": i, "field": "routeId", "code": "unknown_route"} for i, e in enumerate(events) if e.route_id not in active]
            if bad:
                # Same error whether the route is absent, retired, or belongs to another service.
                raise ValidationFailed(bad[:MAX_REPORTED_ERRORS])
            inserted = set(
                (
                    await s.execute(
                        _INSERT_SQL,
                        {
                            "service_id": token.service_id,
                            "received_at": now,
                            "event_ids": [e.event_id for e in unique],
                            "route_ids": [e.route_id for e in unique],
                            "occurred": [e.occurred_at for e in unique],
                            "methods": [e.method for e in unique],
                            "statuses": [e.status_code for e in unique],
                            "durations": [e.duration_ms for e in unique],
                            "digests": [e.digest for e in unique],
                        },
                    )
                ).scalars()
            )
            existing_ids = [e.event_id for e in unique if e.event_id not in inserted]
            if existing_ids:
                stored = dict(
                    (
                        await s.execute(
                            select(RequestEvent.event_id, RequestEvent.digest).where(
                                RequestEvent.service_id == token.service_id, RequestEvent.event_id.in_(existing_ids)
                            )
                        )
                    ).all()
                )
                index_of = {e.event_id: i for i, e in enumerate(events)}
                conflicts = [
                    {"index": index_of[e.event_id], "field": "eventId", "code": "conflicting_event_id"}
                    for e in unique
                    if e.event_id in stored and stored[e.event_id] != e.digest
                ]
                if conflicts:
                    raise Conflict(conflicts[:MAX_REPORTED_ERRORS])
            accepted = len(inserted)
            if accepted:
                # Reject quota overflow inside the transaction: nothing is committed on 429.
                res = await s.execute(
                    text("UPDATE ingest_quota SET used = used + :n WHERE id = 1 AND used + :n <= :max RETURNING used"),
                    {"n": accepted, "max": MAX_RETAINED_EVENTS},
                )
                if res.first() is None:
                    raise QuotaExceeded()
        duplicate = submitted - accepted
        log_event("ingest.accepted", service_id=token.service_id, count=accepted)
        return IngestResult(accepted=accepted, duplicate=duplicate)


async def quota_used(c: Container) -> int:
    async with c.db.session() as s:
        return int((await s.execute(select(IngestQuota.used).where(IngestQuota.id == 1))).scalar_one())
