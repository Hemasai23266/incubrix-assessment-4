"""Structured JSON logs with timestamp, level, job_id, correlation id. Secrets are redacted."""
import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="-")

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization|api[-_ ]?key|api-key|token|secret|password|x-incubrix-signature)"
               r"(['\"]?\s*[:=]\s*['\"]?)(?:bearer\s+|apikey\s+)?([^\s'\",;}]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
]


_URL_QUERY = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")


def redact(text: str) -> str:
    text = _URL_QUERY.sub(lambda m: f"{m.group(1)}?[REDACTED]", text)  # query strings/fragments may carry tokens
    text = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    text = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "job_id": job_id_var.get(),
            "correlation_id": correlation_id_var.get(),
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(json.loads(redact(json.dumps(extra, default=str))))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class _StdoutHandler(logging.StreamHandler):
    """Always writes to the CURRENT sys.stdout (container logs, and pytest capture, both work)."""
    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, value):
        pass


def setup_logging() -> None:
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if getattr(h, "_incubrix", False)]
    if not root.handlers:
        handler = _StdoutHandler()
        handler._incubrix = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, /, **fields) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})
