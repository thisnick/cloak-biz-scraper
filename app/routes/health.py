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
        configured=bool(settings.cloakbrowser_license_key) and settings.proxy_configured(),
        instances=len(instances.running),
    )
