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

import base64
import functools
import logging
from pathlib import Path
from urllib.parse import quote

from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from . import __version__
from .models import (
    ArchiveResult,
    InstanceCreate,
    InstanceView,
    ProfileDeleteResult,
    ProfileView,
    ScrapeResult,
    ServerInfo,
    UploadTicket,
)
from .routes.guard import subject_of
from .services.agent_browser import InstanceNotDrivable
from .services.geo import GeoUnresolved, ProxyUnreachable
from .services.instances import BrowserUnavailable, CapExceeded
from .services.license import LicenseNotPro
from .services.proxy import ProxyNotConfigured
from .services.scrape import NotionNotConfigured
from .services.tokens import OWNER
from .services.urls import public_base
from .services.views import (
    download_message,
    downloaded_file,
    instance_view,
    require_usable_base_url,
    upload_ticket,
)

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


# The safety hints in `tools/list`, which a client reads to decide what it may
# run without asking first.
#
# Worth setting mostly because of what the spec says when they are ABSENT:
# `destructiveHint` and `openWorldHint` both default to *true*, so an
# unannotated server describes every tool it has — including the five that only
# read status — as destructive and open-world. Annotating is therefore less
# about flagging the dangerous tools than about clearing the safe ones.
#
# `openWorldHint` is true only where the CALLER can steer the tool at an
# arbitrary external entity, which here means the three that take or follow a
# URL. It is not "does a packet leave the box": create_instance probes its exit
# IP and may resolve a licence key, but only ever against a fixed set of
# endpoints that nothing in its signature can redirect. Read the other way the
# hint would be true almost everywhere and would carry no signal.
#
# `destructiveHint` is read here as "can this lose something the caller would
# want back", which is deliberately narrower than the spec's literal "performs
# only additive updates". Taken literally it would also cover update_profile,
# which overwrites a name or a geography, and new_proxy_session, which throws
# the old session away — neither of which loses anything: the overwritten values
# can be read first and set back, and the discarded session is internal, never
# returned, and the entire point of the call. Flagging them would put six of the
# ten writing tools behind a prompt, and a client that asks about most of the
# server teaches its user to click through the ones that can actually take
# something from them.
#
# `destructiveHint` and `idempotentHint` are meaningful only when readOnlyHint
# is false, so READ_ONLY leaves them unset rather than asserting something the
# spec ignores.
#
# They are hints, and a client is told not to trust them from a server it does
# not trust. None of this replaces the OAuth guard, the profile lifecycle locks,
# or the agent_browser verb allowlist — those enforce; this only describes.
# The largest downloaded image returned inline as well as by link. A photo a
# model wants to look at fits; a file that would bloat every later turn does not.
_INLINE_IMAGE_MAX = 5 * 1024 * 1024

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADDITIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False,
)
ADDITIVE_OPEN_WORLD = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True,
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False,
)
DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False,
)
DESTRUCTIVE_OPEN_WORLD = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
)


# The failures a caller is MEANT to read: every deliberate refusal. ValueError
# is the house convention for one (AgentBrowserError, ProfileError and NotASweep
# all subclass it); the rest are the launch and sync refusals the REST twin turns
# into a 4xx with the same text.
#
# The SDK shows a tool's own message only for ToolError. Anything else is a
# crash, reported as a bare "Error executing tool X" with its text kept on the
# server — which is right for a crash (an OSError's text carries paths) and
# wrong for a refusal, whose sentence IS the answer. So refusals are re-raised
# as ToolError, and what reaches the client reads exactly as it did on 1.x.
REFUSALS: tuple[type[Exception], ...] = (
    ValueError,
    InstanceNotDrivable,
    CapExceeded,
    BrowserUnavailable,
    GeoUnresolved,
    ProxyUnreachable,
    ProxyNotConfigured,
    LicenseNotPro,
    NotionNotConfigured,
)


