"""The CDP proxy: driving a running browser from outside the container.

Ported from browserd (the WS proxy in app/main.py), with the authentication it
did not need and this does — browserd sat on a private network; this is a public
Railway URL.

**What is being exposed.** CDP is total control of a browser that holds the
user's residential proxy credentials and every cookie it has collected. An
unauthenticated CDP endpoint is not "a debug feature", it is a remote browser
someone else can drive as the user. So every upgrade must present a token that
is machine-minted, scoped to one instance, and short-lived (services/tokens.py),
and the token is checked before the socket is accepted — never after.

**Task-owned instances are refused outright**, even with a valid token. A sweep's
browser is mid-navigation on a schedule of its own; attaching a debugger to it
would corrupt the sweep and confuse the attacher. Interactive instances exist
precisely to be driven, which is why the pool keeps a reserve of them.

Step 4 adds the OAuth subject to the token's claims so one user's URL cannot be
replayed by another. Until then there are no subjects, and what is enforced here
— signature, expiry, instance scope, and Origin — is enforced for real.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..services import tokens
from .mcp import origin_allowed

logger = logging.getLogger("cloakbiz.cdp")

router = APIRouter()

# Chromium answers /json locally and instantly; a slow answer means it is wedged.
_CDP_TIMEOUT_SEC = 5


class CDPDenied(Exception):
    """Close codes are the only thing a WS client can read on a refused upgrade."""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


def _bearer(request_headers) -> str | None:
    auth = request_headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _authorize(app, headers, query_token: str | None, instance_id: str):
    """Everything that must be true before a socket is accepted.

    The token may arrive in the query string or as a Bearer header: WS clients
    frequently cannot set headers, which is the whole reason the URL carries one
    — but a client that can should not be forced into the leakier option.
    """
    if not origin_allowed(headers):
        raise CDPDenied(4403, "origin not allowed")

    secret = app.state.secret.current()
    token = query_token or _bearer(headers)
    if not tokens.verify(token, instance_id, secret):
        # Deliberately one message for missing, expired, forged, and
        # wrong-instance. Which one it was is only useful to someone who should
        # not have been here.
        raise CDPDenied(4401, "invalid or expired token")

    inst = app.state.instances.get(instance_id)
    if inst is None:
        raise CDPDenied(4004, "no such instance")
    if inst.origin == "task":
        raise CDPDenied(4003, "this browser belongs to a running sweep")
    return inst


async def _cdp_json(inst, path: str):
    async with httpx.AsyncClient(timeout=_CDP_TIMEOUT_SEC) as c:
        r = await c.get(f"http://127.0.0.1:{inst.cdp_port}/json/{path}")
        r.raise_for_status()
        return r.json()


@router.get("/instances/{instance_id}/cdp/json/version")
async def cdp_version(request: Request, instance_id: str, t: str | None = None):
    """What a CDP client fetches first. The upstream browser's own WS URL is
    rewritten to point back through this proxy — the real one is a loopback
    address inside the container and useless to the caller."""
    try:
        inst = _authorize(request.app, request.headers, t, instance_id)
    except CDPDenied as denied:
        raise HTTPException(status_code=403 if denied.code != 4004 else 404,
                            detail=denied.reason) from denied
    inst.touch()
    try:
        data = await _cdp_json(inst, "version")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"the browser is not answering: {exc}") from exc

    host = request.headers.get("host", "")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    data["webSocketDebuggerUrl"] = f"{scheme}://{host}/instances/{instance_id}/cdp"
    if t:
        data["webSocketDebuggerUrl"] += f"?t={t}"
    return data


async def _pump(ws: WebSocket, upstream) -> None:
    """Shuttle frames both ways until either side stops."""

    async def downstream_to_browser():
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except (WebSocketDisconnect, Exception):  # noqa: B014
            pass

    async def browser_to_downstream():
        try:
            async for frame in upstream:
                if isinstance(frame, str):
                    await ws.send_text(frame)
                else:
                    await ws.send_bytes(frame)
        except (WebSocketDisconnect, Exception):  # noqa: B014
            pass

    tasks = [asyncio.create_task(downstream_to_browser()),
             asyncio.create_task(browser_to_downstream())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()


@router.websocket("/instances/{instance_id}/cdp")
async def cdp_proxy(ws: WebSocket, instance_id: str, t: str | None = None):
    try:
        inst = _authorize(ws.app, ws.headers, t, instance_id)
    except CDPDenied as denied:
        # Refused before accept(), so nothing is ever attached to the browser.
        logger.warning("cdp refused for %s: %s", instance_id, denied.reason)
        await ws.close(code=denied.code, reason=denied.reason)
        return

    inst.touch()
    try:
        version = await _cdp_json(inst, "version")
        target = version["webSocketDebuggerUrl"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("cdp unreachable for %s: %s", instance_id, exc)
        await ws.close(code=1011, reason="the browser is not answering")
        return

    await ws.accept()
    try:
        # max_size=None: CDP screenshots and DOM snapshots blow past the default
        # 1 MB frame cap. No pings: the client owns liveness, and a ping timeout
        # here would drop a working session mid-command.
        async with websockets.connect(
            target, max_size=None, ping_interval=None, ping_timeout=None
        ) as upstream:
            await _pump(ws, upstream)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cdp proxy for %s ended: %s", instance_id, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
