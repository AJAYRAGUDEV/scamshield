"""Structured logging with secret redaction and per-request IDs.

Rules: never log API keys, user messages, audio content, filenames or provider credentials.
Log metadata only (mode, sizes, durations, status codes, error categories).
"""
from __future__ import annotations

import logging
import re
import traceback
import uuid
from contextvars import ContextVar
from time import perf_counter

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-goog-api-key|x-api-key|cookie|set-cookie)\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_SECRET_PARAM_RE = re.compile(r"(?i)\b(api[_-]?key|apikey|key|token|access_token|password|secret|authorization)=([^&\s\"']+)")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
_HANDLER_FLAG = "_scamshield_handler"


_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this exact value wherever it appears in log output (e.g. the configured API key)."""
    if value and len(value.strip()) >= 6:
        _secrets.add(value.strip())


def redact(text: str) -> str:
    """Mask registered secrets and anything shaped like a Google API key."""
    for secret in _secrets:
        text = text.replace(secret, "[redacted secret]")
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}: [redacted]", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} [redacted]", text)
    text = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _KEY_RE.sub("[redacted API key]", text)


def log_traceback(logger: logging.Logger, event: str, exc: BaseException) -> None:
    """DEBUG-only traceback, redacted BEFORE it reaches any handler (handlers other than ours,
    e.g. a root handler, would otherwise see the raw exception text)."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s | %s", event, redact("".join(traceback.format_exception(exc))))


class RedactingFormatter(logging.Formatter):
    """Redacts the FINAL formatted text, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _AccessLogRedactor(logging.Filter):
    """uvicorn's access log prints the request target INCLUDING the query string. Drop the query."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?[redacted]",) + args[3:]
        return True


def configure_logging(level: str = "INFO") -> None:
    """Idempotently configure the ``scamshield`` logger namespace (and redact uvicorn's access log)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogRedactor) for f in access.filters):
        access.addFilter(_AccessLogRedactor())
    logger = logging.getLogger("scamshield")
    logger.setLevel(level.upper())
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler()
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
    handler.addFilter(_RequestIdFilter())
    logger.addHandler(handler)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """``event key=value ...`` lines. Callers must pass metadata only, never user content."""
    parts = [event]
    for key, value in fields.items():
        text = str(value).replace("\n", " ").replace("\r", " ")[:200]
        parts.append(f"{key}={text}")
    logger.log(level, " ".join(parts))


class RequestContextMiddleware:
    """Pure ASGI middleware: request ID (header + log context) and one access line per request."""

    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger("scamshield.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # Contain unhandled errors here instead of letting them propagate: the server would log
            # the raw traceback (exception text may hold secrets). Log metadata + a redacted
            # DEBUG traceback, and give the client a generic message.
            log_event(self.logger, logging.ERROR, "unhandled_exception", type=type(exc).__name__)
            log_traceback(self.logger, "unhandled_exception traceback", exc)
            if not response_started:
                body = b'{"detail":"Internal server error."}'
                await send_with_id({"type": "http.response.start", "status": 500, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send_with_id({"type": "http.response.body", "body": body})
        finally:
            path = scope.get("path", "")
            level = logging.DEBUG if path == "/health" else logging.INFO
            log_event(
                self.logger, level, "request",
                method=scope.get("method"), path=path, status=status,
                duration_ms=round((perf_counter() - started) * 1000, 1),
            )
            request_id_var.reset(token)
