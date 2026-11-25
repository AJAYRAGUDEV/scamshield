"""Translate Gemini SDK / transport exceptions into provider-neutral ``AnalysisError``s.

Matching is by HTTP status code and by exception class names (walking the MRO) because the SDK
may raise either ``httpx`` or ``httpx2`` exceptions. The produced ``internal_detail`` contains only
the exception class name and status code, so it is safe to log.
"""
from __future__ import annotations

import socket

from app.core.errors import AnalysisError, ReasonCode

_KEY_REJECTED_MARKERS = ("api key not valid", "api_key_invalid", "api key expired", "api key not found")
_NETWORK_NAME_MARKERS = ("connecterror", "connectionerror", "networkerror", "transporterror", "remoteprotocolerror")


def _http_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def translate_exception(exc: BaseException) -> AnalysisError:
    if isinstance(exc, AnalysisError):
        return exc

    code = _http_code(exc)
    names = {cls.__name__.lower() for cls in type(exc).__mro__}
    detail = f"{type(exc).__name__}" + (f" http={code}" if code is not None else "")

    def err(reason: ReasonCode) -> AnalysisError:
        return AnalysisError(reason, detail)

    if isinstance(exc, TimeoutError) or any("timeout" in n for n in names) or code in (408, 504):
        return err(ReasonCode.PROVIDER_TIMEOUT)
    if code in (401, 403):
        return err(ReasonCode.PROVIDER_AUTH_ERROR)
    if code == 429:
        return err(ReasonCode.PROVIDER_RATE_LIMITED)
    if code == 404:  # model id unknown / unavailable to this key: a deployment configuration problem
        return err(ReasonCode.CONFIGURATION_ERROR)
    if code == 400:
        # Google reports an invalid key as HTTP 400. Used for classification only, never logged.
        if any(marker in str(exc).lower() for marker in _KEY_REJECTED_MARKERS):
            return err(ReasonCode.PROVIDER_AUTH_ERROR)
        return err(ReasonCode.PROVIDER_ERROR)
    if code is not None and 500 <= code <= 599:
        return err(ReasonCode.PROVIDER_UNAVAILABLE)
    if code is not None:
        return err(ReasonCode.PROVIDER_ERROR)
    if isinstance(exc, (ConnectionError, socket.gaierror)) or any(m in n for n in names for m in _NETWORK_NAME_MARKERS):
        return err(ReasonCode.PROVIDER_UNAVAILABLE)
    return err(ReasonCode.PROVIDER_ERROR)
