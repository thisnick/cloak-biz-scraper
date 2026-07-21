"""Security and cache policy for every HTTP response.

The application has more response producers than FastAPI route functions:
OAuth delegates to MCP SDK handlers, ``/mcp`` is a raw ASGI endpoint, and the
OAuth bearer guard writes its own errors.  Applying headers at those individual
doors would make the policy depend on each one remembering it.  This middleware
sits outside all of them instead.

The dashboard is intentionally self-contained but uses inline CSS/JavaScript,
and its live viewer imports noVNC and opens a same-origin WebSocket.  The CSP
spells those existing requirements out while still refusing plugins, base-tag
rewrites, off-site form posts, and cross-origin framing.
"""
from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'self'",
        "form-action 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        # noVNC connects back to this deployment over ws:// locally and wss://
        # on Railway.  The socket itself still requires an instance-scoped,
        # subject-bound token and enforces Origin before it accepts.
        "connect-src 'self' ws: wss:",
        "worker-src 'self' blob:",
        "frame-src 'self'",
    )
)

_NO_STORE = "no-store"
_HSTS = "max-age=31536000"
# ``no-referrer`` looks stricter, but Fetch uses the active referrer policy when
# it serializes Origin for non-CORS form submissions.  Chromium therefore sends
# ``Origin: null`` even for our own same-origin POSTs, and the CSRF guard must
# reject that indistinguishable opaque origin.  ``same-origin`` keeps a concrete
# Origin on our forms while still sending no Referer at all to external sites —
# in particular, a live-view URL's short-lived query token never leaves this
# deployment in an off-site navigation.
REFERRER_POLICY = "same-origin"


def _external_scheme(scope: Scope) -> str:
    """Scheme the client used, including Railway's TLS-termination header.

    Only an exact first ``X-Forwarded-Proto`` value is accepted.  HSTS delivered
    over cleartext is ignored by browsers anyway, but an explicit check keeps
    local HTTP honest and avoids claiming a guarantee that connection did not
    have.
    """
    headers = Headers(scope=scope)
    forwarded = (headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded in {"http", "https"}:
        return forwarded
    return str(scope.get("scheme", "http")).lower()


class ResponseSecurity:
    """Attach browser hardening and a deliberate cache policy app-wide."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # WebSocket handshakes have their own token and Origin guard.  An
            # http.response.start wrapper cannot describe an accepted upgrade.
            await self.app(scope, receive, send)
            return

        is_https = _external_scheme(scope) == "https"

        async def send_hardened(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = REFERRER_POLICY
                headers["X-Frame-Options"] = "SAMEORIGIN"
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY

                # Treat every route as sensitive by default so a new endpoint
                # cannot forget to opt in.  This deliberately includes noVNC:
                # its viewer HTML can be opened with a short-lived token in the
                # query string, so publicly caching even that static document
                # would leave a credential-bearing URL in cache metadata.
                headers["Cache-Control"] = _NO_STORE

                if is_https:
                    # Do not includeSubDomains: Railway's parent domain is
                    # shared, and a custom domain's siblings are not ours.
                    headers["Strict-Transport-Security"] = _HSTS
            await send(message)

        await self.app(scope, receive, send_hardened)
