"""The MCP façade.

Stateless Streamable HTTP at a single `/mcp`, with no `Mcp-Session-Id`. Stateless
because the protocol is moving that way (the 2026-07-28 RC drops sessions and the
handshake entirely) and because it is the only shape that survives scale-to-zero:
a session id would pin a conversation to a process that Railway is entitled to
stop between two tool calls.

Every tool here is a façade over `services/` and contains no logic of its own.
The REST routes call the same services and the same view builders, so the two
doors cannot drift apart.

Two things the SDK does not do for us, both verified by probing rather than
reading:

* **`GET /mcp` opens an SSE stream; it does not 405.** The spec permits refusing
  it and we do, in `routes/mcp.py`, because a stateless server has nothing to
  say on a server-initiated stream — holding one open would be a promise we
  never keep.
* **DNS-rebinding protection is off by default** and, when enabled, wants a
  static allowlist of hosts we cannot know: the deployment's public domain is
  assigned by Railway. Origin validation therefore lives in `routes/mcp.py` too,
  where it can be checked against the request's own host.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .models import ArchiveResult, InstanceCreate, InstanceView, ScrapeResult
from .services.views import instance_view

logger = logging.getLogger("cloakbiz.mcp")

INSTRUCTIONS = """\
Finds business-for-sale listings and files them into Notion.

Sweeps are asynchronous: scrape_listings starts one and returns immediately with
a job_id, then get_scrape_listing_results collects it. A sweep takes a few
minutes, so the first collect will often still say "working" — wait and call
again rather than starting a second sweep.

Money fields are reported exactly as the listing said them ("$1,258,000",
"Not Disclosed", "$81,000 + Inventory"). They are strings, not numbers, on
purpose: the card is quoted rather than interpreted.
"""


def _base_url(ctx: Context) -> str:
    """The deployment's own origin, for minting a CDP URL a client can open."""
    try:
        request = ctx.request_context.request
        return str(request.base_url) if request else ""
    except (ValueError, AttributeError):
        return ""


def build(app) -> FastMCP:
    """Wire the tools to the services on `app.state`.

    Read at call time rather than captured, so the MCP app can be constructed
    before the lifespan has populated state.
    """
    mcp = FastMCP(
        "cloak-biz-scraper",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        # A single JSON response per POST. The SSE framing exists to interleave
        # progress with a result; nothing here streams, so it would be envelope
        # around a payload that arrives all at once anyway.
        json_response=True,
        # MUST be passed explicitly, and this is not a preference — it is a
        # production outage otherwise.
        #
        # FastMCP's `host` defaults to "127.0.0.1", and when it is a loopback
        # address the constructor silently turns on DNS-rebinding protection
        # with allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]. We never
        # pass `host` — uvicorn binds the socket, not FastMCP — so that default
        # applies, and every request whose Host header is the Railway domain
        # would be refused with 421 Misdirected Request. It passes locally
        # (Host: 127.0.0.1:8000 matches the allowlist) and fails for every real
        # user, which is the worst shape a bug can have.
        #
        # It could not be configured correctly even in principle: the allowlist
        # wants hostnames, and Railway assigns the deployment's domain without
        # telling the app. So the check is turned off *here* and done properly in
        # routes/mcp.py, against the request's own Host rather than a list we
        # would have to guess. Content-Type is still validated by the SDK either
        # way — that part is not conditional on this setting.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def scrape_listings(
        url: str, max_pages: int = 1, sync: bool = False, db_id: str | None = None
    ) -> ScrapeResult:
        """Start sweeping a search-results page for business listings.

        Returns immediately with status="working" and a job_id — the listings are
        NOT in this response. Call get_scrape_listing_results with the job_id to
        collect them.

        url: a search-results page (BizBuySell only for now). The URL decides how
            it is read, so paste the one from your address bar with filters applied.
        max_pages: how many pages of results to walk.
        sync: false (default) just reads the listings back. true also saves new
            ones to your Notion database, skipping those already there.
        db_id: override the configured Notion database. Only used when sync=true.
        """
        job = app.state.scrape.start(url, max_pages=max_pages, sync=sync, db_id=db_id)
        return ScrapeResult.of(job)

    @mcp.tool()
    async def get_scrape_listing_results(job_id: str) -> ScrapeResult:
        """Collect the results of a sweep started by scrape_listings.

        Never blocks. If status is "working" the sweep is still running: wait a
        few seconds and call again. "completed" means listings holds everything
        found; "failed" means error says why.
        """
        result = app.state.scrape.result(job_id)
        if result is None:
            raise ValueError(
                f"No sweep with job_id={job_id!r}. Check the id from scrape_listings — "
                f"results are kept for two weeks, so an older one may have been cleaned up."
            )
        return result

    @mcp.tool()
    async def archive_page(url: str, notion_page_id: str) -> ArchiveResult:
        """Read a page and append its content to an existing Notion page.

        Blocking: takes roughly a minute. Works on any URL, including a single
        listing's own page. Appends to the page you name and touches nothing
        else — it never creates a page or edits a property.
        """
        return await app.state.archive.archive(url, notion_page_id)

    @mcp.tool()
    async def create_instance(
        ctx: Context, profile: str = "agent", country: str | None = None,
        region: str | None = None, geoip: bool = True,
    ) -> InstanceView:
        """Launch a browser through the residential proxy.

        profile: a persistent identity — same name, same cookies and exit IP.
        country/region: where the proxy should exit.
        geoip: match the browser's timezone and locale to the exit IP. Leave true
            unless geo resolution is failing: with it off the browser keeps the
            container's UTC, which contradicts a residential exit and is itself
            something listing sites look for.
        """
        inst = await app.state.instances.launch(
            InstanceCreate(profile=profile, country=country, region=region, geoip=geoip),
            origin="interactive",
        )
        return instance_view(inst, secret=app.state.secret.current(), base_url=_base_url(ctx))

    @mcp.tool()
    async def list_instances(ctx: Context) -> list[InstanceView]:
        """Every running browser. Each carries a fresh, short-lived cdp_url."""
        secret = app.state.secret.current()
        base = _base_url(ctx)
        return [
            instance_view(i, secret=secret, base_url=base)
            for i in app.state.instances.running.values()
        ]

    @mcp.tool()
    async def get_instance(ctx: Context, instance_id: str) -> InstanceView:
        """One running browser, with a fresh, short-lived cdp_url."""
        inst = app.state.instances.get(instance_id)
        if inst is None:
            raise ValueError(
                f"No running browser with instance_id={instance_id!r}. It may have been "
                f"closed, or reaped after going idle."
            )
        return instance_view(inst, secret=app.state.secret.current(), base_url=_base_url(ctx))

    @mcp.tool()
    async def close_instance(instance_id: str) -> dict:
        """Close a browser and free its slot in the pool."""
        return {"ok": await app.state.instances.stop(instance_id), "instance_id": instance_id}

    return mcp
