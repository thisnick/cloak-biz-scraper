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

from . import __version__
from .models import (
    ArchiveResult,
    InstanceCreate,
    InstanceView,
    ProfileDeleteResult,
    ProfileView,
    ScrapeResult,
    ServerInfo,
)
from .routes.guard import subject_of
from .services.tokens import OWNER
from .services.urls import public_base
from .services.views import instance_view

logger = logging.getLogger("cloakbiz.mcp")

INSTRUCTIONS = """\
Finds business-for-sale listings and can file them into Notion.

Sweeps are asynchronous: scrape_listings starts one and returns immediately with
a job_id, then get_scrape_listing_results collects it. A sweep takes a few
minutes, so the first collect will often still say "working" — wait and call
again rather than starting a second sweep.

Money fields are reported exactly as the listing said them ("$1,258,000",
"Not Disclosed", "$81,000 + Inventory"). They are strings, not numbers, on
purpose: the card is quoted rather than interpreted.
"""


def _request(ctx: Context):
    try:
        return ctx.request_context.request
    except (ValueError, AttributeError):
        return None


def _base_url(ctx: Context) -> str:
    """The deployment's own origin, for minting URLs a client can open.

    public_base() rather than request.base_url: behind Railway's TLS termination
    the request's own scheme is http, and the ws:// URL that produces is blocked
    as mixed content by any browser on the https page. See services/urls.py.
    """
    request = _request(ctx)
    return public_base(request) if request else ""


