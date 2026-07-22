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

# The summary shown while a sweep is admitted but BLOCKED waiting for capacity —
# either at the admission gate (task_budget) or at the instance-manager slot
# wait inside launch. The status stays "working" (so get_scrape_listing_results
# consumers are unaffected), but the summary distinguishes "queued behind a full
# pool" from "actively scraping", which is otherwise an opaque wait. Cleared the
# moment the browser is obtained (see _sweep's on_launch).
_WAITING_SUMMARY = "Waiting for a free browser slot…"
_SCRAPING_SUMMARY = "Sweeping the search results…"


class NotionNotConfigured(RuntimeError):
    """sync=true was asked for without a database to sync into."""


# ── The task-label interface ─────────────────────────────────────────────────
#
# Every task type provides a `describe(job) -> str` that names one of its jobs
# for the dashboard, COLOCATED with that task. The common interface is just this
# signature — there is no central dispatcher to edit. Today the listing sweep is
# the only task, so `describe` below is the only implementation; a future task
# type adds its own `describe` next to ITS definition and wires that in where the
# job is displayed. The UI asks the task for the label; it never builds one from
# site or task strings itself.
def describe(job: Job) -> str:
    """Name a listing-sweep job: verb · source label · count.

    The source label comes from the adapter that owns the job's `source` id
    (`sources.label_for`), so the site's display name lives with the site, not
    here. A single-URL sweep drops the count — "1 sources" would be noise.
    """
    label = sources.label_for(job.source)
    n = len(job.urls)
    if n > 1:
        return f"Listing sweep · {label} · {n} sources"
    return f"Listing sweep · {label}"


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

    def start(self, urls: list[str], *, max_pages: int = 1, sync: bool = False,
              db_id: str | None = None) -> Job:
        """Validate, write the job down, and return without waiting for it.

        Everything that can be known to be wrong before the browser starts is
        decided here, so the caller gets a real error instead of a job id that
        fails a minute later: an empty list, no readable URL at all, or a sync
        with nowhere to sync to. A job record is only created once the sweep is
        genuinely going to run.

        `urls` fan out concurrently into ONE job. A single URL that isn't a
        supported listings page is not fatal — it is recorded as that source's
        failure and the rest still run — so `start` only refuses the batch when
        *nothing* in it is readable (there would be no sweep to run).
        """
        if not urls:
            raise ValueError(
                "scrape_listings needs at least one URL in 'urls', but the list was empty. "
                "Pass one or more search-results (SERP) or broker-profile URLs to sweep."
            )
        max_pages = max(1, min(int(max_pages), _MAX_PAGES_CEILING))

        # Resolve each URL's adapter up front, keeping None for the unreadable
        # ones so they can be reported per-source rather than sinking the batch.
        targets: list[tuple[str, object | None]] = []
        first_unsupported: sources.UnsupportedURL | None = None
        for url in urls:
            try:
                targets.append((url, sources.for_url(url)))
            except sources.UnsupportedURL as exc:
                first_unsupported = first_unsupported or exc
                targets.append((url, None))
        if all(source is None for _, source in targets):
            # Not one URL is a page we can read: there is no sweep to start, so
            # fail loudly with the message that names what IS supported rather
            # than mint a job that can only fail.
            raise first_unsupported

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

        # The representative source for the batch (each Listing still records its
        # own). The instruction names the job id, and the id is minted by
        # create(), so the summary is filled in by the same write rather than a
        # second one.
        source_name = next(s.name for _, s in targets if s is not None)
        job = self._jobs.create(
            source=source_name, urls=urls, max_pages=max_pages, sync=sync, db_id=target_db,
            status="working", summary=_collect_message,
        )

        task = asyncio.create_task(self._run(job, targets))
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

    async def _run(self, job: Job, targets: list[tuple[str, object | None]]) -> None:
        # Fan the URLs out concurrently, but never past the pool's task budget.
        # Two bounds hold at once: a per-job Semaphore(task_budget) — the ported
        # run_targets pattern — caps how many of THIS job's sources are in flight,
        # and the shared admission gate (also task_budget) caps concurrent task
        # browsers across EVERY job. Each source enters the gate and leases its
        # own task profile independently, so the profile pool still mints at most
        # task_budget identities no matter how many URLs or jobs pile up.
        total = len(targets)
        prog = _RunProgress(self._jobs, job, total)
        # Make the wait visible before blocking on it: until a browser is in hand
        # the job is queued (the gate, then the slot wait inside launch), and the
        # summary says so — a full pool, not a stuck sweep. Status stays "working".
        prog.render()
        parallel = max(1, self._settings.load().task_budget)
        sem = asyncio.Semaphore(parallel)

        async def worker(i: int, url: str, source) -> dict:
            async with sem:
                return await self._sweep_url(job, i, url, source, prog)

        try:
            outcomes = await asyncio.gather(
                *(worker(i, url, source) for i, (url, source) in enumerate(targets))
            )
            listings, pages, ok, failures = self._merge(targets, outcomes)
            job.listings = listings
            job.pages_crawled = pages
            if ok == 0:
                # Every source failed — only now is the whole job a failure.
                job.status = "failed"
                job.error = self._failure_text(failures, total)
                job.summary = f"All {total} source(s) failed."
            else:
                job.status = "completed"
                if job.sync:
                    # Dedupe+upsert the MERGED set ONCE, not per source.
                    job.synced = await self._sync(job, listings)
                if failures:
                    job.error = self._failure_text(failures, total)
                job.summary = self._summarize(job, ok, total, failures)
        except Exception as exc:  # noqa: BLE001 — the job must record its own failure
            logger.exception("sweep %s failed", job.id)
            job.status = "failed"
            job.error = str(exc)
            job.summary = "Sweep failed."
        finally:
            self._jobs.save(job)
            logger.info(
                "job %s -> %s (%d listings across %d source(s))",
                job.id, job.status, len(job.listings), total,
            )

    def _merge(self, targets, outcomes) -> tuple[list[Listing], int, int, list[tuple[str, str]]]:
        """Fold every source's outcome into one deduped result.

        Returns (merged listings, total pages crawled, count of sources that
        succeeded, list of (url, reason) for the ones that failed). Dedupe uses
        the same identity the rest of the system does — listing_id, then
        normalized_url — so the same listing surfacing on two SERP pages, or on
        two of the swept URLs, is counted once.
        """
        listings: list[Listing] = []
        seen: set[str] = set()
        pages = 0
        ok = 0
        failures: list[tuple[str, str]] = []
        for (url, _source), res in zip(targets, outcomes):
            data = res.get("data") or {}
            pages += data.get("pages_crawled", 0) or 0
            if res.get("blocked"):
                failures.append((url, "blocked"))
                continue
            if res.get("error"):
                failures.append((url, res["error"]))
                continue
            ok += 1
            for listing in data.get("listings", []):
                key = listing.listing_id or listing.normalized_url or listing.url
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                listings.append(listing)
        return listings, pages, ok, failures

    def _failure_text(self, failures: list[tuple[str, str]], total: int) -> str:
        bits = []
        for url, reason in failures:
            host = urlparse(url).hostname or url
            bits.append(f"{host} ({'blocked by the site' if reason == 'blocked' else reason})")
        text = f"{len(failures)} of {total} source(s) failed: " + "; ".join(bits) + "."
        if any(reason == "blocked" for _, reason in failures):
            text += (
                " A blocked source served an anti-bot page instead of results, each attempt "
                "from a different exit IP. This usually clears on its own — try again in a "
                "few minutes."
            )
        return text

    def _summarize(self, job: Job, ok: int, total: int, failures: list[tuple[str, str]]) -> str:
        pages = f"{job.pages_crawled} page{'s' if job.pages_crawled != 1 else ''}"
        parts = [f"{ok} of {total} source(s) swept · {len(job.listings)} listing(s) across {pages}"]
        if job.synced is None:
            parts.append("Nothing was saved (sync=false)")
        else:
            seg = f"Saved {job.synced.new} new, {job.synced.existing} already known"
            if job.synced.skipped:
                seg += (
                    f"; these columns could not be filled: {', '.join(job.synced.skipped)}"
                    f" — see Settings for why"
                )
            parts.append(seg)
        if failures:
            parts.append(f"{len(failures)} source(s) failed")
        return " · ".join(parts)

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

    def _evidence_dir(self, job: Job, i: int) -> Path:
        """Where source `i`'s screenshots and snapshots land.

        Namespaced per source under the job's own directory so two URLs swept
        into one job never overwrite each other's captures. The job-level
        directory (CONFIG.evidence_dir / job.id) still holds all of them, so the
        /runs listing and ScrapeResult.evidence_dir are unchanged.
        """
        return CONFIG.evidence_dir / job.id / f"source-{i + 1:02d}"

    async def _sweep_url(self, job: Job, i: int, url: str, source, prog: "_RunProgress") -> dict:
        """Sweep one URL. Never raises: a single source failing is recorded and
        returned so the batch (see _run's gather) survives it.

        The gate is entered here and left in `finally`, so each concurrent source
        holds exactly one admission slot for its lifetime — the same capacity
        ceiling the single-sweep path had, applied per source.
        """
        if source is None:
            # An unreadable URL never reaches the pool or the gate; it is simply
            # this source's failure.
            prog.mark_done(i)
            return {
                "url": url, "blocked": False,
                "error": f"not a supported listings page: {url}",
                "data": {"listings": [], "pages_crawled": 0},
            }
        # Admission first: a source leases its profile only once it is past the
        # gate, capping minted profiles at task_budget under any concurrency.
        await self._enter_gate()
        try:
            res = await self._sweep(job, i, url, source, prog)
        except Exception as exc:  # noqa: BLE001 — one source failing must not kill the batch
            logger.warning("source %s in job %s failed: %s", url, job.id, exc)
            res = {"blocked": False, "error": str(exc),
                   "data": {"listings": [], "pages_crawled": 0}}
        finally:
            prog.mark_done(i)
            # Leave the gate last, and shielded, so the slot is returned even if
            # this source is cancelled during shutdown — a queued source can then
            # take the freed slot.
            await asyncio.shield(self._leave_gate())
        res["url"] = url
        return res

    async def _sweep(self, job: Job, i: int, url: str, source, prog: "_RunProgress") -> dict:
        evidence = self._evidence_dir(job, i)
        # Lease a pooled task-N identity for this source, keyed per source so one
        # source releasing its lease never frees another's. Bounded and reused
        # across sweeps, so profiles no longer accumulate one-per-URL on the
        # volume. The lease is returned in the finally, so acquiring here (before
        # any launch) means even a launch failure releases it. Two concurrent
        # sources can never get the same profile — the pool is the sole lease
        # authority — so they cannot collide on Chromium's singleton lock.
        lease_key = f"{job.id}:{i}"
        profile = self._task_profiles.acquire(lease_key)

        def on_launch(_inst) -> None:
            # The slot wait is over for this source — a browser is in hand and
            # scraping is about to start. Advance the aggregate progress summary.
            prog.mark_sweeping(i)

        try:
            return await scrape_with_retry(
                self._instances,
                profile=profile,
                owner=f"job:{job.id}",
                wait_ms=_WAIT_MS,
                attempts=_ATTEMPTS,
                warmup_url=getattr(source, "warmup_url", None),
                scrape_once=lambda inst, page: self._sweep_once(inst, page, job, url, source, evidence),
                on_launch=on_launch,
            )
        finally:
            if self._task_profiles is not None:
                self._task_profiles.release(lease_key)

    async def _sweep_once(self, inst, page, job: Job, url: str, source, evidence: Path) -> dict:
        listings: list[Listing] = []
        seen: set[str] = set()
        pages_done = 0

        for n in range(1, job.max_pages + 1):
            inst.touch()
            target = source.page_url(url, n)
            await page.goto(target, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_timeout(_WAIT_MS)
            await gesture(page)

            result = await source.cards(page)
            pages_done += 1
            if result.blocked or text_contains_blocker(result.title):
                await capture(page, evidence / f"page-{n:02d}-blocked",
                              {"url": target, "reason": "blocked", "proxy_ip": inst.proxy_ip})
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
                      {"url": url, "reason": "success", "found": len(listings),
                       "pages_crawled": pages_done, "proxy_ip": inst.proxy_ip})
        return {"blocked": False, "error": None,
                "data": {"listings": listings, "pages_crawled": pages_done}}


