"""The VNC proxy: watching a running browser.

Ported from browserd (the VNC proxy in app/main.py), with the authentication it
did not need and this does — browserd sat on a private network; this is a public
Railway URL.

Step 3 left `vnc_url` always null: there was a display but no VNC stack, and no
subject to bind a token to. Both now exist.

**Why this is guarded as tightly as CDP.** It is tempting to file "live view"
under harmless — it is only pixels. It is not: RFB carries pointer and key
events, so an unfiltered viewer is a person typing into the user's authenticated
browser. And the pixels themselves are the user's logged-in sessions. So the
same rules apply: a signed, short-lived, instance-scoped, subject-bound token,
checked before `accept()`, with the Origin validated.

**The VNC token is a different audience from the CDP token**, which matters more
here than anywhere else in the app: this URL is designed to be dropped into an
`iframe src`, where it reaches the DOM, the referrer header, and the browser
history of anyone who opens the dashboard. That is a far leakier home than an
agent's tool call. If one token opened both doors, the leakiest URL in the
system would also be the most powerful. It does not: a VNC token verifies only
against `vnc:<id>` (services/tokens.py).

**Task-owned browsers are watchable, but view-only** — the one place this
endpoint is deliberately more permissive than CDP, because watching a sweep is
the inspection feature and it cannot corrupt anything once the input events are
gone (services/rfb.py).
"""
from __future__ import annotations

import asyncio
import logging

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import rfb, tokens
from .ws_guard import Denied, authorize

logger = logging.getLogger("cloakbiz.vnc")

router = APIRouter()


@router.websocket("/instances/{instance_id}/vnc")
async def vnc_proxy(ws: WebSocket, instance_id: str, t: str | None = None):
    try:
        inst = authorize(ws.app, ws.headers, t, instance_id, kind=tokens.VNC)
    except Denied as denied:
        # Refused before accept(), so nothing is ever attached to the browser.
        logger.warning("vnc refused for %s: %s", instance_id, denied.reason)
        await ws.close(code=denied.code, reason=denied.reason)
        return

    if not inst.vnc_port:
        await ws.close(code=4004, reason="this browser has no live view")
        return

    view_only = inst.origin == "task"
    inst.touch()
    requested = ws.scope.get("subprotocols", [])
    await ws.accept(subprotocol="binary" if "binary" in requested else None)

    upstream_url = f"ws://127.0.0.1:{inst.vnc_port}/websockify"
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=["binary"],
            origin=f"http://127.0.0.1:{inst.vnc_port}",
            max_size=None, ping_interval=None, ping_timeout=None, compression=None,
        ) as upstream:
            await _pump(ws, upstream, view_only=view_only)
    except Exception as exc:  # noqa: BLE001
        logger.warning("vnc proxy for %s ended: %s", instance_id, exc)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def _pump(ws: WebSocket, upstream, *, view_only: bool) -> None:
    async def client_to_browser():
        # The RFB handshake (version, security, ClientInit) is not made of the
        # client messages the filter understands, and running it through would
        # mangle the negotiation before the session ever starts. It is a fixed
        # three exchanges, and it carries no input events — so it passes
        # untouched even for a view-only viewer.
        handshake = 0
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if not data:
                    continue
                handshake += 1
                if handshake <= 3:
                    await upstream.send(data)
                    continue
                filtered = rfb.filter_client_messages(data, view_only=view_only)
                if filtered and filtered[0] in rfb.RFB_MSG_SIZE:
                    await upstream.send(filtered)
        except (WebSocketDisconnect, Exception):  # noqa: B014
            pass

    async def browser_to_client():
        try:
            async for msg in upstream:
                # KasmVNC's BinaryClipboard (180) is an extension noVNC does not
                # speak; translate it into the standard ServerCutText.
                if isinstance(msg, bytes) and msg and msg[0] == 180:
                    text = rfb.parse_kasmvnc_clipboard(msg)
                    if text:
                        await ws.send_bytes(rfb.build_server_cut_text(text))
                    continue
                if isinstance(msg, bytes):
                    await ws.send_bytes(msg)
                else:
                    await ws.send_text(msg)
        except (WebSocketDisconnect, Exception):  # noqa: B014
            pass

    tasks = [asyncio.create_task(client_to_browser()),
             asyncio.create_task(browser_to_client())]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