def _subject(ctx: Context) -> str:
    """The OAuth subject behind this tool call.

    Read from the scope the guard populated, so it reflects a token that
    actually verified rather than anything the client asserted.
    """
    request = _request(ctx)
    return (subject_of(request) if request else None) or OWNER


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

    # Say who we actually are. FastMCP takes no `version`, so the lowlevel Server
    # underneath falls back to `pkg_version("mcp")` and every client was told
    # this server was version 1.28.1 — the SDK's version, reported as ours. A
    # client has no way to know that is not us, so it is not cosmetic: it is a
    # wrong answer to "what am I talking to", and it would silently track the
    # SDK's releases forever. Set after construction because the constructor
    # exposes no seam; it is read when a session initializes, which is later.
    mcp._mcp_server.version = __version__

    @mcp.tool()
    async def scrape_listings(
        urls: list[str], max_pages: int = 1, sync: bool = False, db_id: str | None = None
    ) -> ScrapeResult:
        """Start sweeping one or more listings pages for business listings.

        Returns immediately with status="working" and a job_id — the listings are
        NOT in this response. Call get_scrape_listing_results with the job_id to
        collect them. All the URLs fan out into ONE job, so there is one job_id to
        collect and the results come back merged and de-duplicated.

        What the collected `listings` hold depends on `sync`. With sync=false you
        get EVERY listing found, and each `synced_row_id` is empty. With sync=true
        you get only the listings this sweep NEWLY added to Notion, each carrying
        the `synced_row_id` of the row it was written to — hand that straight to
        archive_page(notion_page_id=…). Listings already in the database are left
        out of `listings` but still counted in `synced.existing`.

        urls: a NON-EMPTY list of pages that each list many businesses, not single
            listings (BizBuySell only for now). Each entry is either a
            SEARCH-RESULTS (SERP) page, or a broker's profile page
            (bizbuysell.com/business-broker/…), whose for-sale listings are swept.
            Each URL decides how it is read, so for a search use one with the
            filters already applied. Pass several to sweep several searches or
            brokers at once (e.g. the same search across a few regions). If a URL
            isn't a supported listings page it is reported as that source's
            failure and the others still run; the call only errors outright if the
            list is empty or none of the URLs are readable. If you don't have such
            a URL, either ask the user for it, OR get one yourself: create_instance
            a browser, use agent_browser to run the search on the site (navigate,
            fill the search box, apply filters), read the resulting address bar
            (agent_browser get url), and pass that here.
        max_pages: how many pages of results to walk PER URL (shared across all of
            them). A broker profile pages its for-sale tab too, so raise this to
            sweep a broker with many listings.
        sync: false (default) just reads the listings back — no Notion involved,
            and the collected result holds ALL listings found with an empty
            synced_row_id on each. true also saves new ones to your Notion
            database, skipping those already there; the merged set from all URLs is
            de-duplicated and upserted once, and the collected result then holds
            ONLY the newly-added listings, each with the synced_row_id of its new
            Notion row (ready for archive_page). (The Notion layer is opt-in:
            sync=true here, plus archive_page to file a page's full content into a
            Notion page.)
        db_id: override the configured Notion database. Only used when sync=true.
        """
        job = app.state.scrape.start(urls, max_pages=max_pages, sync=sync, db_id=db_id)
        return ScrapeResult.of(job)

    @mcp.tool()
    async def get_scrape_listing_results(job_id: str) -> ScrapeResult:
        """Collect the results of a sweep started by scrape_listings.

        Never blocks. If status is "working" the sweep is still running: wait a
        few seconds and call again. "failed" means error says why. "completed"
        means `listings` is ready — but WHAT it holds depends on how the sweep was
        started: a sync=false sweep returns every listing found (each
        synced_row_id empty); a sync=true sweep returns only the ones it newly
        added to Notion, each carrying the synced_row_id of its new row (pass it to
        archive_page). Rows already in the database are omitted from `listings` but
        counted in `synced.existing`.
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
        ctx: Context, profile: str = "Default", country: str | None = None,
        region: str | None = None, geoip: bool = True,
    ) -> InstanceView:
        """Launch a cloaked, anti-detection browser (CloakBrowser).

        It carries a real, consistent browser fingerprint. With no CloakBrowser
        key configured it deliberately runs the public build, which has fewer
        bypasses and has not been tested by us against the listing sites. A
        saved key must resolve Pro or launch fails visibly; it is never silently
        downgraded to public. If an Evomi proxy is
        configured, it exits through that residential IP, which is recommended
        for listing sites that block datacenter addresses. Without a proxy it
        launches through this server's direct datacenter connection. A proxy
        configuration that is present but incomplete, rejected, or unreachable
        fails visibly and is never bypassed with a direct retry.

        profile: a DURABLE identity. Cookies, logins, and local storage are kept
            in the profile's own storage and survive across relaunches, so the
            same profile name stays logged in to sites. Default to the same
            profile ("Default") for continuity; use a NEW name only when you
            deliberately want a clean, logged-out identity. Each profile keeps a
            stable fingerprint and, when a proxy is configured, a sticky exit IP.
        country/region: where the optional proxy should exit; ignored in direct mode.
        geoip: with a proxy, match the browser's timezone and locale to the exit
            IP. Leave true unless proxy geo resolution is failing. Direct mode
            does not probe or geolocate the server, so these fields remain unknown.

        Lifecycle: the browser closes itself after 15 minutes idle or 60 minutes
        total, freeing its slot. The returned cdp_url is a Chrome DevTools Protocol
        websocket carrying a short-lived token (~10 min): drive it with
        agent_browser, or attach your own client — Playwright's
        connectOverCDP(cdp_url). The token is minted fresh on every get_instance /
        list_instances call, so if a connection drops, re-fetch the instance to get
        a working cdp_url rather than reusing an old one.
        """
        subject = _subject(ctx)
        inst = await app.state.instances.launch(
            InstanceCreate(profile=profile, country=country, region=region, geoip=geoip),
            origin="interactive", subject=subject,
        )
        return instance_view(inst, secret=app.state.secret.current(),
                             base_url=_base_url(ctx), subject=subject)

    @mcp.tool()
    async def list_profiles() -> list[ProfileView]:
        """List the durable browser identities available to create_instance.

        Safe status only: name, optional proxy geography, whether it is Default,
        whether a browser is queued/opening/open/closing on it, and whether a
        complete proxy is configured. Fingerprint seeds, sticky-session tokens,
        cookie storage, and filesystem paths are never returned, so this tool
        never exposes the profile's internal identity material.
        """
        return await app.state.profile_service.list_profiles()

    @mcp.tool()
    async def create_profile(
        name: str, country: str | None = None, region: str | None = None,
    ) -> ProfileView:
        """Create a durable, initially logged-out browser identity.

        name: unique profile name passed later to create_instance.
        country/region: optional proxy exit target. They are stored even in
            direct mode but only take effect when a residential proxy is
            configured. Omitted values use the server's proxy geography defaults.

        This does not launch a browser. It returns safe status and never exposes
        the new fingerprint seed, proxy session token, or cookie directory.
        """
        return await app.state.profile_service.create_profile(
            name, country=country, region=region,
        )

    @mcp.tool()
    async def update_profile(
        name: str,
        new_name: str | None = None,
        country: str | None = None,
        region: str | None = None,
    ) -> ProfileView:
        """Rename a profile and/or change its future proxy exit geography.

        Omitted fields stay unchanged. A rename keeps cookies, logins, and the
        stable fingerprint, but is refused while a browser is queued, opening,
        open, or closing on the profile. Geography changes apply to the next
        proxied launch and may be made while the current browser is open. Default
        cannot be renamed. Missing profiles and name collisions fail explicitly.
        """
        return await app.state.profile_service.update_profile(
            name, new_name=new_name, country=country, region=region,
        )

    @mcp.tool()
    async def new_proxy_session(name: str) -> ProfileView:
        """Give a profile a fresh sticky proxy session for its next launch.

        Use after a residential exit IP is blocked. Cookies, logins, fingerprint,
        name, and geography stay unchanged; only the internal proxy session is
        replaced, and its token is never returned. This is refused in direct mode
        or with incomplete proxy settings because no usable proxy session exists.
        An already-open browser keeps its current connection; the next launch uses
        the new session.
        """
        return await app.state.profile_service.new_proxy_session(name)

    @mcp.tool()
    async def delete_profile(name: str) -> ProfileDeleteResult:
        """Permanently delete a profile and its saved cookies/logins.

        Destructive and irreversible. Default cannot be deleted. Deletion is
        refused while any browser is queued, opening, open, or closing on the
        profile, so it cannot race a launch. A missing name fails explicitly.
        Close the profile's browser first, then call this once.
        """
        return await app.state.profile_service.delete_profile(name)

    @mcp.tool()
    async def list_instances(ctx: Context) -> list[InstanceView]:
        """Every running browser. Each carries a fresh, short-lived cdp_url and,
        where the browser has a live view, a vnc_url to watch it."""
        secret = app.state.secret.current()
        base = _base_url(ctx)
        subject = _subject(ctx)
        return [
            instance_view(i, secret=secret, base_url=base, subject=subject)
            for i in app.state.instances.running.values()
        ]

    @mcp.tool()
    async def get_instance(ctx: Context, instance_id: str) -> InstanceView:
        """One running browser, with a FRESH, short-lived cdp_url and vnc_url.

        Each call mints new tokens (~10 min), so call this to get a working
        cdp_url again after one expires or a connection drops — attach with
        agent_browser or Playwright's connectOverCDP(cdp_url)."""
        inst = app.state.instances.get(instance_id)
        if inst is None:
            raise ValueError(
                f"No running browser with instance_id={instance_id!r}. It may have been "
                f"closed, or reaped after going idle."
            )
        return instance_view(inst, secret=app.state.secret.current(),
                             base_url=_base_url(ctx), subject=_subject(ctx))

    @mcp.tool()
    async def agent_browser(ctx: Context, instance_id: str, command: str):
        """Drive a running browser one action at a time, and see the result.

        Use this to actually operate a browser you launched with create_instance:
        open pages, read them, click, and fill forms. The browser is the cloaked,
        anti-detection CloakBrowser and keeps the profile's fingerprint and
        cookies across the session. When an optional residential proxy is
        configured, it also keeps that profile's sticky exit IP; direct mode uses
        the server's datacenter connection and may be blocked by listing sites.

        The workflow is snapshot-then-act. A snapshot lists the page's elements
        with short refs like @e3; you act on those refs. Refs are reassigned on
        every snapshot, so snapshot again after anything that changes the page.

            navigate <url>        go to a page
            snapshot -i           list interactive elements (@e refs). add -u for link urls
            read                  read the page's text (no refs)
            click @e3             click an element by its ref
            fill @e3 "some text"  type into a field
            press Enter           press a key
            get url               also: get title, get text @e3
            back / forward / reload
            screenshot            see the page as an image (add --full for the whole scroll height)

        Calls return text by default — the snapshot refs or the read text you act
        on. Ask for `screenshot` only when you actually need to SEE the page; it
        returns an image and costs far more, so don't screenshot after every step.

        One action per call. Quote arguments that contain spaces. Only the
        listed read/interact verbs are accepted; anything else is refused. Only
        snapshot and screenshot take flags; the other verbs take plain arguments.
        """
        from mcp.server.fastmcp import Image

        outcome = await app.state.agent_browser.drive(
            instance_id, command, subject=_subject(ctx)
        )
        blocks: list = [outcome.output]
        if outcome.screenshot:
            blocks.append(Image(data=outcome.screenshot, format="png"))
        return blocks

    @mcp.tool()
    async def server_info() -> ServerInfo:
        """How this server is set up: proxy, browser, pool, and Notion status.

        Read-only, and carries no secrets — status and versions only. Useful to
        check before a sweep or a browser launch: whether the optional residential
        proxy is direct/configured/working, whether the selected CloakBrowser build
        is public, resolved Pro, or has an unverified Pro key, how many browser slots
        are free, and whether Notion is connected.
        """
        from .services.views import server_info as build_server_info

        return build_server_info(app.state.settings.load(), app.state.instances)

    @mcp.tool()
    async def close_instance(instance_id: str) -> dict:
        """Close a browser and free its slot in the pool."""
        return {"ok": await app.state.instances.stop(instance_id), "instance_id": instance_id}

    return mcp
