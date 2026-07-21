"""FastAPI application wiring.

Routes are façades and nothing else: they resolve a service off app.state, call
it, and shape the response. All behaviour lives in services/ so that the REST
API, the MCP tools, and the web UI added in later steps are three doors onto one
implementation rather than three implementations that drift.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from .services.log_safety import install_log_sanitizer

# Uvicorn configures its access logger before importing the ASGI app. A record
# factory still sees those records, plus dependency logs emitted during startup.
install_log_sanitizer()

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from . import __version__, mcp_server
from .config import CONFIG, bootstrap_binary_cache, purge_binary_env
from .routes import api, cdp, health, oauth, ui, vnc
from .routes.guard import AuthGuard
from .routes.mcp import MCPEndpoint
from .response_security import ResponseSecurity
from .services import heartbeat
from .services.agent_browser import AgentBrowserService
from .services.archive import ArchiveService
from .services.instances import InstanceManager
from .services.jobs import JobStore
from .services.oauth import OAuthProvider, OAuthStore
from .services.profiles import ProfileService
from .services.ratelimit import RateLimiter
from .services.scrape import ScrapeService
from .services.secret import SecretService
from .services.settings import SettingsService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("cloakbiz.main")

_REAP_INTERVAL_SEC = 60


async def _reap_loop(instances: InstanceManager) -> None:
    while True:
        await asyncio.sleep(_REAP_INTERVAL_SEC)
        try:
            await instances.reap()
        except Exception:
            logger.exception("reap failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = bootstrap_binary_cache()
    logger.info("cloakbrowser binary cache -> %s", cache)

    settings_service = SettingsService(CONFIG.settings_path, CONFIG.dek_path)
    settings = settings_service.load()  # first boot seeds from env; volume wins after
    purge_binary_env()  # only after seeding, or the seed would find nothing

    # APP_SECRET is read straight from the environment every boot — the Railway
    # variable is the one source of truth. Never fatal when absent: the login
    # page explains itself, whereas a crash loop explains nothing.
    secret_service = SecretService()
    secret = secret_service.bootstrap()

    # Jobs live on the volume so a finished sweep survives the container
    # sleeping. Anything still "working" belongs to a process that no longer
    # exists, so it is failed here — before the first poll can be told to keep
    # waiting for it. Both must happen before any request is served.
    jobs = JobStore(CONFIG.jobs_dir)
    interrupted = jobs.adopt()
    jobs.prune()

    app.state.settings = settings_service
    app.state.secret = secret_service
    # The OAuth store holds registered clients and live authorization codes; the
    # provider signs tokens with whatever the secret service currently says, so
    # rotating APP_SECRET invalidates every outstanding token with no
    # revocation list to maintain.
    app.state.oauth = OAuthProvider(
        OAuthStore(CONFIG.oauth_path, CONFIG.dek_path), secret_service
    )
    # Shared by both doors that take APP_SECRET (the UI login and OAuth's), so a
    # flood cannot use one to reset the other's budget.
    app.state.login_limiter = RateLimiter()
    # Registration is a separate budget: it must stay open for DCR (ChatGPT and
    # Claude register themselves), but each one rewrites the encrypted client
    # store, so an unthrottled flood is O(n) disk work per request on a file the
    # flood is growing. Looser than the login — a real user registers a handful
    # of clients ever, and none of them are guesses.
    app.state.register_limiter = RateLimiter(max_failures=10, window_sec=60, global_max=20)
    app.state.jobs = jobs
    app.state.instances = InstanceManager(settings_service)
    # Read settings through app.state so tests and any future live service swap
    # cannot leave profile status/creation bound to a stale SettingsService.
    app.state.profile_service = ProfileService(
        app.state.instances, lambda: app.state.settings,
    )
    # Guarantee the DEFAULT profile (migrating a legacy "agent" once). Non-fatal:
    # a bad profiles file must never take down boot — log and carry on, the UI can
    # create one later.
    try:
        _s = settings_service.load()
        app.state.instances.profiles.ensure_default(
            default_country=_s.proxy_country, default_region=_s.proxy_region)
    except Exception:  # noqa: BLE001
        logger.exception("could not ensure the Default profile at startup")
    app.state.scrape = ScrapeService(app.state.instances, jobs, settings_service)
    app.state.archive = ArchiveService(app.state.instances, settings_service)
    app.state.agent_browser = AgentBrowserService(app.state.instances)
    logger.info(
        "ready: secret=%s license=%s proxy=%s notion=%s pool max=%d reserve=%d "
        "jobs=%d interrupted=%d oauth_clients=%d",
        "set" if secret else "MISSING",
        "pro-key-saved" if settings.cloakbrowser_license_key else "public",
        settings.proxy_status(),
        "set" if settings.notion_configured() else "MISSING",
        settings.max_instances,
        settings.interactive_reserve,
        len(jobs.all()),
        interrupted,
        app.state.oauth.client_count(),
    )

    # Built here, not at import: the SDK's session manager is single-use, so one
    # per lifespan is what lets this app be started more than once in a process.
    # streamable_http_app() is what constructs it from the FastMCP settings
    # (stateless, JSON responses); the Starlette app it returns is deliberately
    # discarded — its GET handler opens an SSE stream we refuse, and its routing
    # cannot see the Origin check. MCPEndpoint drives the same manager instead.
    mcp = mcp_server.build(app)
    mcp.streamable_http_app()
    app.state.mcp_manager = mcp.session_manager

    reaper = asyncio.create_task(_reap_loop(app.state.instances))
    pulse = asyncio.create_task(heartbeat.loop(lambda: app.state.scrape.in_flight))
    # The manager owns the task group every MCP request runs inside.
    async with app.state.mcp_manager.run():
        try:
            yield
        finally:
            for task in (reaper, pulse):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.instances.cleanup_all()


app = FastAPI(title="cloak-biz-scraper", version=__version__, lifespan=lifespan)


@app.exception_handler(ui.NotAuthenticated)
async def _login_redirect(request: Request, exc: ui.NotAuthenticated) -> RedirectResponse:
    """Send a signed-out browser to the login page rather than a JSON 401.

    303 so the browser re-issues as GET: a POST that lost its session (an expired
    cookie, a rotated secret) must not replay itself against /login.
    """
    return RedirectResponse("/login", status_code=303)


app.include_router(health.router)
app.include_router(oauth.router)
app.include_router(api.router)
app.include_router(cdp.router)
app.include_router(vnc.router)
app.include_router(ui.router)

# The noVNC viewer, if the image has it. Mounted rather than proxied because it
# is static assets: the page is inert until it dials the socket, and the socket
# is what carries the token and the check. Served from our own origin so the
# viewer and the websocket share one, which is what the Origin rule expects.
_NOVNC = Path("/usr/share/novnc")
if _NOVNC.is_dir():
    app.mount("/novnc", StaticFiles(directory=str(_NOVNC)), name="novnc")
else:
    logger.warning("noVNC is not installed; live view URLs will not be offered")

# Above the router, so it sees /mcp and /api/* before any route does. This is
# the gate: without it both surfaces answer 200 to anyone with the URL.
app.add_middleware(AuthGuard, get_provider=lambda: getattr(app.state, "oauth", None))
# Added after AuthGuard so Starlette places it outside the bearer gate.  That is
# what gives guard-generated 401/503 responses the same no-store/security policy
# as FastAPI, SDK OAuth, and raw MCP responses.
app.add_middleware(ResponseSecurity)

# A Route, deliberately, not a Mount. `Mount("/mcp")` compiles to the regex
# ^/mcp/(?P<path>.*)$ — it never matches a bare "/mcp", so Starlette's
# redirect_slashes answers the actual endpoint with a 307 to "/mcp/". The spec
# says one endpoint and clients POST to exactly "/mcp"; worse, the redirect is
# invisible to any client that follows redirects (including httpx and every test
# client), so it would have looked fine here and cost a real client its POST body.
#
# `methods=None` means every method reaches the endpoint, which is what lets it
# answer GET with its own 405 and an explanation rather than Starlette's bare one.
# Starlette treats a non-function endpoint as a raw ASGI app, which MCPEndpoint is.
app.router.routes.append(
    Route("/mcp", MCPEndpoint(lambda: getattr(app.state, "mcp_manager", None)), methods=None)
)
