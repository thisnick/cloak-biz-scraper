"""Request/response models shared by the service layer and its façades."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Listing(BaseModel):
    """One business listing, normalized.

    Every source adapter emits this same shape, so the stores never learn which
    site a row came from and the scrape adapters never learn where it lands.

    The money fields are numbers or nothing: a value we could not parse exactly
    is left None rather than guessed at, because the whole reason they are
    numeric is to make "$1–7M with SDE over $500k" a filter you can trust. The
    verbatim text survives in `excerpt`. See stores/money.py.
    """

    listing_id: str = ""
    url: str = ""
    normalized_url: str = ""
    title: str = ""
    location: str = ""
    asking_price: float | None = None
    revenue: float | None = None
    cashflow: float | None = None
    ebitda: float | None = None
    excerpt: str = ""
    source: str = ""


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
