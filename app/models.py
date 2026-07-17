"""Request/response models shared by the service layer and its façades."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Listing(BaseModel):
    """One business listing, normalized.

    Every source adapter emits this same shape, so the stores never learn which
    site a row came from and the scrape adapters never learn where it lands.

    **The money fields are verbatim strings — exactly what the card said**:
    "$1,258,000", "Not Disclosed", "$81,000 + Inventory". A scraper reports what
    it saw and never discards information, because the moment it parses it has
    thrown away the difference between "$81,000" and "$81,000 + Inventory" for
    everyone downstream, including the agent reading this over MCP.

    **Parsing to a number is the STORE's job**, not this model's, because
    "number" is a property of the Notion column rather than of the listing:
    NotionStore parses when writing to a Number column and leaves the cell empty
    when it cannot be sure. See stores/money.py for why an empty cell beats a
    confident wrong one, and stores/notion.py for where it happens.
    """

    listing_id: str = ""
    url: str = ""
    normalized_url: str = ""
    title: str = ""
    location: str = ""
    asking_price: str = ""
    revenue: str = ""
    cashflow: str = ""
    ebitda: str = ""
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
