"""Centralized error model.

Two families, handled differently on purpose:

* ``InputError``  - the CALLER sent something unacceptable -> HTTP 4xx with ``{"detail": ...}``
                    (the original API shape, preserved for the existing frontend).
* ``AnalysisError`` - the request was fine but the analysis could not be completed
                    -> HTTP 200 with ``verdict: "unable_to_assess"``, ``risk_score: null`` and a
                    ``reason_code``. It is never turned into a Low Risk result.

User-facing text is generic. Provider messages, paths and stack traces never reach the client.
"""
from __future__ import annotations

from enum import StrEnum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException


class ReasonCode(StrEnum):
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PROVIDER_AUTH_ERROR = "PROVIDER_AUTH_ERROR"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INVALID_PROVIDER_RESPONSE = "INVALID_PROVIDER_RESPONSE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    MEDIA_PROCESSING_FAILED = "MEDIA_PROCESSING_FAILED"
    INTERNAL_ANALYSIS_ERROR = "INTERNAL_ANALYSIS_ERROR"


REASON_MESSAGES: dict[ReasonCode, str] = {
    ReasonCode.CONFIGURATION_ERROR: (
        "The analysis service is not configured correctly. The server administrator must set a valid "
        "GEMINI_API_KEY and GEMINI_MODEL in the server environment."
    ),
    ReasonCode.PROVIDER_AUTH_ERROR: (
        "The analysis service rejected the server's credentials. The server administrator must check "
        "the Gemini API key and its permissions."
    ),
    ReasonCode.PROVIDER_RATE_LIMITED: "The analysis service's usage quota or rate limit was reached. Try again later.",
    ReasonCode.PROVIDER_TIMEOUT: "The analysis service did not respond in time. Try again.",
    ReasonCode.PROVIDER_UNAVAILABLE: "The analysis service could not be reached or is temporarily unavailable. Try again shortly.",
    ReasonCode.PROVIDER_ERROR: "The analysis service reported an error and the content could not be assessed.",
    ReasonCode.INVALID_PROVIDER_RESPONSE: "The analysis service returned a response that could not be validated, so no assessment was made.",
    ReasonCode.INSUFFICIENT_EVIDENCE: (
        "The content could not be understood well enough to judge it, so no assessment was made. "
        "An unreadable or partly understood item is never treated as low risk."
    ),
    ReasonCode.MEDIA_PROCESSING_FAILED: "The uploaded file could not be processed.",
    ReasonCode.INTERNAL_ANALYSIS_ERROR: "An unexpected internal error stopped the analysis.",
}

SAFE_NEXT_STEP = (
    "No assessment was made, so do not treat this content as safe. Verify any urgent request through an "
    "official channel you trust, and try the analysis again later."
)


class ScamShieldError(Exception):
    """Base class for expected application errors."""


def error_response(status_code: int, detail: str, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """The one error body shape: ``{"detail": <short user-safe text>, "code": <stable code>}``."""
    return JSONResponse({"detail": detail, "code": code}, status_code=status_code, headers=headers)


class InputError(ScamShieldError):
    """The request was rejected before analysis. Becomes an HTTP 4xx/503 ``{"detail", "code"}`` response."""

    status_code = 400
    code = "INVALID_INPUT"

    def __init__(self, detail: str, status_code: int | None = None, code: str | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.headers = headers
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class InvalidInput(InputError):
    status_code = 400
    code = "INVALID_INPUT"


class PayloadTooLarge(InputError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"


class UnsupportedMedia(InputError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"


class InvalidVideo(InputError):
    """The file is not a usable video (corrupt, no video stream, unreadable or malformed metadata)."""

    status_code = 422
    code = "INVALID_VIDEO"


class VideoTooLong(InputError):
    status_code = 422
    code = "VIDEO_TOO_LONG"


class VideoValidationUnavailable(InputError):
    """ffprobe is missing or failed to run. Validation fails CLOSED: the upload is refused, never waved through."""

    status_code = 503
    code = "VIDEO_VALIDATION_UNAVAILABLE"

    def __init__(self):
        super().__init__("Video validation is not available on this server right now, so the upload was not accepted.",
                         headers={"Retry-After": "30"})


class NotFound(InputError):
    status_code = 404
    code = "JOB_NOT_FOUND"


class ServiceBusy(InputError):
    """Temporary capacity limit: too many analyses running and waiting. Safe to retry shortly."""

    status_code = 503
    code = "SERVICE_BUSY"

    def __init__(self, retry_after_seconds: int = 5):
        super().__init__("The service is at capacity right now. Please try again in a few seconds.",
                         headers={"Retry-After": str(retry_after_seconds)})


class AnalysisError(ScamShieldError):
    """The analysis could not be completed. Carries a stable, user-safe reason code.

    ``internal_detail`` is for logs only (exception class / HTTP status). It must never contain
    user content or secrets and is never returned to the client.
    """

    def __init__(self, reason_code: ReasonCode, internal_detail: str = ""):
        super().__init__(f"{reason_code}: {internal_detail}" if internal_detail else str(reason_code))
        self.reason_code = reason_code
        self.internal_detail = internal_detail


_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def _retry_after(exc: RateLimitExceeded) -> int:
    try:
        return max(1, int(exc.limit.limit.get_expiry()))
    except Exception:
        return 60


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(InputError)
    async def _input_error(_: Request, exc: InputError) -> JSONResponse:
        return error_response(exc.status_code, exc.detail, exc.code, exc.headers)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return error_response(429, "Too many requests. Please wait a moment and try again.", "RATE_LIMITED",
                              {"Retry-After": str(_retry_after(exc))})

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Names the offending FIELDS only. FastAPI's default body echoes the submitted values back.
        fields = sorted({str(err["loc"][-1]) for err in exc.errors() if err.get("loc")})
        detail = "The request is missing or has an invalid field" + (f": {', '.join(fields)}." if fields else ".")
        return error_response(422, detail, "INVALID_REQUEST")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, detail, _HTTP_CODES.get(exc.status_code, "HTTP_ERROR"), getattr(exc, "headers", None))