def _refusals_reach_the_caller(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except REFUSALS as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


# The in-chat live view (MCP Apps). The page is one self-contained document:
# a host's default sandbox allows inline script and style and data: images, and
# no network at all, so everything it needs is inlined and every frame comes
# back through the `live_view` tool. See services/live_view.py.
LIVE_VIEW_URI = "ui://cloak-biz-scraper/live-view.html"
_UI_DIR = Path(__file__).parent / "ui"
_EXT_APPS = _UI_DIR / "vendor" / "ext-apps-app-2.0.0.min.js"


@functools.lru_cache(maxsize=1)
def live_view_html() -> str:
    """The panel's HTML with the vendored MCP Apps bridge inlined."""
    page = (_UI_DIR / "live_view.html").read_text(encoding="utf-8")
    bridge = _EXT_APPS.read_text(encoding="utf-8").replace("</script", "<\\/script")
    return page.replace("<!--EXT_APPS-->", f"<script>\n{bridge}\n</script>", 1)


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


def build(app) -> MCPServer:
    """Wire the tools to the services on `app.state`.

    Read at call time rather than captured, so the MCP app can be constructed
    before the lifespan has populated state.
    """
    # MCP Apps. Its tools and the page are fixed when the server is
    # constructed, so everything bound to the live view is registered here,
    # first; the plain tools follow on `mcp` below.
    apps = Apps()
    apps.add_html_resource(
        LIVE_VIEW_URI, live_view_html(),
        name="live-view", title="Live browser",
        description="A view-only live picture of a running browser, with its activity and files.",
        # The page draws its own card.
        prefers_border=False,
    )

    def ui_tool(**options):
        """`apps.tool` bound to the live view, with refusals passed through."""
        def register(fn):
            return apps.tool(resource_uri=LIVE_VIEW_URI, **options)(
                _refusals_reach_the_caller(fn))
        return register

    # Closed-world despite launching a browser: the open-world capability is
    # exercised by agent_browser, which is annotated for it. This call reaches
    # only the geo probe's own echo services and the CloakBrowser artifact.
    @ui_tool(annotations=ADDITIVE)
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

        In chat apps that show interactive views (Claude, ChatGPT), a live view
        of this browser appears in the conversation, so the user can watch what
        you do; it is view-only. Tell them they can take control from its "Take
        control" button if a site needs them (a login, a code, a CAPTCHA).

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

    # get_instance, with the live view attached. It returns exactly what
    # get_instance does, so a client that cannot show the view still gets a
    # vnc_url to hand the user — the graceful degradation SEP-2133 asks for.
    @ui_tool(annotations=READ_ONLY)
    async def show_browser(ctx: Context, instance_id: str) -> InstanceView:
        """Show the user a live, view-only picture of a running browser in the chat.

        Use when the user wants to watch or check on a browser — e.g. "show me",
        "what is it doing". In chat apps that show interactive views (Claude,
        ChatGPT) a live view appears with its activity and any downloaded files;
        a view appears automatically after create_instance, so call this only to
        bring one back. Elsewhere, give the user the returned vnc_url instead: it
        opens the same view in their own browser.
        """
        inst = app.state.instances.get(instance_id)
        if inst is None:
            raise ValueError(
                f"No running browser with instance_id={instance_id!r}. It may have been "
                f"closed, or reaped after going idle."
            )
        return instance_view(inst, secret=app.state.secret.current(),
                             base_url=_base_url(ctx), subject=_subject(ctx))

    # App-only: the page polls this. Hidden from the model by any host that
    # honours `visibility`; a client that does not, and calls it anyway, gets
    # one frame and a sentence telling it this is not for it.
    @ui_tool(annotations=READ_ONLY, visibility=["app"])
    async def live_view(ctx: Context, instance_id: str, since: str = "") -> CallToolResult:
        """Polled by the in-chat live view panel; not useful to call directly.

        Returns the browser's status, address, title, recent activity and files,
        and its latest frame when it differs from `since` (the frame_id the
        panel already shows).
        """
        state, jpeg = await app.state.live_view.state(instance_id, _subject(ctx), since=since)
        base = _base_url(ctx)
        if "files" in state:
            state["files"] = _file_links(state["files"], base)
        if state.get("status") != "closed":
            state["control_url"] = (
                f"{base}/?view=browsers&instance={quote(instance_id, safe='')}" if base else None
            )
        content: list = [TextContent(
            type="text",
            text=(f"Live view of {instance_id}: {state.get('status')}. This tool feeds the "
                  "in-chat panel; to look at the page yourself, use agent_browser."),
        )]
        if jpeg is not None:
            content.append(ImageContent(type="image", mime_type="image/jpeg",
                                        data=base64.b64encode(jpeg).decode("ascii")))
        return CallToolResult(content=content, structured_content=state)

    def _file_links(kept_files: list, base: str) -> list[dict]:
        from .services.downloads import DownloadsError

        links = []
        for kept in kept_files:
            try:
                view = downloaded_file(kept, base_url=base)
            except DownloadsError:
                continue
            links.append({"name": view.name, "bytes": view.bytes,
                          "content_type": view.content_type,
                          "expires_at": view.expires_at, "url": view.url})
        return links

    mcp = MCPServer(
        "cloak-biz-scraper",
        instructions=INSTRUCTIONS,
        # Say who we actually are. Left unset, the server reports an empty
        # version (the 1.x SDK reported its OWN version, 1.28.1, as ours — a
        # wrong answer to "what am I talking to" that a client cannot detect).
        version=__version__,
        extensions=[apps],
    )

    def tool(**options):
        """`mcp.tool`, with this server's refusals passed through. See REFUSALS."""
        def register(fn):
            return mcp.tool(**options)(_refusals_reach_the_caller(fn))
        return register

    # Annotated for sync=true, because an annotation cannot vary by argument:
    # sync=false only reads, but the same tool writes Notion rows when asked to.
    @tool(annotations=ADDITIVE_OPEN_WORLD)
    async def scrape_listings(
        urls: list[str], max_pages: int = 1, sync: bool = False
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
            Notion page.) Sync always targets the Notion database configured under
            Settings — there is no per-call database override.
        """
        job = app.state.scrape.start(urls, max_pages=max_pages, sync=sync)
        return ScrapeResult.of(job)

    @tool(annotations=READ_ONLY)
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

    # Not idempotent: it appends, so a second call with the same arguments
    # leaves the page holding the content twice.
    @tool(annotations=ADDITIVE_OPEN_WORLD)
    async def archive_page(url: str, notion_page_id: str) -> ArchiveResult:
        """Read a page and append its content to an existing Notion page.

        Blocking: takes roughly a minute. Works on any URL, including a single
        listing's own page. Appends to the page you name and touches nothing
        else — it never creates a page or edits a property.
        """
        return await app.state.archive.archive(url, notion_page_id)

    @tool(annotations=READ_ONLY)
    async def list_profiles() -> list[ProfileView]:
        """List the durable browser identities available to create_instance.

        Safe status only: name, optional proxy geography, whether it is Default,
        whether a browser is queued/opening/open/closing on it, and whether a
        complete proxy is configured. Fingerprint seeds, sticky-session tokens,
        cookie storage, and filesystem paths are never returned, so this tool
        never exposes the profile's internal identity material.
        """
        return await app.state.profile_service.list_profiles()

    @tool(annotations=ADDITIVE)
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

    # Not idempotent, because of the rename: a geography-only change repeats
    # cleanly, but replaying a rename fails on a name that is no longer there.
    @tool(annotations=ADDITIVE)
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

    # Additive rather than destructive: cookies, logins, fingerprint, name and
    # geography all survive; only the internal proxy session is replaced. And
    # closed-world — rotate_session mints the new session here, without asking
    # the proxy provider for one.
    @tool(annotations=ADDITIVE)
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

    @tool(annotations=DESTRUCTIVE)
    async def delete_profile(name: str) -> ProfileDeleteResult:
        """Permanently delete a profile and its saved cookies/logins.

        Destructive and irreversible. Default cannot be deleted. Deletion is
        refused while any browser is queued, opening, open, or closing on the
        profile, so it cannot race a launch. A missing name fails explicitly.
        Close the profile's browser first, then call this once.
        """
        return await app.state.profile_service.delete_profile(name)

    # Read-only despite handing back fresh CDP and VNC URLs on every call:
    # tokens.issue signs claims with the app secret and records nothing, so the
    # minting changes no state here or anywhere else. Same for get_instance.
    @tool(annotations=READ_ONLY)
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

    @tool(annotations=READ_ONLY)
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

    # The only tool whose blast radius is unbounded, and the one a client should
    # prompt on if it prompts on nothing else: it clicks and submits on whatever
    # site the browser is pointed at, where a single action can be irreversible
    # and belong to someone other than the caller.
    @tool(annotations=DESTRUCTIVE_OPEN_WORLD)
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
            upload @e3 <path>     attach an uploaded file to a file input
            download @e5          click a download link or button, keep the file, get a link to it
            get url               also: get title, get text @e3
            back / forward / reload
            screenshot            see the page as an image (add --full for the whole scroll height)

        Calls return text by default — the snapshot refs or the read text you act
        on. Ask for `screenshot` only when you actually need to SEE the page; it
        returns an image and costs far more, so don't screenshot after every step.

        `upload` attaches a file the page asks for. The <path> is only ever one that
        create_upload_url's flow gave you — call it, POST the file to the URL it
        returns, and use the path from that response. A path you wrote yourself is
        refused, and so is one whose file has expired; both say what to do instead.
        It needs a real file input. Most sites hide theirs behind a styled button,
        and a snapshot shows you only the button — so if `upload @eN` says the node
        is not a file input, pass a CSS selector instead:
        `upload "input[type=file]" <path>`. That usually works even when the input
        is invisible. What genuinely cannot work is a button that opens the
        operating system's own file picker, with no input on the page at all.

        `download` clicks the element and waits for the file it starts — a link, an
        Export button, a "Download PDF" button. It answers with a link to the saved
        file that works as-is in curl or a browser, for two hours. If you can run
        commands, fetch it with the curl it gives you; if you cannot, give the link
        to the user. Images also come back inline. Plain `click` does not save
        downloads — use `download` for anything that should produce a file. Files
        over 100 MB are cancelled; an element that only opens a page (not a file)
        times out, so navigate to that page instead.

        One action per call. Quote arguments that contain spaces. Only the
        listed read/interact verbs are accepted; anything else is refused. Only
        snapshot and screenshot take flags; the other verbs take plain arguments.
        """
        outcome = await app.state.agent_browser.drive(
            instance_id, command, subject=_subject(ctx)
        )
        if outcome.download is not None:
            return _download_blocks(outcome.download, _base_url(ctx))
        blocks: list = [outcome.output]
        if outcome.screenshot:
            blocks.append(Image(data=outcome.screenshot, format="png"))
        return blocks

    def _download_blocks(kept, base: str) -> list:
        """The link and the curl as text; an image also inline when it is small.

        Inline only for images, and only small ones: a model can look at a photo
        it downloaded without a shell, which is most of what "download this
        image" means in a chat client — but a 90 MB file must not ride back
        through the MCP response. Everything else is the link.
        """
        from .services.downloads import IMAGE_TYPES, DownloadsError

        try:
            view = downloaded_file(kept, base_url=base)
        except DownloadsError as exc:
            return [str(exc)]
        blocks: list = [download_message(view)]
        if kept.content_type in IMAGE_TYPES and kept.bytes <= _INLINE_IMAGE_MAX:
            try:
                data = (app.state.downloads.root / kept.handle / kept.name).read_bytes()
            except OSError:  # swept or cleared between the two calls
                return blocks
            blocks.append(Image(data=data, format=kept.content_type.split("/", 1)[1]))
        return blocks

    # Closed-world: the URL it mints points back at this server.
    @tool(annotations=ADDITIVE)
    async def create_upload_url(ctx: Context) -> UploadTicket:
        """Get a temporary URL for putting a file on this server, so a browser can upload it.

        Use this when a page needs a file — a photo for a listing, a document for a
        form — and the file is on YOUR machine, not this server's. MCP cannot carry
        the bytes, so they go over plain HTTP instead: this mints a short-lived URL,
        you POST the file to it, and it answers with a path on this server. Hand that
        path to agent_browser to attach it to a file input.

            1. create_upload_url()                     -> upload_url, token, curl
            2. run the curl, once per file             -> {"path": "/data/uploads/..."}
            3. agent_browser(id, "upload @e3 <path>")  -> attached to the page

        You need a shell or an HTTP client for step 2. The `curl` field is the whole
        command with the URL and token already filled in — run it as-is and change
        only the filename. If you cannot run commands, or it fails, say so and stop:
        there is no way to move the bytes from inside the conversation itself, and
        pasting the file as text will not work. You can post several files in one
        command by repeating `-F file=@...`; the answer then also carries a `files`
        list with one entry each.

        Images and PDFs only, checked by content rather than by filename. Uploaded
        files are temporary — the clock starts when you call this, not when you
        upload, and everything staged under one URL is deleted a couple of hours
        later. Get a fresh URL for each new task instead of reusing an old path. An
        expired path simply fails at step 3 and tells you to upload again.
        """
        from .services.uploads import UploadsError

        try:
            # Before the mint: a refusal must not leave a ticket behind. See
            # views.require_usable_base_url.
            # One binding, passed to both — see the REST twin.
            base = _base_url(ctx)
            require_usable_base_url(base)
            ticket = await app.state.uploads.mint(
                subject=_subject(ctx), secret=app.state.secret.current()
            )
            return upload_ticket(ticket, base_url=base)
        except UploadsError as exc:
            # Every refusal arrives as one ValueError, so THE MESSAGE IS THE ONLY
            # THING that distinguishes "the volume is full" from "this server
            # cannot work out its own address" — the REST twin gets a distinct
            # status code for each and this door does not. Passing `exc` through
            # verbatim is therefore load-bearing rather than lazy, and a test
            # pins that the two read differently.
            raise ValueError(str(exc)) from exc

    # Closed-world: proxy status comes from saved settings, not a live probe.
    @tool(annotations=READ_ONLY)
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

    # Destructive because live page state dies with the browser: a half-filled
    # form is gone, where cookies and logins survive in the profile. Idempotent
    # even so — instances.stop returns False for an id it no longer holds rather
    # than raising, so a retry after a dropped response is safe.
    @tool(annotations=DESTRUCTIVE_IDEMPOTENT)
    async def close_instance(instance_id: str) -> dict:
        """Close a browser and free its slot in the pool."""
        return {"ok": await app.state.instances.stop(instance_id), "instance_id": instance_id}

    return mcp


def session_manager(mcp: MCPServer):
    """Construct the stateless session manager `/mcp` hands requests to.

    The Starlette app streamable_http_app() returns is deliberately discarded
    (see main.py); what matters is the manager it builds, from these settings.
    """
    mcp.streamable_http_app(
        stateless_http=True,
        # A single JSON response per POST. The SSE framing exists to interleave
        # progress with a result; nothing here streams, so it would be envelope
        # around a payload that arrives all at once anyway.
        json_response=True,
        # MUST be passed explicitly, and this is not a preference — it is a
        # production outage otherwise.
        #
        # streamable_http_app's `host` defaults to "127.0.0.1", and when it is a
        # loopback address the SDK silently turns on DNS-rebinding protection
        # with allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]. We never
        # pass `host` — uvicorn binds the socket, not the SDK — so that default
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
    return mcp.session_manager
