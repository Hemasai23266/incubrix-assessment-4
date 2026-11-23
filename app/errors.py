"""Shared error codes (Appendix A) and the one JSON error envelope (Integration Standard point 5)."""
from typing import Any

# code -> (retryable, meaning)
ERROR_CODES: dict[str, tuple[bool, str]] = {
    "DEST_SPEC_VIOLATION": (False, "Content breaks the destination's format limits (field-level reasons in details)."),
    "DEST_RULE_FREQUENCY_CAP": (True, "Frequency cap reached; rescheduled after the cap window."),
    "CONNECTION_AUTH_EXPIRED": (False, "Token expired or revoked; pause and ask the owner to reconnect."),
    "PLATFORM_RATE_LIMIT": (True, "Platform returned 429; back off using the reset time."),
    "PLATFORM_QUOTA_EXHAUSTED": (True, "Daily quota used up; reschedule to the next window."),
    "PLATFORM_CONTENT_REJECTED": (False, "Platform refused the content; its message is in details."),
    "PLATFORM_UNAVAILABLE": (True, "Platform down; retry, stop after 5 consecutive failures."),
    "CONNECTOR_DISABLED": (False, "Kill switch is on for this connector."),
    # service-level codes
    "DEST_RULE_VIOLATION": (False, "A posting rule (self-promo, link policy, AI disclosure) blocked the post."),
    "DEST_NOT_CERTIFIED": (False, "Destination is not certified/active, so publishing is blocked."),
    "DEST_PERMISSION_REVOKED": (False, "The partner revoked permission for this destination."),
    "UNAUTHORIZED": (False, "Missing or invalid API token."),
    "NOT_FOUND": (False, "Resource not found."),
    "VALIDATION_ERROR": (False, "Request body failed validation."),
    "IDEMPOTENCY_KEY_REUSED": (False, "Idempotency-Key was already used with a different request."),
    "IDEMPOTENCY_KEY_REQUIRED": (False, "POST requires an Idempotency-Key header."),
    "RATE_LIMITED": (True, "Too many requests to this service."),
    "CALLBACK_URL_REJECTED": (False, "callback_url failed the outbound-request safety checks (https, public address, allowed port)."),
    "QUEUE_UNAVAILABLE": (True, "The job queue is unavailable; retry with the same Idempotency-Key."),
    "METHOD_NOT_ALLOWED": (False, "HTTP method not allowed for this route (the allowed methods are listed in details)."),
    "HTTP_ERROR": (False, "Generic HTTP error raised by the framework (status code preserved)."),
    "IDEMPOTENCY_KEY_INVALID": (False, "Idempotency-Key must be 1-200 printable ASCII characters without spaces."),
    "IDEMPOTENCY_IN_PROGRESS": (True, "A request with this Idempotency-Key is still running; retry shortly."),
    "MEDIA_URL_REJECTED": (False, "The URL failed the outbound-request safety checks (scheme, port, address, redirects)."),
    "MEDIA_FETCH_FAILED": (True, "Downloading the URL failed (timeout, connection error or remote 5xx/429)."),
    "MEDIA_TOO_LARGE": (False, "The remote file is larger than the configured maximum."),
    "MEDIA_TYPE_REJECTED": (False, "The remote content type is not allowed or does not match the downloaded bytes."),
    "CONTRACT_VIOLATION": (False, "Platform response no longer matches what the connector expects."),
    "INTERNAL_ERROR": (False, "Unexpected server error."),
}


class AppError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None,
                 status_code: int = 400, retryable: bool | None = None):
        assert code in ERROR_CODES, f"unknown error code {code}"
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        self.retryable = ERROR_CODES[code][0] if retryable is None else retryable
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message,
                          "retryable": self.retryable, "details": self.details}}
