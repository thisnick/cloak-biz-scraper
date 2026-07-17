"""The gate in front of the tool surface.

Until this existed, `/mcp` and `/api/*` answered 200 to anyone who found the
URL: drive the browser, read and write the user's Notion, spend their proxy
quota. This is the thing that lets the repo go public and the service go on a
real address.

**One middleware, not one guard per door.** `/api/*` is FastAPI routes and
`/mcp` is a raw ASGI endpoint, so the obvious implementations differ — a
dependency for one, something else for the other. That is precisely how two
doors onto one service layer end up with two auth rules and a gap between them.
The rule lives here once, above both, and neither route can forget it.

**What is deliberately NOT behind this gate:**

* `/healthz` — Railway's healthcheck has no credential to offer, and a
  deployment that fails its healthcheck is a deployment that never boots.
* `/.well-known/*`, `/register`, `/authorize`, `/token` — the machinery for
  *getting* a token cannot itself require one.
* the settings UI — cookie sessions, checked in routes/ui.py.
* `/instances/{id}/cdp` and `/vnc` — their own short-lived tokens
  (services/tokens.py). They are WebSockets, where the client usually cannot
  send an Authorization header at all, which is the whole reason that separate
  token exists.

**The 401 must teach the client how to fix itself.** An MCP client that gets a
bare 401 gives up; one that gets `WWW-Authenticate` with `resource_metadata`
follows it to discovery, registers, and comes back with a token. That header is
load-bearing protocol, not decoration — it is the difference between "add this
connector" working and the user seeing an unexplained failure.

**Cookies are not accepted here.** The UI session is for the UI. Honouring it on
a JSON API would make every `/api/*` route CSRF-reachable from any page the
owner has open, in exchange for convenience nothing currently needs — the
settings pages are server-rendered and call no API.
"""
from __future__ import annotations

import json
import logging

from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

from ..services.urls import base_from

logger = logging.getLogger("cloakbiz.guard")

# The key the rest of the app reads the caller's identity from. Set only after a
# token has verified, so its presence *is* the proof.
ACCESS_SCOPE_KEY = "cbs_access"

_RESOURCE_METADATA = "/.well-known/oauth-protected-resource/mcp"


def _protected(path: str) -> bool:
    return path == "/mcp" or path.startswith("/api/") or path == "/api"


class AuthGuard:
    """Requires a live OAuth access token on the tool surface."""

    def __init__(self, app, get_provider) -> None:
        self.app = app
        self._get_provider = get_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _protected(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        provider = self._get_provider()
        if provider is None:
            await _error(send, 503, "service_unavailable", "The server is still starting.")
            return

        headers = Headers(scope=scope)
        auth = headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None

        access = provider.verify_access(token)
        if access is None:
            base = base_from(headers, scope.get("scheme", "http"))
            logger.info("401 on %s: %s", scope.get("path"),
                        "no bearer token" if not token else "invalid or expired token")
            await _error(
                send, 401, "invalid_token",
                "Authentication required. This server uses OAuth 2.1; register and "
                "authorize at the endpoints named in its metadata.",
                resource_metadata=f"{base}{_RESOURCE_METADATA}",
            )
            return

        # The identity every downstream mint hangs off: the CDP and VNC URLs
        # handed out by this request are bound to this subject.
        scope[ACCESS_SCOPE_KEY] = access
        await self.app(scope, receive, send)


def subject_of(request) -> str | None:
    """The authenticated subject behind a request, or None if unauthenticated.

    Reads the scope key the guard sets, which only exists after a token
    verified — so this can never report a subject nobody proved.
    """
    access = request.scope.get(ACCESS_SCOPE_KEY) if hasattr(request, "scope") else None
    return getattr(access, "subject", None)


async def _error(send: Send, status: int, error: str, description: str,
                 resource_metadata: str | None = None) -> None:
    body = json.dumps({"error": error, "error_description": description}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        parts = [f'error="{error}"', f'error_description="{description}"']
        if resource_metadata:
            parts.append(f'resource_metadata="{resource_metadata}"')
        headers.append((b"www-authenticate", f"Bearer {', '.join(parts)}".encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
