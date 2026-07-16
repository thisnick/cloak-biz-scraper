"""Request/response models shared by the service layer and its façades."""
from __future__ import annotations

from pydantic import BaseModel, Field


class InstanceCreate(BaseModel):
    profile: str = Field(description="Persistent profile name (created if new).")
    country: str | None = None
    region: str | None = None
    owner: str | None = None  # optional label for interactive callers (agent id, etc.)
    headed: bool = True
    geoip: bool = True
    humanize: bool = True
    human_preset: str = "careful"
    ttl_min: int | None = None
    width: int = 1440
    height: int = 900


class InstanceResponse(BaseModel):
    id: str
    profile: str
    origin: str = "interactive"
    owner: str | None = None
    status: str = "running"
    proxy_ip: str | None = None
    timezone: str | None = None
    locale: str | None = None
    headed: bool = True
    geoip: bool = True
    humanize: bool = True
    fingerprint_seed: int | None = None
    created_at: float | None = None
    last_used_at: float | None = None
    expires_at: float | None = None
    idle_sec: float | None = None


class Health(BaseModel):
    ok: bool = True
    service: str = "cloak-biz-scraper"
    version: str
    configured: bool = Field(
        description="Whether a license and proxy are set — false means the UI setup is unfinished."
    )
    instances: int = 0