class _RunProgress:
    """The aggregate job's live summary while its sources fan out.

    A multi-URL job's status stays "working" until every source is done, but the
    summary should say *what* is happening: queued behind a full pool, or sweeping
    N sources with M finished. It is recomputed from counters on each source's
    transition (all in the one event loop, so no lock is needed) and only while
    the job is still working — the final summary is _run's to write.

    For a single-source job the text collapses to the exact strings the
    single-sweep path shipped (`_WAITING_SUMMARY` / `_SCRAPING_SUMMARY`), so that
    UX is unchanged.
    """

    def __init__(self, jobs: JobStore, job: Job, total: int) -> None:
        self._jobs = jobs
        self._job = job
        self._total = total
        self._sweeping: set[int] = set()
        self._done: set[int] = set()

    def render(self) -> None:
        if self._job.status != "working":
            return
        sweeping, done, total = len(self._sweeping), len(self._done), self._total
        if not sweeping and not done:
            summary = _WAITING_SUMMARY
        elif total == 1:
            summary = _SCRAPING_SUMMARY
        else:
            summary = f"Sweeping {total} sources… ({done} of {total} done)"
        self._job.summary = summary
        self._jobs.save(self._job)

    def mark_sweeping(self, i: int) -> None:
        if i not in self._sweeping and i not in self._done:
            self._sweeping.add(i)
            self.render()

    def mark_done(self, i: int) -> None:
        self._sweeping.discard(i)
        self._done.add(i)
        self.render()


def _default_store(settings) -> ListingStore:
    """Resolved here, and only on the sync path, so importing this module never
    imports Notion."""
    from ..stores.notion import NotionStore

    return NotionStore(settings.notion_api_token)
