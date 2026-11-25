"""Health and generic status routes.

``/health`` is a liveness probe. ``/api/v1/status`` tells the dashboard whether analysis is available
("ready" / "not_ready"). It says nothing about WHY and never mentions credentials. Both are rate limited.

NOTE: no ``from __future__ import annotations`` here (or in the other route modules): the rate-limit
decorators wrap the handlers, and FastAPI must be able to resolve their real annotations.
"""
from fastapi import APIRouter, Request

from app.core.config import Settings


def build_router(limiter, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["health"])
    limit = limiter.limit(settings.rate_limit_default)

    @router.get("/health")
    @router.head("/health", include_in_schema=False)           # uptime monitors (e.g. UptimeRobot free) probe with HEAD
    @limit
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/v1/status")
    @limit
    async def status(request: Request) -> dict[str, str]:
        ready = request.app.state.settings.gemini_configured
        return {"status": "ready" if ready else "not_ready"}

    return router
