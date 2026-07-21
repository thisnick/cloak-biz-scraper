"""Railway's healthcheck. Unauthenticated by necessity — the platform's prober
carries no credentials — so it must never disclose anything sensitive.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..models import Health

router = APIRouter()


@router.get("/healthz", response_model=Health)
async def healthz(request: Request) -> Health:
    settings = request.app.state.settings.load()
    instances = request.app.state.instances
    return Health(
        version=__version__,
        # Public CloakBrowser and direct egress are both deliberate modes. The
        # one shape that is not launchable is a half-entered proxy: it must fail
        # visibly rather than being mistaken for permission to go direct.
        configured=settings.proxy_status() != "incomplete",
        instances=len(instances.running),
    )
