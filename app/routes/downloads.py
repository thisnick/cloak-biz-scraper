"""`GET /downloads/{handle}/{name}` — the one door kept downloads go out through.

**Outside the OAuth gate, like `/uploads`, and for a stronger reason.** The
reader is often not the agent at all but the person it is talking to, clicking
a link in a chat client that has no shell and no way to send a header. So the
ticket is accepted in the query string as well as the `Authorization` header —
see services/downloads.py for why that is acceptable here and not for uploads.
It opens exactly one file, for the subject it was minted for, until that file
expires.

**Always an attachment, never a page.** The bytes are whatever a website sent,
and this origin also serves the dashboard and holds its session cookie. So the
file goes out with `Content-Disposition: attachment`, the sniffed type (never
`text/html` — the sniffer cannot produce it), `nosniff`, and a `sandbox` CSP
(response_security.py), so that even a browser persuaded to render it inline
runs nothing as this origin.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..services.downloads import Expired, NotFound

logger = logging.getLogger("cloakbiz.downloads.http")

router = APIRouter()


def _bearer(headers) -> str | None:
    auth = headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


@router.get("/downloads/{handle}/{name}")
async def fetch_download(request: Request, handle: str, name: str,
                         t: str | None = None) -> FileResponse:
    token = _bearer(request.headers) or t
    try:
        path, entry = request.app.state.downloads.open_for(
            handle, name, token, request.app.state.secret.current()
        )
    except Expired as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=str(entry.get("content_type") or "application/octet-stream"),
        filename=str(entry["name"]),
        content_disposition_type="attachment",
    )
