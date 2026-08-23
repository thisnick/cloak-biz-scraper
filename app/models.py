"""Request/response models shared by the service layer and its façades."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

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
    synced_row_id: str = Field(
        default="",
        description="The id of the row this listing was written to when synced to your "
        "store; in Notion this is the page id — pass it to archive_page(notion_page_id=…). "
        "Empty unless this sweep synced and inserted the row (so it is empty for sync=false "
        "and for listings already in the store).",
    )


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


class TaskBase(BaseModel):
    """What every stored task has, whatever kind of work it did.

    A task is one unit of browser work this server ran and wrote to the volume
    (see services/jobs.py). Only the fields here are common: an id, how it went,
    what it operated on, and where its captures landed. Everything a *particular*
    kind of task knows — a sweep's listings, an archive's Notion page — belongs
    on that kind, so no reader of one is tempted to reach for the other's fields.
    """

    id: str
    status: str = "working"  # working | completed | failed
    # What this task operates on. A sweep fans out over several URLs into one
    # record; an archive reads exactly one. The list is the common shape.
    urls: list[str] = Field(default_factory=list)
    summary: str = ""
    error: str | None = None
    evidence_dir: str = ""
    # Which process run started this. A "working" task from an older boot is one
    # nobody is working on — see JobStore.adopt.
    boot_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_url(cls, data):
        """Read a legacy single `url` into `urls`.

        Jobs written before the multi-URL change carry a scalar `url` on the
        volume, and some callers still pass `url=`. Fold either into the list so
        an old job loads without losing its target and there is one field to read
        from here on. A present `urls` always wins.
        """
        if isinstance(data, dict) and "urls" not in data and "url" in data:
            single = data.get("url")
            data = {**data, "urls": [single] if single else []}
        return data


class SweepTask(TaskBase):
    """A listings sweep, as persisted to the volume.

    A sweep spans one *or more* source URLs (a multi-URL fan-out that lands in
    one record), so the target is `urls`, a list. `source` is the representative
    adapter name for the batch — every URL in v1 is BizBuySell, and each Listing
    still carries its own `source`, so nothing downstream depends on this being
    a single value.
    """

    kind: Literal["sweep"] = "sweep"
    source: str = ""
    max_pages: int = 1
    sync: bool = False
    db_id: str = ""
    pages_crawled: int = 0
    listings: list[Listing] = Field(default_factory=list)
    synced: SyncResult | None = None


class ArchiveTask(TaskBase):
    """One `archive_page` run, as persisted to the volume.

    It leases a pooled task identity through the same capacity gate a sweep
    does and takes about as long, so it is a task in exactly the sense the
    dashboard means — it was invisible there only because nothing wrote it down.
    """

    kind: Literal["archive"] = "archive"
    # Where the content was appended. A sweep's destination is a database
    # (`db_id`); an archive's is one page, and they are not the same thing.
    notion_page_id: str = ""
    title: str = ""
    blocks_appended: int = 0
    used_path: str = ""


# The stored task, discriminated by `kind`. Reading a record goes through this
# (see services/jobs.py) so the loader returns the concrete kind rather than a
# union of every field any task might have.
Task = Annotated[SweepTask | ArchiveTask, Field(discriminator="kind")]


class ScrapeResult(BaseModel):
    """The result of a sweep.

    While status is "working" the sweep is still running and `listings` is
    empty — collect it with get_scrape_listing_results. `synced` is null when
    sync was false, which means nothing was saved rather than nothing was found.

    What `listings` holds once completed depends on how the sweep was started.
    sync=false: every listing found, each with an empty `synced_row_id`.
    sync=true: only the listings this sweep newly added to the store, each
    carrying the `synced_row_id` of the row it was written to (hand straight to
    archive_page). Already-stored listings are left out of `listings` but counted
    in `synced.existing`.
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
    def of(cls, job: SweepTask) -> "ScrapeResult":
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
    geoip: bool = Field(
        default=True,
        description="With a configured proxy, match timezone/locale to its measured exit. "
        "Ignored in direct mode, which is not geolocated by this service.",
    )
    humanize: bool = True
    human_preset: str = "careful"
    ttl_min: int | None = None
    width: int = 1440
    height: int = 900


class ProfileCreate(BaseModel):
    """Create one durable browser identity."""

    name: str
    country: str | None = None
    region: str | None = None


class ProfileUpdate(BaseModel):
    """Changes applied to a durable profile; omitted fields stay unchanged."""

    name: str
    new_name: str | None = None
    country: str | None = None
    region: str | None = None


class ProfileNameRequest(BaseModel):
    """Select a profile for a non-update management operation."""

    name: str


class ProfileView(BaseModel):
    """A safe profile status.

    A profile contains a fingerprint seed, sticky proxy session token, cookie
    directory, and browser storage internally. None of those credentials or
    identifiers are exposed here.
    """

    name: str
    country: str
    region: str
    is_default: bool
    in_use: bool = Field(
        description="True while a browser is queued, opening, open, or closing on this profile."
    )
    proxy_configured: bool = Field(
        description="Whether a complete residential proxy is configured for profile sessions."
    )


