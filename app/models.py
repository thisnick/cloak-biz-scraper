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


class SyncResult(BaseModel):
    """What a sweep wrote to the store. Null on the result when sync=false —
    which is the difference between "wrote nothing" and "was never asked to"."""

    new: int = 0
    existing: int = 0
    db_id: str = ""
    skipped: list[str] = Field(
        default_factory=list,
        description="Columns the database could not hold, so their values were not written.",
    )


class Job(BaseModel):
    """A sweep, as persisted to the volume. See services/jobs.py."""

    id: str
    status: str = "working"  # working | completed | failed
    source: str = ""
    url: str = ""
    max_pages: int = 1
    sync: bool = False
    db_id: str = ""
    summary: str = ""
    pages_crawled: int = 0
    error: str | None = None
    synced: SyncResult | None = None
    listings: list[Listing] = Field(default_factory=list)
    # Which process run started this. A "working" job from an older boot is one
    # nobody is working on — see JobStore.adopt.
    boot_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


class ScrapeResult(BaseModel):
    """The one shape both `scrape_listings` and `get_scrape_listing_results`
    return.

    Same schema from both, so an agent never has to learn two. Starting a sweep
    and collecting it are the same question asked at different times, and the
    only honest difference between the answers is `status` and how full
    `listings` is.
    """

    job_id: str
    status: str = "working"
    source: str = ""
    summary: str = ""
    pages_crawled: int = 0
    error: str | None = None
    synced: SyncResult | None = None
    listings: list[Listing] = Field(default_factory=list)

    @classmethod
    def of(cls, job: Job) -> "ScrapeResult":
        return cls(
            job_id=job.id,
            status=job.status,
            source=job.source,
            summary=job.summary,
            pages_crawled=job.pages_crawled,
            error=job.error,
            synced=job.synced,
            listings=job.listings,
        )


class ArchiveResult(BaseModel):
    """What `archive_page` did — a blocking call, so this is the whole story."""

    ok: bool = False
    url: str = ""
    title: str = ""
    notion_page_id: str = ""
    blocks_appended: int = 0
    markdown_chars: int = 0
    used_path: str = ""
    attempts_used: int = 0
    evidence_dir: str = ""
    error: str | None = None
    summary: str = ""


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


class InstanceView(BaseModel):
    """A running browser, as an agent sees it.

    `timezone` and `locale` are `None` when they were not measured, and that is
    load-bearing. Step 1 defaulted an unmeasured timezone to America/Los_Angeles,
    which reported a value nobody had observed as though it were resolved — for
    an instance whose proxy could not even route. Step 2 deleted that fallback.
    Nothing here may reintroduce one: a browser whose reported timezone
    contradicts its exit IP is the exact tell listing sites look for, so an
    honest `null` is worth more than a plausible string.

    `cdp_url` carries a freshly minted, short-lived token and is therefore
    different on every call. It is never stored and never reused.
    """

    instance_id: str
    profile: str
    origin: str
    proxy_ip: str | None = None
    timezone: str | None = None
    locale: str | None = None
    cdp_url: str | None = None
    vnc_url: str | None = None
    expires_at: float | None = None
    age_sec: float = 0.0
    idle_sec: float = 0.0
    geoip: bool = True
    humanize: bool = True


class Health(BaseModel):
    ok: bool = True
    service: str = "cloak-biz-scraper"
    version: str
    configured: bool = Field(
        description="Whether a license and proxy are set — false means the UI setup is unfinished."
    )
    instances: int = 0
