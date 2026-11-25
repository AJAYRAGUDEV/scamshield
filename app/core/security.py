"""Web-layer protections that must run BEFORE the application parses anything.

* ``BodyLimitMiddleware``      - request-body size limits that do not depend on Starlette's multipart
                                 parser (which would otherwise read the whole upload first).
* ``SecurityHeadersMiddleware`` - baseline response headers and the Content-Security-Policy.
* ``make_client_key``          - the rate-limit identity of a request (never trusts a spoofable header).
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Callable

from starlette.requests import Request

from app.core.config import Settings
from app.core.errors import error_response
from app.core.logging import log_event

logger = logging.getLogger("scamshield.security")

MULTIPART_OVERHEAD_BYTES = 64 * 1024      # boundaries, headers and the small form fields around a file
DEFAULT_BODY_LIMIT_BYTES = 64 * 1024      # any other route
URLENCODED_WORST_CASE = 12                # one 4-byte UTF-8 character becomes %XX%XX%XX%XX


def body_limits(settings: Settings) -> dict[str, int]:
    """Maximum request-body bytes per path. Both spellings of the video route are limited too."""
    audio = settings.max_audio_bytes + MULTIPART_OVERHEAD_BYTES
    video = settings.max_video_bytes + MULTIPART_OVERHEAD_BYTES
    text = settings.max_text_length * URLENCODED_WORST_CASE + 16 * 1024
    return {
        "/api/analyze-audio": audio,
        "/api/v1/analyze/audio": audio,
        "/api/analyze-text": text,
        "/api/v1/analyze/text": text,
        "/api/v1/analyze/video": video,
        "/api/analyze-video": video,
    }


class BodyLimitMiddleware:
    """Layered request-size protection (pure ASGI, so it works before FastAPI parses the body).

    1. ``Content-Length`` present and too large  -> 413 immediately; the application never runs.
    2. ``Content-Length`` malformed              -> 400.
    3. No/understated ``Content-Length`` (chunked, or lying) -> bytes are COUNTED as they stream in;
       once the limit is exceeded the application is told the client disconnected (it stops reading)
       and whatever it answers is replaced by a 413.
    The size of an accepted upload is checked a third time while it is copied to disk
    (``app/utils/temp_files.py``).
    """

    def __init__(self, app, settings: Settings):
        self.app = app
        self.limits = body_limits(settings)

    def limit_for(self, path: str) -> int:
        return self.limits.get(path, DEFAULT_BODY_LIMIT_BYTES)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope.get("path", ""))
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                size = int(declared)
                if size < 0:
                    raise ValueError
            except ValueError:
                await error_response(400, "The Content-Length header is invalid.", "INVALID_REQUEST")(scope, receive, send)
                return
            if size > limit:
                log_event(logger, logging.WARNING, "body_rejected", reason="content_length", declared=size, limit=limit)
                await self._too_large(limit)(scope, receive, send)
                return

        state = {"seen": 0, "exceeded": False, "responded": False}

        async def limited_receive():
            message = await receive()
            if message["type"] == "http.request":
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > limit:
                    state["exceeded"] = True
                    return {"type": "http.disconnect"}      # the application stops reading
            return message

        async def guarded_send(message):
            if state["exceeded"]:
                if not state["responded"]:
                    state["responded"] = True
                    log_event(logger, logging.WARNING, "body_rejected", reason="streamed", seen=state["seen"], limit=limit)
                    await self._too_large(limit)(scope, receive, send)
                return                                      # swallow anything the application tried to send
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not state["exceeded"]:
                raise
        if state["exceeded"] and not state["responded"]:
            state["responded"] = True
            await self._too_large(limit)(scope, receive, send)

    @staticmethod
    def _too_large(limit: int):
        mb = limit / (1024 * 1024)
        return error_response(413, f"The request is too large (limit about {mb:.1f} MB).", "PAYLOAD_TOO_LARGE")


# ----------------------------------------------------------------------------- security headers
# Since Phase 6 the page has NO inline <script>: its code is in external modules, so script-src does not need
# 'unsafe-inline'. style-src still does: the Tailwind Play CDN injects <style> elements at runtime and the markup
# and JavaScript use style="" attributes. See docs/security.md for what removing that would take.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"


class SecurityHeadersMiddleware:
    def __init__(self, app, settings: Settings, csp: str | None = None):
        self.app = app
        self.hsts = settings.enable_hsts
        self.csp = CONTENT_SECURITY_POLICY if csp is None else csp

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        https = scope.get("scheme") == "https"

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                content_type = next((v for n, v in headers if n.lower() == b"content-type"), b"").decode("latin-1")

                def add(name: str, value: str) -> None:
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode("latin-1")))

                add("x-content-type-options", "nosniff")
                add("referrer-policy", "no-referrer")
                add("permissions-policy", PERMISSIONS_POLICY)
                add("x-frame-options", "DENY")
                if path.startswith("/api/"):
                    add("cache-control", "no-store")             # analysis results are not cacheable
                elif path.startswith(("/js/", "/css/")):
                    add("cache-control", "no-cache")             # always revalidate (ETag) so edits are never stale
                if content_type.startswith("text/html") and self.csp:
                    add("content-security-policy", self.csp)
                if self.hsts and https:
                    add("strict-transport-security", "max-age=31536000; includeSubDomains")
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ----------------------------------------------------------------------------- rate-limit identity
def _in_networks(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def make_client_key(settings: Settings) -> Callable[[Request], str]:
    """The identity used for per-client rate limits.

    By default this is the TCP peer address, which a client cannot change. ``X-Forwarded-For`` is read
    ONLY when the direct peer is listed in ``TRUSTED_PROXY_IPS`` (i.e. the operator has said a reverse
    proxy sits in front); even then the right-most address that is not itself a trusted proxy is used,
    so a client-supplied prefix cannot choose its own identity.
    """
    trusted = tuple(ipaddress.ip_network(item, strict=False) for item in settings.trusted_proxy_ips)

    def key(request: Request) -> str:
        peer = request.client.host if request.client and request.client.host else "unknown"
        if not trusted or not _in_networks(peer, trusted):
            return peer
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                return peer                       # malformed header: fall back to the proxy address
            if not _in_networks(hop, trusted):
                return hop
        return peer

    return key


GLOBAL_KEY = "all-clients"
