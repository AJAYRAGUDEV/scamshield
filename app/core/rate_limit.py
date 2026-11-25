"""Rate limiting with slowapi.

Per-client limits use ``make_client_key`` (the TCP peer; proxy headers only from trusted proxies).
Analysis routes also share ONE global limit across all clients, so many addresses together cannot
exhaust the Gemini quota.

Storage is in memory: counters live in this process only. That is correct for a single-process
development or small deployment; with several workers/servers each would count separately, and a
shared store (e.g. Redis) would be required. Counters reset on restart.
"""
from __future__ import annotations

from slowapi import Limiter

from app.core.config import Settings
from app.core.security import GLOBAL_KEY, make_client_key


def build_limiter(settings: Settings) -> Limiter:
    return Limiter(
        key_func=make_client_key(settings),
        storage_uri="memory://",
        enabled=settings.rate_limit_enabled,
        headers_enabled=False,               # our 429 handler sets Retry-After itself
    )


def global_key(request) -> str:     # the parameter MUST be named "request": slowapi inspects the signature
    return GLOBAL_KEY
