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

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from . import __version__
from .config import CONFIG, bootstrap_binary_cache, purge_binary_env
from .routes import health, ui
from .services.instances import InstanceManager
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

    # The volume's secret is authoritative; APP_SECRET seeds it once and
    # APP_SECRET_RESET recovers a forgotten one. Never fatal when absent: the
    # login page explains itself, whereas a crash loop explains nothing.
    secret_service = SecretService(CONFIG.secret_path, CONFIG.dek_path)
    secret = secret_service.bootstrap()

    app.state.settings = settings_service
    app.state.secret = secret_service
    app.state.instances = InstanceManager(settings_service)
    logger.info(
        "ready: secret=%s license=%s proxy=%s notion=%s pool max=%d reserve=%d",
        "set" if secret else "MISSING",
        "set" if settings.cloakbrowser_license_key else "MISSING",
        "set" if settings.proxy_configured() else "MISSING",
        "set" if settings.notion_configured() else "MISSING",
        settings.max_instances,
        settings.interactive_reserve,
    )

    reaper = asyncio.create_task(_reap_loop(app.state.instances))
    try:
        yield
    finally:
        reaper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
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
app.include_router(ui.router)
