"""Incident change notifications (Redis Pub/Sub). Payloads carry IDs only."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

CHANNEL = "pulseops:incident-changes"


@dataclass(frozen=True)
class Note:
    service_id: uuid.UUID
    incident_id: uuid.UUID
    event_id: uuid.UUID
    version: int
    size: int


def parse_note(raw: bytes | str) -> Note | None:
    """Strictly parse a notification published by the outbox dispatcher (ids only)."""
    try:
        d = json.loads(raw)
        if not isinstance(d, dict) or set(d) != {"serviceId", "incidentId", "eventId", "version"}:
            return None
        v = d["version"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            return None
        return Note(uuid.UUID(d["serviceId"]), uuid.UUID(d["incidentId"]), uuid.UUID(d["eventId"]), v, len(raw))
    except (ValueError, TypeError, AttributeError):
        return None