class ProfileDeleteResult(BaseModel):
    """Confirmation that a profile and its persisted browser data were deleted."""

    ok: bool = True
    name: str


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
    geoip: bool = Field(
        default=True,
        description="Whether proxy-exit geolocation was applied. False in direct mode.",
    )
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


class StagedFile(BaseModel):
    """One file staged on this server, ready for `agent_browser`'s `upload` verb."""

    path: str = Field(
        description="Absolute path on this server. Pass it to agent_browser as "
                    "`upload @eN <path>`. It is accepted only while its upload URL is "
                    "live, and only for the caller it was staged for."
    )
    name: str = Field(description="The filename as stored — sanitized, and given a "
                                  "-1/-2 suffix if another file already had that name.")
    bytes: int
    sha256: str = Field(description="Of the stored bytes. Posting the same file twice "
                                    "returns the first copy's path rather than a second copy.")
    content_type: str = Field(description="Decided by the file's first bytes, not by its "
                                          "name or the Content-Type that was sent.")


class StagedUpload(StagedFile):
    """The answer to one POST to an upload URL.

    The top-level fields describe the FIRST file, and `files` lists every file
    the request staged. That shape is deliberate rather than accidental: the
    pre-baked curl posts one file and a model should be able to read `path`
    without indexing, while `-F file=@a -F file=@b` is a natural thing to type
    and deserves a complete answer instead of a silent one.
    """

    files: list[StagedFile] = Field(
        description="Every file this request staged, in the order they were sent. "
                    "For a single-file POST it holds exactly the record above."
    )


class UploadTicket(BaseModel):
    """A temporary URL for putting a file on this server, and the bearer for it.

    Minted fresh per call and never stored, like the CDP and VNC URLs: `token`
    is a short-lived grant to add bytes to THIS one staging slot and nothing
    else. It is not the OAuth access token and cannot be used as one.
    """

    handle: str
    upload_url: str = Field(description="POST files here as multipart/form-data.")
    token: str = Field(
        description="Send as `Authorization: Bearer <token>`. Header only — this "
                    "endpoint does not accept the token in the URL."
    )
    curl: str = Field(
        description="The whole command with the URL and token already filled in. Run "
                    "it as-is and change only the filename."
    )
    expires_at: float = Field(description="Unix seconds. After this the URL stops "
                                          "accepting files and the staged paths stop resolving.")
    expires_in: int = Field(description="Seconds the ticket is good for, from now.")
    max_files: int = Field(description="Most files one upload URL will hold.")
    max_bytes_per_file: int = Field(description="Largest single file it will accept.")
    accepts: list[str] = Field(
        description="Content types accepted, decided by the file's first bytes rather "
                    "than by its name or declared type."
    )


class ProxyInfo(BaseModel):
    configured: bool = Field(
        description="Whether the optional residential proxy is fully set up. False can mean "
        "the valid direct mode; inspect status to distinguish direct from incomplete."
    )
    status: str = Field(description="direct / incomplete / untested / working / broken.")
    country: str | None = Field(default=None, description="Configured proxy's default country.")
    region: str | None = Field(default=None, description="Configured proxy's default region.")


class BrowserInfo(BaseModel):
    build: Literal["public", "pro", "pro-unverified"] = Field(
        description="Selected/resolved build: public, pro, or pro-unverified."
    )
    pro: bool | None = Field(
        description="True/false only after the selected artifact is known (or public was "
                    "explicitly selected); null means a Pro key is saved but has not been "
                    "resolved successfully in this process."
    )
    version: str = Field(description="The running or pinned CloakBrowser version, or 'latest'.")
    windows_fonts: str = Field(description="Windows-font availability. Not bundled — they are "
                               "proprietary and, per the fonts gate, not required for the "
                               "target sites.")


class PoolInfo(BaseModel):
    max: int = Field(description="Most browsers that may run at once.")
    reserved: int = Field(description="Slots kept for interactive (agent/human) use.")
    in_use: int = Field(description="Browsers running right now.")
    recommended_max: int | None = Field(
        default=None,
        description="Most browsers this container's detected memory can safely run, or "
                    "null when the limit could not be read. If 'max' exceeds this, launches "
                    "may fail under load ('Page crashed') or at the OS ('Resource "
                    "temporarily unavailable').",
    )


class NotionInfo(BaseModel):
    connected: bool = Field(description="Whether a Notion token and database are set.")


class ServerInfo(BaseModel):
    """A read-only status snapshot of the server's setup. Never carries a secret —
    no proxy password, no licence key, no Notion token; status and version only."""

    proxy: ProxyInfo
    browser: BrowserInfo
    pool: PoolInfo
    notion: NotionInfo


class Health(BaseModel):
    ok: bool = True
    service: str = "cloak-biz-scraper"
    version: str
    configured: bool = Field(
        description="Whether launch settings are structurally complete. A licence and "
                    "residential proxy are optional; a partial proxy is not. This does not "
                    "retest a saved key or proxy."
    )
    instances: int = 0
