"""Request/response models shared by the service layer and its façades."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .config import CONFIG


class Listing(BaseModel):
    """One business listing.

    Money fields are quoted verbatim from the listing, exactly as it stated them
    — "$1,258,000", "Not Disclosed", "$81,000 + Inventory". They are strings, so
    read them as text rather than assuming a number.
    """

    # The docstring above is shipped to the model as part of the tool's output
    # schema, so it describes the data and nothing else. The reasoning, for
    # whoever edits this next:
    #
    # Every source adapter emits this shape, so stores never learn which site a
    # row came from and adapters never learn where it lands.
    #
    # The money fields are verbatim because a scraper that parses has already
    # destroyed the difference between "$81,000" and "$81,000 + Inventory" for
    # everyone downstream, including the agent. Parsing is the STORE's job:
    # "number" is a property of the Notion column, not of the listing, so
    # NotionStore parses on the way in and leaves the cell empty when it cannot
    # be sure. See stores/money.py for why an empty cell beats a confident wrong
    # one, and stores/notion.py for where it happens.

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
    """The result of a sweep.

    While status is "working" the sweep is still running and `listings` is
    empty — collect it with get_scrape_listing_results. `synced` is null when
    sync was false, which means nothing was saved rather than nothing was found.
    """

    # Both tools return this one shape so an agent never has to learn two:
    # starting a sweep and collecting it are the same question asked at
    # different times, and the only honest difference between the answers is
    # `status` and how full `listings` is.

    job_id: str
    status: str = "working"
    source: str = ""
    summary: str = ""
    pages_crawled: int = 0
    error: str | None = None
    synced: SyncResult | None = None
    listings: list[Listing] = Field(default_factory=list)
    # Where this sweep's screenshots and page snapshots were written. A sweep
    # that finds nothing is the failure users hit first, and "it didn't work and
    # you can't see why" is where they give up: the pictures of the blocked page
    # are the answer, and until now nothing told anyone they existed.
    # ArchiveResult has carried this since it was written; a sweep never did.
    evidence_dir: str = ""

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
            evidence_dir=str(CONFIG.evidence_dir / job.id),
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
    """A running browser.

    `timezone` and `locale` are null when they could not be measured — never
    guessed at. `cdp_url` carries a short-lived token and is only valid for a
    few minutes.
    """

    # Everything above this line is shipped to the model as the tool's output
    # schema, so it says what the data means and stops. The reasoning belongs
    # here, where it is for whoever edits this next:
    #
    # The nulls are load-bearing. Step 1 defaulted an unmeasured timezone to
    # America/Los_Angeles, reporting a value nobody had observed as though it
    # were resolved — on instances whose proxy could not even route. Step 2
    # deleted that fallback. This is the first step where an agent can see the
    # field, so it is the first step where a default would be believed, and a
    # browser whose timezone contradicts its exit IP is the exact tell listing
    # sites look for. An honest null beats a plausible string.
    #
    # cdp_url is minted per call and never stored (services/views.py).

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


class AgentBrowserResult(BaseModel):
    """The result of one `agent_browser` action against a running browser."""

    instance_id: str
    command: str = Field(description="The command that was run, echoed back.")
    ok: bool = Field(description="Whether the action succeeded (exit code 0).")
    output: str = Field(
        description="agent-browser's output — e.g. a snapshot's @eN element refs, "
        "or the extracted text/url/title. Read this to decide the next action."
    )
    screenshot_png_base64: str | None = Field(
        default=None,
        description="A PNG screenshot of the page after the action, base64-encoded. "
        "The MCP tool returns this as an inline image instead.",
    )


class Health(BaseModel):
    ok: bool = True
    service: str = "cloak-biz-scraper"
    version: str
    configured: bool = Field(
        description="Whether a license and proxy are set — false means the UI setup is unfinished."
    )
    instances: int = 0
