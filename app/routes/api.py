"""The REST façade.

Mirrors every MCP tool over the same service layer and the same view builders,
because the dashboard, the VNC view, and the CDP proxy need REST and WebSockets
regardless — so the choice was never "REST or MCP", it was "one implementation
behind two doors, or two implementations that drift".

Nothing here decides anything. A route reads its inputs, calls a service, and
returns what the service returned. Every payload on this side is built by the
same `ScrapeResult.of` / `instance_view` the tools use, which is what makes
"MCP and REST return identical payloads" a property of the code rather than a
thing we remembered to keep true.

Every route here requires an OAuth access token. The check is not in this module
— it is one middleware above the whole router (routes/guard.py), so `/api/*` and
`/mcp` cannot end up with two different ideas of who is allowed in. What this
module does with the result is stamp the caller's subject onto the browsers it
launches, so the CDP and VNC URLs minted later can be bound to whoever asked.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..models import ArchiveResult, InstanceCreate, InstanceView, ScrapeResult
from ..services.geo import GeoUnresolved, ProxyUnreachable
from ..services.instances import CapExceeded, PinUnavailable
from ..services.proxy import ProxyNotConfigured
from ..services.scrape import NotionNotConfigured
from ..services.tokens import OWNER
from ..services.urls import public_base
from ..services.views import instance_view
from ..sources import UnsupportedURL
from .guard import subject_of

logger = logging.getLogger("cloakbiz.api")

router = APIRouter(prefix="/api")


class ScrapeRequest(BaseModel):
    url: str
    max_pages: int = 1
    sync: bool = False
    db_id: str | None = None


class ArchiveRequest(BaseModel):
    url: str
    notion_page_id: str


def _base_url(request: Request) -> str:
    # Not str(request.base_url): behind Railway's TLS termination that says
    # http://, and the ws:// URL it produces is blocked as mixed content on an
    # https page. See services/urls.py.
    return public_base(request)


def _subject(request: Request) -> str:
    """Who is asking. The guard has already refused anyone without a token, so
    this is never a guess — `or OWNER` covers only the single-subject default."""
    return subject_of(request) or OWNER


@router.post("/scrape", response_model=ScrapeResult)
async def scrape_listings(request: Request, body: ScrapeRequest) -> ScrapeResult:
    """Start a sweep. Returns immediately; collect with GET /api/scrape/{job_id}."""
    try:
        job = request.app.state.scrape.start(
            body.url, max_pages=body.max_pages, sync=body.sync, db_id=body.db_id
        )
    except UnsupportedURL as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except NotionNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ScrapeResult.of(job)


@router.get("/scrape/{job_id}", response_model=ScrapeResult)
async def get_scrape_listing_results(request: Request, job_id: str) -> ScrapeResult:
    """Collect a sweep. Never blocks."""
    result = request.app.state.scrape.result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No sweep with job_id={job_id!r}. Results are kept for two weeks.",
        )
    return result


@router.post("/archive", response_model=ArchiveResult)
async def archive_page(request: Request, body: ArchiveRequest) -> ArchiveResult:
    """Read a page and append it to a Notion page. Blocking, ~40-60s."""
    return await request.app.state.archive.archive(body.url, body.notion_page_id)


@router.post("/instances", response_model=InstanceView)
async def create_instance(request: Request, body: InstanceCreate) -> InstanceView:
    subject = _subject(request)
    try:
        inst = await request.app.state.instances.launch(
            body, origin="interactive", subject=subject
        )
    except CapExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ProxyNotConfigured, ProxyUnreachable, GeoUnresolved, PinUnavailable) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return instance_view(
        inst, secret=request.app.state.secret.current(), base_url=_base_url(request),
        subject=subject,
    )


@router.get("/instances", response_model=list[InstanceView])
async def list_instances(request: Request) -> list[InstanceView]:
    secret = request.app.state.secret.current()
    base = _base_url(request)
    subject = _subject(request)
    return [
        instance_view(i, secret=secret, base_url=base, subject=subject)
        for i in request.app.state.instances.running.values()
    ]


@router.get("/instances/{instance_id}", response_model=InstanceView)
async def get_instance(request: Request, instance_id: str) -> InstanceView:
    inst = request.app.state.instances.get(instance_id)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"No running browser {instance_id!r}.")
    return instance_view(
        inst, secret=request.app.state.secret.current(), base_url=_base_url(request),
        subject=_subject(request),
    )


@router.delete("/instances/{instance_id}")
async def close_instance(request: Request, instance_id: str) -> dict:
    return {
        "ok": await request.app.state.instances.stop(instance_id),
        "instance_id": instance_id,
    }
