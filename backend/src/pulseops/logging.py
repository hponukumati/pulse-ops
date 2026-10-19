"""Allowlisted structured logging.

Only known-safe fields are emitted. Free-text messages from third-party libraries
(uvicorn, SQLAlchemy, asyncpg, Strawberry, redis) are *dropped*: their messages can
contain SQL bind values, request lines or exception text built from input. Exceptions
are reduced to their class name. Redaction is not relied upon.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

# Fields allowed in log lines. Values must be codes, ints, floats or internal IDs.
ALLOWED_FIELDS = frozenset(
    {
        "event",
        "request_id",
        "status",
        "duration_ms",
        "method",
        "route",
        "service_id",
        "user_id",
        "rule_id",
        "incident_id",
        "job_id",
        "worker_id",
        "code",
        "error_class",
        "count",
        "attempt",
        "slot",
        "pending",
        "version",
    }
)

# Route labels are fixed strings chosen by the app, never the raw request path.
_MAX_VALUE_LEN = 80


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:_MAX_VALUE_LEN]


class AllowlistJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
        }
        fields = getattr(record, "pulseops_fields", None)
        if record.name.startswith("pulseops") and isinstance(fields, dict):
            for key, value in fields.items():
                if key in ALLOWED_FIELDS:
                    out[key] = _safe_value(value)
        else:
            # Third-party record: keep only logger name; never the message or args.
            out["event"] = "third_party_log"
            out["code"] = record.name[:_MAX_VALUE_LEN]
        if record.exc_info and record.exc_info[0] is not None:
            out["error_class"] = record.exc_info[0].__name__
        return json.dumps(out, separators=(",", ":"))


_logger = logging.getLogger("pulseops")


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit an allowlisted event. Unknown keys are silently dropped by the formatter."""
    fields["event"] = event
    _logger.log(level, event, extra={"pulseops_fields": fields})


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AllowlistJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("pulseops").setLevel(level)
    # Third-party loggers stay at WARNING and are message-stripped by the formatter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "asyncpg", "strawberry", "redis"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
        lg.setLevel(logging.WARNING)
    # Access logs are produced by our own middleware with allowlisted fields only.
    logging.getLogger("uvicorn.access").disabled = True


def uvicorn_log_config() -> dict[str, Any]:
    """Uvicorn log config that routes everything through the allowlist formatter."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"allowlist": {"()": "pulseops.logging.AllowlistJsonFormatter"}},
        "handlers": {"default": {"class": "logging.StreamHandler", "formatter": "allowlist", "stream": "ext://sys.stdout"}},
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "uvicorn.access": {"handlers": [], "level": "CRITICAL", "propagate": False},
        },
    }
