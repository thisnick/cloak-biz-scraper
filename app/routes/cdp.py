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

**The subject binding Step 3 could not have.** The token now carries the OAuth
subject it was minted for, and it is checked against the subject that owns the
instance — a token minted for one owner cannot drive another's browser. With a
single APP_SECRET there is exactly one subject today, so this check is enforced
and tested but cannot fire in production; it is defence in depth for a future
with more than one, not a wall between two users who exist now. Saying it any
more strongly would be the placeholder Step 3 deliberately refused to write.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from ..services import tokens
from ..services.urls import websocket_base
from .ws_guard import Denied, authorize

logger = logging.getLogger("cloakbiz.cdp")

router = APIRouter()

# Chromium answers /json locally and instantly; a slow answer means it is wedged.
_CDP_TIMEOUT_SEC = 5


def _authorize(app, headers, query_token: str | None, instance_id: str):
    return authorize(app, headers, query_token, instance_id, kind=tokens.CDP)


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
    except Denied as denied:
        raise HTTPException(status_code=403 if denied.code != 4004 else 404,
                            detail=denied.reason) from denied
    inst.touch()
    try:
        data = await _cdp_json(inst, "version")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"the browser is not answering: {exc}") from exc

    # websocket_base(), not the request's own scheme: behind Railway's TLS
    # termination request.url.scheme is http, and a ws:// URL handed to a client
    # on an https page is blocked as mixed content. See services/urls.py.
    data["webSocketDebuggerUrl"] = f"{websocket_base(request)}/instances/{instance_id}/cdp"
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
    except Denied as denied:
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
