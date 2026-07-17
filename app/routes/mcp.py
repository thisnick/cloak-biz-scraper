"""The `/mcp` endpoint: transport rules the SDK does not enforce for us.

This is a thin ASGI wrapper around the SDK's stateless session manager rather
than a mounted sub-application, because two of the endpoint's requirements are
ours to keep and both were established by probing the SDK, not by reading it:

* **`GET` must be refused with 405.** The SDK's GET handler opens a
  server-initiated SSE stream and holds it open — verified: the request simply
  hangs. The spec makes that stream optional, and a stateless server has nothing
  to send down one; a client waiting on it would wait forever.
* **`Origin` must be validated on every request** (the spec says MUST; the
  attack is DNS rebinding). The SDK ships this switched off by default, and when
  switched on it wants a static allowlist of hostnames — which we cannot write,
  because Railway assigns the deployment's domain and the user never tells us
  what it is.

**The policy, and what it does and does not buy.** A request with no `Origin` is
allowed: that is every server-side MCP client, including ChatGPT and Claude, and
it is not the attack — a browser always sends `Origin` on a cross-origin request.
A request whose `Origin` names a different host than the one it was sent to is
refused, which is what stops a page on evil.example from driving this server
through a logged-in user's browser. What this does not stop on its own is an
attacker who rebinds their own hostname to our address, since then `Origin` and
`Host` agree; defending that needs a known-hosts list, which is exactly what we
cannot have here. It matters far less than it would for a localhost server:
this deployment is already reachable from the internet, so an attacker gains no
network position by going through a victim's browser — and from Step 4 they
would still need a bearer token they cannot read cross-origin.
"""
from __future__ import annotations

import json
import logging
import os

from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

logger = logging.getLogger("cloakbiz.mcp.http")

# An escape hatch for a browser-based client on a known origin (the MCP
# Inspector, a custom dashboard). Empty by default: every mainstream client is
# server-side and sends no Origin at all, so this stays unset for almost everyone.
_EXTRA_ORIGINS = tuple(
    o.strip().rstrip("/") for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
)


def _host_of(origin: str) -> str:
    """The host[:port] of an Origin header value, lowercased."""
    return origin.split("://", 1)[-1].rstrip("/").lower()


def _is_loopback(host: str) -> bool:
    """Whether a host[:port] names this machine.

    Textual on purpose: no DNS lookup. Resolving the name here would *be* the
    rebinding vulnerability — the attacker owns the record and would simply
    answer 127.0.0.1.
    """
    name = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
    return name in ("127.0.0.1", "localhost", "::1") or name.startswith("127.")


def origin_allowed(headers: Headers) -> bool:
    """Whether a request may proceed, on Origin grounds alone.

    Two rules, and the second is the Step 3 review's recommendation:

    1. `Origin` must name the same host it was sent to. A request with no
       `Origin` is allowed: that is every server-side MCP client, including
       ChatGPT and Claude, and it is not the attack — a browser always sends
       `Origin` cross-origin.
    2. **If the `Host` is loopback, the `Origin` must be loopback too**, and this
       one outranks the operator's own allowlist. On a laptop or a LAN box,
       `MCP_ALLOWED_ORIGINS` is the foot-gun: point it at a site you trust and
       that site — or anything that can inject a script into it — can now reach
       a server bound to your localhost, which is the one place a browser was
       never supposed to be able to go. A remote deployment is unaffected,
       because its Host is a real domain.

    **What this still does not stop, stated plainly:** DNS rebinding, where the
    attacker points `evil.example` at 127.0.0.1 so that `Origin` and `Host` are
    *both* `evil.example` and agree. Nothing checkable here can tell that from a
    legitimate Railway request, because we are not allowed to know our own
    domain (Railway assigns it; the user sets one variable). A static allowlist
    is what would catch it, and it is exactly what FastMCP's `allowed_hosts`
    tried to be — and why it 421'd every real request in Step 3.

    **The reason that residue is acceptable is the gate this step adds.** Since
    Step 4, `/mcp` requires a bearer token. A rebound page passes the Origin
    check and then gets a 401, because the token lives in the client's storage
    on a different origin and the same-origin policy will not hand it over. The
    Origin rule is defence in depth on top of that; it is not what is holding
    the door.
    """
    origin = headers.get("origin")
    if not origin:
        return True  # not a browser; not the attack this defends against
    origin = origin.strip().rstrip("/")
    host = (headers.get("host") or "").strip().lower()
    if _is_loopback(host) and not _is_loopback(_host_of(origin)):
        return False
    if origin.lower() in (o.lower() for o in _EXTRA_ORIGINS):
        return True
    return bool(host) and _host_of(origin) == host


async def _json_error(send: Send, status: int, message: str, allow: str = "") -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if allow:
        headers.append((b"allow", allow.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class MCPEndpoint:
    """ASGI app enforcing the transport rules, then delegating to the SDK.

    Takes a *getter* rather than a manager because the SDK's session manager is
    single-use: its `run()` raises if entered twice, so one built at import time
    would tie the process to a single lifespan — fine for a container that boots
    once, but it means the app cannot be started twice in one process, which is
    what every test that constructs a client does. The manager is therefore
    created per lifespan and looked up per request.
    """

    def __init__(self, get_manager) -> None:
        self._get_manager = get_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        headers = Headers(scope=scope)
        method = scope.get("method", "")

        if not origin_allowed(headers):
            logger.warning("rejected /mcp request from origin %r", headers.get("origin"))
            await _json_error(
                send, 403,
                "This request came from a web page on another site, which is not allowed. "
                "If you are connecting an MCP client, it should call this server directly "
                "rather than from a browser.",
            )
            return

        if method == "GET":
            await _json_error(
                send, 405,
                "This server does not open a server-initiated event stream. It is stateless: "
                "send each message as its own POST to /mcp.",
                allow="POST",
            )
            return

        manager = self._get_manager()
        if manager is None:
            await _json_error(send, 503, "The server is still starting. Try again shortly.")
            return
        await manager.handle_request(scope, receive, send)
