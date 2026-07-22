"""Sweeping a search-results page for listings.

The shape of this service is set by one constraint: **a sweep is longer than any
MCP client will wait.** Multiple pages, a warmup, deliberate pacing, and up to
three attempts with a fresh exit IP each — that is minutes, against a client wall
of roughly four. So starting a sweep and collecting it are two calls, and
`start` returns the moment the job is written down.

The other constraint is that the scrape half must not know where listings land.
`sync=false` is a pure scrape: no store is constructed, no token is read, and
nothing is written. That is not a flag on a Notion code path, it is the absence
of one — which is what makes this usable by someone who has not configured
Notion at all.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from .. import sources
from ..config import CONFIG
from ..models import Job, Listing, ScrapeResult, SyncResult
from ..stores.base import ListingStore
from .blocker import text_contains_blocker
from .browsing import capture, gesture, scrape_with_retry
from .jobs import JobStore
from .settings import SettingsService
from .task_profiles import TaskProfilePool

logger = logging.getLogger("cloakbiz.scrape")

# Time on the page before reading it. Long enough for the cards to render and
# for the visit not to look instantaneous.
_WAIT_MS = 12_000
_ATTEMPTS = 3
_MAX_PAGES_CEILING = 20


class NotionNotConfigured(RuntimeError):
    """sync=true was asked for without a database to sync into."""


def _collect_message(job_id: str) -> str:
    """The instruction the model reads when a sweep starts.

    Phrased as an instruction rather than a status because it is one: the tool
    has returned but the work has not, and a model that does not call back will
    silently report zero listings for a sweep that is running fine.
    """
    return (
        f"Sweep started (job {job_id}). Call get_scrape_listing_results with "
        f"job_id={job_id} to collect. It runs for a few minutes — if the status "
        f"is still 'working', wait a little and call again."
    )


class ScrapeService:
    def __init__(self, instances, jobs: JobStore, settings: SettingsService,
                 store_factory=None, task_profiles: TaskProfilePool | None = None) -> None:
        self._instances = instances
        self._jobs = jobs
        self._settings = settings
        # Injected so the sweep never imports Notion. The default is resolved
        # lazily and only when sync=true, so a user with no Notion token can
        # still scrape.
        self._store_factory = store_factory or _default_store
        # A bounded pool of reusable task-N browser identities, replacing the old
        # per-URL serp-<path> profiles that accumulated on the volume forever.
        # Injectable for tests; built from the instance manager's ProfileStore in
        # normal use. None only when there is no instance manager (unit tests that
        # stub _sweep) — release() below is guarded for that case.
        self._task_profiles = task_profiles
        if self._task_profiles is None and instances is not None:
            self._task_profiles = TaskProfilePool(instances.profiles, settings)
        self._running: set[asyncio.Task] = set()
        # Admission gate: at most task_budget sweeps run past this point at once.
        # The instance pool's cap only bites INSIDE launch, but start() spawns an
        # unbounded background task per call, so without this every concurrent
        # sweep would acquire (and mint) a profile before any of them blocked on a
        # slot — the profile count would track peak concurrency, not the budget.
        # A sweep leases its task profile only after passing this gate, so the pool
        # can never mint more than task_budget profiles. The bound is re-read from
        # settings on every wait, so it tracks the Pool setting rather than a stale
        # value captured at construction. Excess sweeps queue here — they would
        # have queued on the instance slot anyway, so there is no throughput loss
        # and no deadlock (the gate cap equals the instance pool's task cap).
        self._gate = asyncio.Condition()
        self._past_gate = 0

    @property
    def in_flight(self) -> int:
        """Sweeps currently running. The heartbeat asks this to decide whether the
        machine must be kept awake."""
        return len(self._running)

    def start(self, url: str, *, max_pages: int = 1, sync: bool = False,
              db_id: str | None = None) -> Job:
        """Validate, write the job down, and return without waiting for it.

        Everything that can be known to be wrong before the browser starts is
        decided here, so the caller gets a real error instead of a job id that
        fails a minute later: an unreadable URL, or a sync with nowhere to sync
        to. A job record is only created once the sweep is genuinely going to run.
        """
        source = sources.for_url(url)  # raises UnsupportedURL, naming what works
        max_pages = max(1, min(int(max_pages), _MAX_PAGES_CEILING))

        target_db = ""
        if sync:
            settings = self._settings.load()
            target_db = (db_id or settings.notion_db_id or "").strip()
            if not settings.notion_api_token or not target_db:
                raise NotionNotConfigured(
                    "sync=true asks for the listings to be saved, but no Notion database is "
                    "set up. Either add your Notion token and pick a database under "
                    "Settings, pass db_id explicitly, or call this with sync=false to just "
                    "read the listings back without saving them."
                )

        # The instruction names the job id, and the id is minted by create(), so
        # the summary is filled in by the same write rather than a second one.
        job = self._jobs.create(
            source=source.name, url=url, max_pages=max_pages, sync=sync, db_id=target_db,
            status="working", summary=_collect_message,
        )

        task = asyncio.create_task(self._run(job, source))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return job

    def result(self, job_id: str) -> ScrapeResult | None:
        """The job as it stands. Never blocks, never waits, never launches anything."""
        job = self._jobs.get(job_id)
        return ScrapeResult.of(job) if job else None

    async def _enter_gate(self) -> None:
        """Block until fewer than task_budget sweeps are past the gate, then admit.

        The budget is re-read from settings on each wake, so raising or lowering
        the Pool setting takes effect without a restart.
        """
        async with self._gate:
            await self._gate.wait_for(
                lambda: self._past_gate < self._settings.load().task_budget
            )
            self._past_gate += 1

    async def _leave_gate(self) -> None:
        async with self._gate:
            self._past_gate -= 1
            self._gate.notify_all()

    async def _run(self, job: Job, source) -> None:
        # Admission first: a sweep leases its profile only once it is past the
        # gate, capping minted profiles at task_budget under any concurrency.
        await self._enter_gate()
        try:
            res = await self._sweep(job, source)
            listings: list[Listing] = res.get("data", {}).get("listings", [])
            job.pages_crawled = res.get("data", {}).get("pages_crawled", 0)
            job.listings = listings

            if res.get("blocked"):
                job.status = "failed"
                job.error = (
                    f"{urlparse(job.url).hostname} served an anti-bot page instead of "
                    f"results on every attempt, each from a different exit IP. This "
                    f"usually clears on its own — try again in a few minutes."
                )
                job.summary = "Blocked by the site."
            elif res.get("error"):
                job.status = "failed"
                job.error = res["error"]
                job.summary = "Sweep failed."
            else:
                job.status = "completed"
                if job.sync:
                    job.synced = await self._sync(job, listings)
                job.summary = self._summarize(job)
        except Exception as exc:  # noqa: BLE001 — the job must record its own failure
            logger.exception("sweep %s failed", job.id)
            job.status = "failed"
            job.error = str(exc)
            job.summary = "Sweep failed."
        finally:
            # Return the task profile on every path — success, block, error, or
            # cancel — so a crashed sweep never leaks a lease and pins a profile
            # as busy forever. Released even if the launch itself failed, since
            # acquire happens before scrape_with_retry inside _sweep. Idempotent,
            # so a sweep that never acquired (stubbed _sweep) is a safe no-op.
            if self._task_profiles is not None:
                self._task_profiles.release(job.id)
            self._jobs.save(job)
            logger.info("job %s -> %s (%d listings)", job.id, job.status, len(job.listings))
            # Leave the gate last, and shielded, so the slot is returned even if
            # this sweep is cancelled during shutdown — the job is already saved,
            # so a queued sweep can take the freed slot.
            await asyncio.shield(self._leave_gate())

    def _summarize(self, job: Job) -> str:
        pages = f"{job.pages_crawled} page{'s' if job.pages_crawled != 1 else ''}"
        head = f"Found {len(job.listings)} listing(s) across {pages}."
        if job.synced is None:
            return f"{head} Nothing was saved (sync=false)."
        out = f"{head} Saved {job.synced.new} new, {job.synced.existing} already known."
        if job.synced.skipped:
            out += (
                f" These columns could not be filled: {', '.join(job.synced.skipped)}"
                f" — see Settings for why."
            )
        return out

    async def _sync(self, job: Job, listings: list[Listing]) -> SyncResult:
        settings = self._settings.load()
        store: ListingStore = self._store_factory(settings)
        # The map belongs to the configured database. If the sweep targets a
        # different db_id (an agent passing one explicitly), the map does not
        # apply to it — fall back to identity mapping, which is safe for any
        # correctly-named database.
        column_map = settings.notion_column_map or None
        if job.db_id != settings.notion_db_id:
            column_map = None
        result = await store.upsert_new(job.db_id, listings, column_map=column_map)
        return SyncResult(
            new=result.new, existing=result.existing, db_id=result.db_id,
            skipped=result.skipped_names,
        )

    async def _sweep(self, job: Job, source) -> dict:
        evidence = CONFIG.evidence_dir / job.id
        # Lease a pooled task-N identity for this sweep. Bounded and reused across
        # sweeps, so profiles no longer accumulate one-per-URL on the volume. The
        # lease is returned in _run's finally, so acquiring here (before any launch)
        # means even a launch failure releases it. Two concurrent sweeps can never
        # get the same profile — the pool is the sole lease authority — so they
        # cannot collide on Chromium's singleton lock.
        profile = self._task_profiles.acquire(job.id)
        return await scrape_with_retry(
            self._instances,
            profile=profile,
            owner=f"job:{job.id}",
            wait_ms=_WAIT_MS,
            attempts=_ATTEMPTS,
            warmup_url=getattr(source, "warmup_url", None),
            scrape_once=lambda inst, page: self._sweep_once(inst, page, job, source, evidence),
        )

    async def _sweep_once(self, inst, page, job: Job, source, evidence: Path) -> dict:
        listings: list[Listing] = []
        seen: set[str] = set()
        pages_done = 0

        for n in range(1, job.max_pages + 1):
            inst.touch()
            url = source.page_url(job.url, n)
            await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_timeout(_WAIT_MS)
            await gesture(page)

            result = await source.cards(page)
            pages_done += 1
            if result.blocked or text_contains_blocker(result.title):
                await capture(page, evidence / f"page-{n:02d}-blocked",
                              {"url": url, "reason": "blocked", "proxy_ip": inst.proxy_ip})
                return {"blocked": True, "error": None,
                        "data": {"listings": listings, "pages_crawled": pages_done}}

            # Paging stops on cards this crawl has already seen, not on cards the
            # store already has: a feed whose first two pages are all known
            # listings still has new ones on page three, and dedupe is a separate
            # question answered at the end.
            fresh = 0
            for listing in result.listings:
                if listing.url in seen:
                    continue
                seen.add(listing.url)
                fresh += 1
                listings.append(listing)
            if fresh == 0 and n > 1:
                break

        await capture(page, evidence / "final",
                      {"url": job.url, "reason": "success", "found": len(listings),
                       "pages_crawled": pages_done, "proxy_ip": inst.proxy_ip})
        return {"blocked": False, "error": None,
                "data": {"listings": listings, "pages_crawled": pages_done}}


def _default_store(settings) -> ListingStore:
    """Resolved here, and only on the sync path, so importing this module never
    imports Notion."""
    from ..stores.notion import NotionStore

    return NotionStore(settings.notion_api_token)
