"""Opaque keyset cursors.

A cursor encodes only a position (sort value + id). Every list query still filters by
the caller's authorization, so replaying another user's cursor reveals nothing extra.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Any

from pulseops.errors import ValidationFailed

DEFAULT_PAGE = 20
MAX_PAGE = 100


def page_size(first: int | None) -> int:
    if first is None:
        return DEFAULT_PAGE
    if not 1 <= first <= MAX_PAGE:
        raise ValidationFailed([{"field": "first", "code": "out_of_range"}])
    return first


def encode_cursor(sort_value: datetime | str | int, item_id: uuid.UUID) -> str:
    value = sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value
    raw = json.dumps([value, str(item_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None, kind: type) -> tuple[Any, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 200:
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value, item_id = json.loads(raw)
        parsed: Any
        if kind is datetime:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError
        elif kind is int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError
            parsed = value
        else:
            if not isinstance(value, str):
                raise ValueError
            parsed = value
        return parsed, uuid.UUID(item_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationFailed([{"field": "after", "code": "invalid_cursor"}]) from exc
