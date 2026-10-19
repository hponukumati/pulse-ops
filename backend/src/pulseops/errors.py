"""Application errors carrying only safe codes.

Messages are fixed per code: they never include input values, SQL, tokens or stack
traces. ``details`` may contain only batch indices, field names and codes.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "INTERNAL"
    http_status = 500
    message = "Internal error"

    def __init__(self, details: list[dict[str, Any]] | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(self.code)
        self.details = details or []
        self.retry_after = retry_after


class BadRequest(AppError):
    code, http_status, message = "BAD_REQUEST", 400, "Malformed request"


class MalformedJson(AppError):
    code, http_status, message = "MALFORMED_JSON", 400, "Malformed JSON"


class Unauthenticated(AppError):
    code, http_status, message = "UNAUTHENTICATED", 401, "Authentication required"


class InvalidCredential(AppError):
    code, http_status, message = "INVALID_CREDENTIAL", 401, "Invalid credential"


class Forbidden(AppError):
    code, http_status, message = "FORBIDDEN", 403, "Not permitted"


class OriginRejected(AppError):
    code, http_status, message = "ORIGIN_REJECTED", 403, "Request origin not allowed"


class ReauthRequired(AppError):
    code, http_status, message = "REAUTH_REQUIRED", 403, "Recent password confirmation required"


class NotFound(AppError):
    # Used for both absent and inaccessible objects so existence is not revealed.
    code, http_status, message = "NOT_FOUND", 404, "Not found"


class Conflict(AppError):
    code, http_status, message = "CONFLICT", 409, "Conflict"


class PayloadTooLarge(AppError):
    code, http_status, message = "PAYLOAD_TOO_LARGE", 413, "Request body too large"


class UnsupportedMedia(AppError):
    code, http_status, message = "UNSUPPORTED_MEDIA_TYPE", 415, "Unsupported content type or encoding"


class ValidationFailed(AppError):
    code, http_status, message = "VALIDATION_FAILED", 422, "Invalid fields"


class RateLimited(AppError):
    code, http_status, message = "RATE_LIMITED", 429, "Too many requests"


class QuotaExceeded(AppError):
    code, http_status, message = "QUOTA_EXCEEDED", 429, "Quota exceeded"


class DependencyUnavailable(AppError):
    code, http_status, message = "SERVICE_UNAVAILABLE", 503, "Dependency unavailable"


class Timeout(AppError):
    code, http_status, message = "TIMEOUT", 503, "Request timed out"
