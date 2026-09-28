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

Which source reads a URL is decided here too, because only this service knows
what the Settings say. A URL a site adapter matches is read by it. A URL on a
site with no adapter is read by the generic reader (`sources/generic.py`) —
when the TypeSafe Classifier (e.g. Jev) key is saved, since the reader cannot
decide anything without it, and with that site's override if one is pinned. A
URL on a site that HAS an adapter, which the adapter does not read, is refused:
it never falls through.

**Triage** runs after a synced sweep when the call carries a `triage_prompt`:
every row this sweep inserted, plus every row it saw whose Bot Triage is still
blank, gets REVIEW or REJECT from the TypeSafe Classifier (e.g. Jev) — on the
card first, then (for a card REVIEW) on the listing's detail page, which is
archived into the row when the verdict stays REVIEW. A row that already has a
decision is never judged again. See `_TriagePhase` for the order of writes, and
services/triage.py for the question itself. Without a prompt none of it runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .. import sources
from ..config import CONFIG
from ..models import (
    Listing,
    ScrapeResult,
    SweepTask,
    SyncResult,
    TriagedRow,
    TriageFailure,
    TriageSummary,
)
from ..sources.generic import GenericSource
from ..sources.overrides import OverridesInvalid, SiteOverride, override_for, parse_overrides
from ..stores.base import ListingStore, TriageUnavailable, UpsertResult
from . import legibility
from .archive import GUARD_THRESHOLD
from .archive import guard as archive_guard
from .blocker import text_contains_blocker
from .browsing import capture, gesture, scrape_with_retry
from .jobs import JobStore, interrupted
from .settings import SettingsService
from .task_profiles import TaskProfilePool
from .triage import REJECT, REVIEW, TriageDecision, Triager, criteria_version
from .typesafe import TypeSafeError, TypeSafeUnavailable

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

# Why a site with no adapter is refused when no classifier key is saved. Says
# where the key goes, because that is the whole fix.
NEEDS_CLASSIFIER = (
    "Reading sites other than BizBuySell needs the TypeSafe Classifier (e.g. Jev) — add an "
    "OpenRouter key under Settings → TypeSafe Classifier (e.g. Jev)."
)


class NotionNotConfigured(RuntimeError):
    """sync=true was asked for without a database to sync into."""


class TriageNotConfigured(NotionNotConfigured):
    """A `triage_prompt` was given, and triage cannot run — said before anything starts.

    A NotionNotConfigured, so every façade already refuses it as a setup problem
    (the MCP tool's answer, a 409 over REST, a banner in the dashboard): the
    prompt is blank, sync is off, no classifier key is saved or the saved one
    failed its check, or the Notion database has nowhere to record a decision.
    Raised for the whole call — even a BizBuySell-only one — because a sweep
    asked to triage that cannot would save rows and leave every one of them
    undecided. `cause` is the classifier check's exception when that is the
    reason, so REST can answer an outage with 503 like ClassifierNotReady.
    """

    def __init__(self, message: str, cause: TypeSafeError | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    @property
    def transient(self) -> bool:
        return isinstance(self.cause, TypeSafeUnavailable)


@dataclass
class _TriagePlan:
    """A sweep's triage, from the call that asked for it to the end of the run.

    `store` and `target` are filled by `submit`'s preflight — the database was
    read once there to prove Bot Triage has somewhere to go — and reused by the
    run, so the save and every triage write share the store's one client. A
    sweep started with `start` directly has neither, and the run prepares them.
    """

    prompt: str
    version: str
    store: Any = None
    target: Any = None


class ClassifierNotReady(RuntimeError):
    """The sweep needs the TypeSafe Classifier (e.g. Jev), and it failed its check.

    Raised by `submit` before anything starts, when every readable URL in the
    call is read by the generic reader: each would fail on its first question,
    so a job id would be a promise of a result that cannot come. The message is
    the check's own — OpenRouter rejected the key, the account is out of
    credits, the service is not answering — because each is fixed somewhere
    different. `cause` is the check's exception, so a façade can tell an outage
    (`transient`: try again later) from a key problem (fix Settings first).
    """

    def __init__(self, message: str, cause: TypeSafeError | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    @property
    def transient(self) -> bool:
        return isinstance(self.cause, TypeSafeUnavailable)


@dataclass(frozen=True)
class _Target:
    """One URL of a sweep, with the source that reads it — or why none can.

    The reason travels with the URL because a batch is not refused for one bad
    URL: that URL becomes its own source's failure, and the job's error should
    say what was wrong with it ("needs the TypeSafe Classifier…"), not a
    generic "not supported".
    """

    url: str
    source: Any = None
    refusal: str = ""


class NotASweep(ValueError):
    """A sweep's id was expected and a task of another kind was given.

    Ids are minted from one sequence for every kind of task, so an archive's id
    looks exactly like a sweep's and an agent holding the wrong one gets a
    sentence saying so — rather than a ScrapeResult with an empty `listings`,
    which reads as "the sweep found nothing".
    """


# ── The task-label interface ─────────────────────────────────────────────────
#
# Every task type provides a `describe(job) -> str` that names one of its tasks
# for the dashboard, COLOCATED with that task. The common interface is just this
# signature. `archive_page` is the second implementation (services/archive.py),
# and adding it needed no edit here: routes/ui.py picks one by `kind`. The UI
# asks the task for the label; it never builds one from site or task strings
# itself.
def describe(job: SweepTask) -> str:
    """Name a listing-sweep job: verb · source label · count.

    The source label comes from the adapter that owns the job's `source` id
    (`sources.label_for`), so the site's display name lives with the site, not
    here. A single-URL sweep drops the count — "1 sources" would be noise.
    """
    label = sources.label_for(job.source)
    if job.source == sources.GENERIC_NAME:
        # "Any site" says nothing about which one; the site is the label. The
        # URLs a generic sweep read are the ones no adapter owns.
        sites = list(dict.fromkeys(
            site for u in job.urls if sources.owner_of(u) is None and (site := _site(u))
        ))
        if sites:
            label = sites[0] if len(sites) == 1 else f"{sites[0]} and {len(sites) - 1} more"
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
                 store_factory=None, task_profiles: TaskProfilePool | None = None,
                 typesafe=None, archive=None) -> None:
        self._instances = instances
        self._jobs = jobs
        self._settings = settings
        # The ArchiveService (app.state.archive): triage reads a listing's
        # detail page with its `read` — the same gate and pooled identities an
        # archive_page call uses — and files it with its idempotent `append`.
        # Optional: without it a triage_prompt is refused up front.
        self._archive = archive
        # The shared TypeSafe Classifier client (app.state.typesafe). Optional:
        # without it — or without a saved key, checked at sweep time so a key
        # added in Settings applies to the next sweep — the legibility check
        # runs its code half only.
        self._typesafe = typesafe
        # Injected so the sweep never imports Notion. The default is resolved
        # lazily and only when sync=true, so a user with no Notion token can
        # still scrape.
        self._store_factory = store_factory or _default_store
        # A bounded pool of reusable task-N browser identities, replacing the old
        # per-URL serp-<path> profiles that accumulated on the volume forever.
        # Injectable for tests; otherwise the instance manager's own pool — the
        # single lease authority, shared with archives, never a second one built
        # here. None only when there is no instance manager (unit tests that stub
        # _sweep) — release() below is guarded for that case.
        self._task_profiles = task_profiles
        if self._task_profiles is None and instances is not None:
            self._task_profiles = instances.task_profiles
        self._running: set[asyncio.Task] = set()
        # The parsed site overrides, keyed by the text they were parsed from, so
        # a sweep start re-parses only after the document was edited.
        self._overrides_text: str | None = None
        self._overrides_parsed: list[SiteOverride] = []
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

    async def submit(self, urls: list[str], *, max_pages: int = 1,
                     sync: bool = False, triage_prompt: str | None = None) -> SweepTask:
        """Start a sweep the way the tools and the dashboard do: preflight, then `start`.

        With a `triage_prompt`, `start`'s own refusals (an empty list, nothing
        readable, a sync with no database) and triage's (see `_triage_plan`)
        come first, because they cost nothing to find out.

        The preflight is the classifier's key. A URL read by the generic reader
        asks the TypeSafe Classifier (e.g. Jev) about every page, so a key that
        OpenRouter rejects, an account out of credits, or a service that is not
        answering would fail each such source on its first question — minutes
        into a job the caller has already been told is running. One tiny check
        now turns that into an answer now, and it is asked only when something
        needs it: a generic URL, or a `triage_prompt` (a BizBuySell-only call
        without one never pays for it).

        When it fails: a call with a `triage_prompt` is refused whole
        (`TriageNotConfigured`) — even a BizBuySell-only one, since it would
        save rows it then cannot decide. Otherwise, if every readable URL needs
        the classifier, the call is refused (`ClassifierNotReady`, with the
        check's own message); in a mixed batch the generic URLs become their
        own sources' failures with that message, and the BizBuySell ones run.

        With a `triage_prompt`, the Notion database is read once more to find
        where Bot Triage goes (`prepare_triage`); a database with nowhere to
        record a decision refuses the call before anything starts.
        """
        plan = None
        if triage_prompt is not None:
            self._admit(urls, max_pages, sync)
            plan = self._triage_plan(triage_prompt, sync)

        refused: dict[str, str] = {}
        targets, _ = self._resolve(urls or [])
        generic = [t for t in targets if isinstance(t.source, GenericSource)]
        if (generic or plan is not None) and self._typesafe is not None:
            check = await self._typesafe.check()
            if not check.ok and plan is not None:
                raise TriageNotConfigured(
                    "Can't start this sweep with triage: triage asks the TypeSafe Classifier "
                    "(e.g. Jev) about every new listing, and it failed its check just now. "
                    f"{check.message}",
                    check.error,
                )
            if not check.ok:
                readable = [t for t in targets if t.source is not None]
                if len(generic) == len(readable):
                    what = "this page" if len(generic) == 1 else f"these {len(generic)} pages"
                    raise ClassifierNotReady(
                        f"Can't start this sweep: reading {what} needs the TypeSafe Classifier "
                        f"(e.g. Jev), and it failed its check just now. {check.message}",
                        check.error,
                    )
                reason = ("needs the TypeSafe Classifier (e.g. Jev), which failed its check "
                          f"just now: {check.message}")
                refused = {t.url: reason for t in generic}
        if plan is not None:
            await self._prepare_triage(plan)
            return self.start(urls, max_pages=max_pages, sync=sync, refused=refused or None,
                              triage_plan=plan)
        if refused:
            return self.start(urls, max_pages=max_pages, sync=sync, refused=refused)
        return self.start(urls, max_pages=max_pages, sync=sync)

    def start(self, urls: list[str], *, max_pages: int = 1, sync: bool = False,
              refused: Mapping[str, str] | None = None,
              triage_prompt: str | None = None,
              triage_plan: _TriagePlan | None = None) -> SweepTask:
        """Validate, write the job down, and return without waiting for it.

        Everything that can be known to be wrong before the browser starts is
        decided here, so the caller gets a real error instead of a job id that
        fails a minute later: an empty list, no readable URL at all, or a sync
        with nowhere to sync to. A job record is only created once the sweep is
        genuinely going to run.

        `urls` fan out concurrently into ONE job. A single URL that can't be
        read is not fatal — it is recorded as that source's failure, with its
        own reason, and the rest still run — so `start` only refuses the batch
        when *nothing* in it is readable (there would be no sweep to run).
        `refused` names URLs a caller already knows cannot be read, each with
        its reason (`submit`'s preflight); they are failed the same way.

        `triage_prompt` gets the checks that cost nothing (see `_triage_plan`);
        the classifier's check and the Notion column are `submit`'s, which
        hands over what it prepared as `triage_plan`. A run started here with
        only a prompt prepares its triage target itself.
        """
        max_pages, targets, target_db = self._admit(urls, max_pages, sync, refused)
        plan = triage_plan
        if plan is None and triage_prompt is not None:
            plan = self._triage_plan(triage_prompt, sync)

        # The representative source for the batch (each Listing still records its
        # own). The instruction names the job id, and the id is minted by
        # create(), so the summary is filled in by the same write rather than a
        # second one.
        source_name = next(t.source.name for t in targets if t.source is not None)
        job = self._jobs.create(
            source=source_name, urls=urls, max_pages=max_pages, sync=sync, db_id=target_db,
            status="working", summary=_collect_message,
            triage=TriageSummary(criteria_version=plan.version) if plan is not None else None,
        )

        task = asyncio.create_task(self._run(job, targets, plan))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return job

    def _admit(self, urls: list[str], max_pages: int, sync: bool,
               refused: Mapping[str, str] | None = None,
               ) -> tuple[int, list[_Target], str]:
        """`start`'s refusals, and what it needs once they pass: the clamped
        page count, each URL's source, and the database a sync writes to."""
        if not urls:
            raise ValueError(
                "scrape_listings needs at least one URL in 'urls', but the list was empty. "
                "Pass one or more URLs of pages that list businesses for sale."
            )
        max_pages = max(1, min(int(max_pages), _MAX_PAGES_CEILING))

        # Resolve each URL's source up front, keeping the unreadable ones (with
        # their reasons) so they can be reported per-source rather than sinking
        # the batch.
        targets, first_unsupported = self._resolve(urls, refused)
        if all(t.source is None for t in targets):
            # Not one URL is a page we can read: there is no sweep to start, so
            # fail loudly with the first URL's own reason rather than mint a job
            # that can only fail.
            raise first_unsupported

        target_db = ""
        if sync:
            settings = self._settings.load()
            target_db = (settings.notion_db_id or "").strip()
            if not settings.notion_api_token or not target_db:
                raise NotionNotConfigured(
                    "sync=true asks for the listings to be saved, but no Notion database is "
                    "set up. Either add your Notion token and pick a database under "
                    "Settings, or call this with sync=false to just read the listings back "
                    "without saving them."
                )
        return max_pages, targets, target_db

    def _triage_plan(self, prompt: str | None, sync: bool) -> _TriagePlan:
        """The checks a `triage_prompt` needs that cost nothing, or raise
        TriageNotConfigured saying what to change."""
        text = (prompt or "").strip()
        if not text:
            raise TriageNotConfigured(
                "triage_prompt is empty. Pass the text of your triage criteria (what makes a "
                "listing one to reject), or leave triage_prompt out to sweep without triage."
            )
        if not sync:
            raise TriageNotConfigured(
                "Triage writes each decision into the listing's Notion row, so it needs "
                "sync=true. Call again with sync=true, or leave triage_prompt out to just read "
                "the listings."
            )
        if self._classifier() is None:
            raise TriageNotConfigured(
                "Triage needs the TypeSafe Classifier (e.g. Jev), and no OpenRouter key is "
                "saved for it. Add one under Settings → TypeSafe Classifier (e.g. Jev), or "
                "leave triage_prompt out."
            )
        if self._archive is None:
            raise TriageNotConfigured(
                "This server was started without its page reader, so it can't open listings' "
                "detail pages to triage them. Leave triage_prompt out."
            )
        return _TriagePlan(prompt=text, version=criteria_version(text))

    async def _prepare_triage(self, plan: _TriagePlan) -> None:
        """Find where Bot Triage is recorded, once, or refuse the call.

        The store built here is the one the run saves with, so the save and
        every triage write share its client (one pace for Notion's rate limit).
        """
        settings = self._settings.load()
        store: ListingStore = self._store_factory(settings)
        try:
            target = await store.prepare_triage(
                (settings.notion_db_id or "").strip(), settings.notion_column_map or None,
            )
        except TriageUnavailable as exc:
            raise TriageNotConfigured(f"Can't triage into your Notion database: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — any failure to read it is a refusal
            raise TriageNotConfigured(
                "Can't start this sweep with triage: reading your Notion database to find "
                f"where Bot Triage goes failed. {exc}"
            ) from exc
        plan.store, plan.target = store, target

    def _resolve(self, urls: list[str], refused: Mapping[str, str] | None = None,
                 ) -> tuple[list[_Target], sources.UnsupportedURL | None]:
        """Each URL's source, or the reason it has none; plus the first refusal."""
        targets: list[_Target] = []
        first: sources.UnsupportedURL | None = None
        for url in urls:
            try:
                if refused and url in refused:
                    raise sources.UnsupportedURL(url, sources.SOURCES, hint=refused[url])
                targets.append(_Target(url, self._source_for(url)))
            except sources.UnsupportedURL as exc:
                first = first or exc
                targets.append(_Target(url, None, exc.reason))
        return targets, first

    def _source_for(self, url: str):
        """The source that reads `url`, or raise `UnsupportedURL` saying why none can.

        A site adapter first; else the generic reader, unless the URL is not a
        web address, its site already has an adapter (which chose not to read
        this page), no classifier key is saved, or the saved site overrides
        cannot be read. A GenericSource is built per URL: it remembers what it
        decided on the page it is reading, and two URLs must not share that.
        """
        try:
            return sources.for_url(url)
        except sources.UnsupportedURL:
            pass
        supported = sources.SOURCES
        try:
            p = urlparse((url or "").strip())
            web = p.scheme.lower() in ("http", "https") and bool(p.hostname)
        except ValueError:
            web = False
        if not web:
            raise sources.UnsupportedURL(
                url, supported,
                hint="it is not a web address. Pass the http(s) URL of a page that lists "
                     "businesses for sale.",
                reason="not a web address — it must start with http:// or https://",
            )
        owner = sources.owner_of(url)
        if owner is not None:
            site = _site(url)
            owned = set(getattr(owner, "hosts", ()))
            pages = "; ".join(s.describes for s in supported
                              if owned & set(getattr(s, "hosts", ())))
            raise sources.UnsupportedURL(
                url, supported,
                hint=f"{site} is read by this app's own adapter, which sweeps only the pages "
                     f"below — not a single listing's page, or any other page on the site.",
                reason=f"on {site}, only these pages are swept: {pages}",
            )
        if self._classifier() is None:
            raise sources.UnsupportedURL(url, supported, hint=NEEDS_CLASSIFIER)
        try:
            overrides = self._overrides()
        except OverridesInvalid as exc:
            raise sources.UnsupportedURL(
                url, supported,
                hint="the Site overrides saved under Settings → Site overrides can't be read "
                     f"({exc}). Fix or clear them to read sites other than BizBuySell.",
            ) from None
        return GenericSource(url, self._typesafe, override_for(url, overrides))

    def _overrides(self) -> list[SiteOverride]:
        """The saved site overrides, parsed once per edit. Raises OverridesInvalid.

        Parsed here, when a generic sweep starts, and not when settings load: a
        document that no longer parses must refuse the URLs that would use it,
        never stop the app from booting (see `Settings.site_overrides_json`).
        """
        text = self._settings.load().site_overrides_json
        if text != self._overrides_text:
            parsed = parse_overrides(text)
            self._overrides_text, self._overrides_parsed = text, parsed
        return self._overrides_parsed

    def result(self, job_id: str) -> ScrapeResult | None:
        """The sweep as it stands. Never blocks, never waits, never launches anything.

        None means no such record. A record of another kind raises NotASweep:
        every task shares one id space, so this is a reachable mistake, and the
        one answer it must never give is an empty-looking sweep.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if not isinstance(job, SweepTask):
            raise NotASweep(
                f"job_id={job_id!r} is an {job.kind} task, not a listings sweep, so there "
                f"are no listings to collect. Its result is on the Tasks page; "
                f"get_scrape_listing_results only collects ids returned by scrape_listings."
            )
        return ScrapeResult.of(job)

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

    async def _run(self, job: SweepTask, targets: list[_Target],
                   plan: _TriagePlan | None = None) -> None:
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

        async def worker(i: int, target: _Target) -> dict:
            async with sem:
                return await self._sweep_url(job, i, target.url, target.source, prog,
                                             refusal=target.refusal)

        # The job's final state is kept here and written to the record only at
        # the very end (the `finally`). Until then the record says "working":
        # a poll that read "completed" while triage was still deciding rows
        # would hand an agent listings without their decisions, and the
        # progress summary (which only renders a working job) would freeze.
        status, error, summary = "failed", None, "Sweep failed."
        try:
            outcomes = await asyncio.gather(
                *(worker(i, target) for i, target in enumerate(targets))
            )
            listings, pages, ok, failures = self._merge(targets, outcomes)
            job.decisions = self._decisions(targets, outcomes)
            job.listings = listings
            job.pages_crawled = pages
            if ok == 0:
                # Every source failed — only now is the whole job a failure.
                error = self._failure_text(failures, total)
                summary = f"All {total} source(s) failed."
                _triage_not_run(job, "Nothing was triaged: every source failed.")
                return
            found = len(listings)
            store = plan.store if plan is not None else None
            upsert: UpsertResult | None = None
            if job.sync:
                # Dedupe+upsert the MERGED set ONCE, not per source. Under
                # sync the caller keeps only the NEWLY-inserted rows, each
                # carrying its store page id — the already-known ones stay in
                # `synced.existing` but drop out of `listings`.
                try:
                    store, upsert = await self._sync(job, listings, store)
                except Exception as exc:  # noqa: BLE001 — a save failure is the job's own
                    # The scrape SUCCEEDED; only the Notion write broke. This is
                    # distinct from a scrape failure (where sources failed) and
                    # must read that way. Drop the scraped listings on purpose:
                    # handing back scraped-but-unsaved rows looks like success
                    # and is worse than a clean failure. The message says the
                    # scrape worked, saving failed, nothing was saved — and
                    # carries the underlying store error verbatim.
                    logger.exception("saving sweep %s to Notion failed", job.id)
                    job.listings = []
                    job.synced = None
                    error = (
                        f"Scraped {found} listing(s) from {ok} source(s), but saving to "
                        f"your Notion database failed — nothing was saved. {exc}"
                    )
                    summary = (
                        f"Scraped {found} listing(s), but saving to Notion failed — "
                        f"nothing saved."
                    )
                    _triage_not_run(job, "Nothing was triaged: saving to Notion failed.")
                    return
                job.synced = SyncResult(
                    new=upsert.new, existing=upsert.existing, db_id=upsert.db_id,
                    skipped=upsert.skipped_names,
                )
                job.listings = upsert.new_listings
                # The rows exist in the store from here on, so the record says
                # so now, while the job is still working: a restart during
                # triage then reports what was saved (JobStore.adopt), not
                # "nothing was saved".
                self._jobs.save(job)
            status = "completed"
            if failures:
                error = self._failure_text(failures, total)
            if plan is not None and upsert is not None:
                # A triage that stops part-way (the classifier going down, a
                # detail page that will not load) leaves those rows blank for a
                # later sweep. The scrape and the save succeeded, so the job
                # still completes, with the reason alongside.
                await _TriagePhase(self, job, plan, store, upsert).run()
                note = _triage_note(job.triage)
                if note:
                    error = f"{error} {note}" if error else note
            summary = self._summarize(job, ok, total, failures, found)
        except asyncio.CancelledError:
            # Cancelled: the server is shutting down under this job. Whatever
            # is written now is what every later poll reads, so it must say
            # what actually happened — including rows saved before it stopped.
            logger.warning("sweep %s was cancelled", job.id)
            status = "failed"
            error, summary = interrupted(job)
            if job.triage is not None and not job.triage.ok and not job.triage.error:
                job.triage.error = "Interrupted before triage finished."
            raise
        except Exception as exc:  # noqa: BLE001 — the job must record its own failure
            logger.exception("sweep %s failed", job.id)
            status, error, summary = "failed", str(exc), "Sweep failed."
            _triage_not_run(job, f"Triage did not finish: {exc}")
        finally:
            job.status, job.error, job.summary = status, error, summary
            self._jobs.save(job)
            logger.info(
                "job %s -> %s (%d listings across %d source(s))",
                job.id, job.status, len(job.listings), total,
            )

    def _phase(self, job: SweepTask, text: str) -> None:
        """Say what a still-working sweep is doing now, where a poll and the
        dashboard can see it (the sources' own progress is over by then)."""
        job.summary = text
        self._jobs.save(job)

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
        for target, res in zip(targets, outcomes):
            url = target.url
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

    def _decisions(self, targets: list[_Target], outcomes: list[dict]) -> list[dict]:
        """How each URL was read, for the run's detail (see `SweepTask.decisions`)."""
        entries: list[dict] = []
        for target, res in zip(targets, outcomes):
            data = res.get("data") or {}
            entry: dict[str, Any] = {
                "url": target.url,
                "adapter": getattr(target.source, "name", None),
                "pages": list(data.get("pages") or []),
                "legibility": list(data.get("legibility") or []),
                "suggested_override": data.get("suggested_override"),
            }
            error = "blocked by the site" if res.get("blocked") else res.get("error")
            if error:
                entry["error"] = error
            entries.append(entry)
        return entries

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

    def _summarize(
        self, job: SweepTask, ok: int, total: int, failures: list[tuple[str, str]], found: int
    ) -> str:
        # `found` is how many DISTINCT listings the sweep saw, passed explicitly
        # because under sync `job.listings` has already been narrowed to just the
        # newly-inserted rows — the crawl breadth line should still report the
        # whole find, not only the new ones.
        pages = f"{job.pages_crawled} page{'s' if job.pages_crawled != 1 else ''}"
        parts = [f"{ok} of {total} source(s) swept · {found} listing(s) across {pages}"]
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
            if job.triage is not None:
                t = job.triage
                seg = f"triaged: {t.review} review, {t.reject} reject"
                if t.undecided:
                    seg += f", {t.undecided} left blank for a later sweep"
                parts.append(seg)
        if failures:
            parts.append(f"{len(failures)} source(s) failed")
        return " · ".join(parts)

    async def _sync(self, job: SweepTask, listings: list[Listing],
                    store: ListingStore | None = None) -> tuple[ListingStore, UpsertResult]:
        """Upsert the merged set; return the store used and what it did.

        The result's `new_listings` are the newly-inserted rows, each carrying
        the store page id it was written to — the caller swaps them in for
        `job.listings`, so an agent collecting a synced sweep gets exactly the
        rows this sweep added, ready to hand to `archive_page`. Its `untriaged`
        are the known rows whose Bot Triage is blank, for triage. `store` is the
        one triage's preflight already built, when there is one, so the save
        and the triage writes share its client.
        """
        settings = self._settings.load()
        if store is None:
            store = self._store_factory(settings)
        # The sweep always targets the configured database (job.db_id is set to
        # settings.notion_db_id at start), so the configured column map always
        # applies.
        column_map = settings.notion_column_map or None
        return store, await store.upsert_new(job.db_id, listings, column_map=column_map)

    def _evidence_dir(self, job: SweepTask, i: int) -> Path:
        """Where source `i`'s screenshots and snapshots land.

        Namespaced per source under the job's own directory so two URLs swept
        into one job never overwrite each other's captures. The job-level
        directory (CONFIG.evidence_dir / job.id) still holds all of them, so the
        /runs listing and ScrapeResult.evidence_dir are unchanged.
        """
        return CONFIG.evidence_dir / job.id / f"source-{i + 1:02d}"

    async def _sweep_url(self, job: SweepTask, i: int, url: str, source, prog: "_RunProgress",
                         refusal: str = "") -> dict:
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
                "error": refusal or "not a supported listings page",
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
        # An attempt that raised mid-page returns no data, but what the source
        # decided before it did is still the best clue to why.
        data = res.get("data")
        if not isinstance(data, dict):
            data = res["data"] = {}
        if "pages" not in data:
            data.update(_diagnostics(source))
        return res

    async def _sweep(self, job: SweepTask, i: int, url: str, source, prog: "_RunProgress") -> dict:
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

    def _classifier(self):
        """The TypeSafe client when a key is saved right now, else None."""
        if self._typesafe is None:
            return None
        return self._typesafe if self._settings.load().typesafe_configured() else None

    async def _sweep_once(self, inst, page, job: SweepTask, url: str, source, evidence: Path) -> dict:
        listings: list[Listing] = []
        kept: set[str] = set()   # URLs already in `listings`
        seen: set[str] = set()   # every listing URL any page showed, dropped ones included
        checks: list[dict] = []  # the legibility verdict of each page that had cards
        pages_done = 0

        def data() -> dict:
            return {"listings": listings, "pages_crawled": pages_done, "legibility": checks,
                    **_diagnostics(source)}

        async def failed(n: int, tag: str, error: str, retry: bool) -> dict:
            # A page that loaded, was not a block, and still cannot be used. The
            # evidence is what makes "cards don't read as listings" checkable
            # after the fact, so it is captured before anything is returned.
            await capture(page, evidence / f"page-{n:02d}-{tag}",
                          {"url": page.url, "reason": tag, "error": error, "page": n,
                           "proxy_ip": inst.proxy_ip})
            logger.warning("job %s: %s page %d failed (%s): %s", job.id, url, n, tag, error)
            return {"blocked": False, "error": error, "retry": retry, "data": data()}

        # One source object serves every attempt scrape_with_retry makes, so
        # whatever it learned on a failed attempt is forgotten before this one.
        begin = getattr(source, "begin", None)
        if begin is not None:
            begin()
        advance = getattr(source, "advance", None)

        for n in range(1, job.max_pages + 1):
            inst.touch()
            if n == 1 or advance is None:
                await page.goto(source.page_url(url, n), wait_until="domcontentloaded",
                                timeout=120_000)
            elif not await advance(page, n):
                break
            await page.wait_for_timeout(_WAIT_MS)
            await gesture(page)

            result = await source.cards(page)
            pages_done += 1
            # Evidence records page.url — where the browser actually is — not
            # page_url(url, n): after a click or a redirect they differ.
            if result.blocked or text_contains_blocker(result.title):
                await capture(page, evidence / f"page-{n:02d}-blocked",
                              {"url": page.url, "reason": "blocked", "proxy_ip": inst.proxy_ip})
                return {"blocked": True, "error": None, "data": data()}
            if result.error:
                return await failed(n, _evidence_tag(result.error), result.error,
                                    result.retry)

            page_listings = result.listings
            if page_listings:
                verdict = await legibility.check(page_listings, page=n,
                                                 classifier=self._classifier())
                checks.append(verdict.record(n))
                if not verdict.ok:
                    # Illegible cards are not a block: the same page from a new
                    # exit IP reads the same way, so the failure is final.
                    return await failed(n, "illegible", verdict.reason, retry=False)
                page_listings = verdict.listings

            # Paging stops on cards this crawl has already seen, not on cards the
            # store already has: a feed whose first two pages are all known
            # listings still has new ones on page three, and dedupe is a separate
            # question answered at the end. "Seen" counts every card the page
            # showed — including ones the source or the legibility check dropped
            # — so a page of sold listings is not mistaken for the end.
            on_page = [l.url for l in result.listings] + list(result.seen_urls or ())
            fresh = 0
            for href in on_page:
                if href and href not in seen:
                    seen.add(href)
                    fresh += 1
            for listing in page_listings:
                if listing.url in kept:
                    continue
                kept.add(listing.url)
                listings.append(listing)
            if fresh == 0 and n > 1:
                break

        await capture(page, evidence / "final",
                      {"url": url, "page_url": page.url, "reason": "success",
                       "found": len(listings), "pages_crawled": pages_done,
                       "legibility": checks, "proxy_ip": inst.proxy_ip})
        return {"blocked": False, "error": None, "data": data()}


def _site(url: str) -> str:
    """A URL's host without `www.` — how a site is named to a person."""
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _diagnostics(source) -> dict:
    """What a source decided on its last attempt: `pages` and `suggested_override`.

    Only the generic reader decides anything worth reporting (a site adapter's
    choices are its code), so a source without `decisions` reports nothing.
    Never raises: a diagnostic that fails must not fail the sweep it describes.
    """
    decisions = getattr(source, "decisions", None)
    if decisions is None:
        return {}
    out: dict[str, Any] = {"pages": list(decisions), "suggested_override": None}
    suggest = getattr(source, "suggested_override", None)
    if callable(suggest):
        try:
            out["suggested_override"] = suggest()
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("could not build a suggested override for %s",
                             getattr(source, "url", "?"))
    return out


def _evidence_tag(reason: str, limit: int = 50) -> str:
    """A failure reason as an evidence directory name, cut at a word boundary.

    The directory is how a person finds the capture of a failed page in the
    run's evidence, so it says what went wrong in the source's own words
    ("page-01-no-list-of-businesses-for-sale-on-this-page") rather than a code.
    """
    words = re.findall(r"[a-z0-9]+", reason.lower())
    tag = ""
    for word in words:
        longer = f"{tag}-{word}" if tag else word
        if len(longer) > limit:
            break
        tag = longer
    return tag or (words[0][:limit] if words else "error")


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

    def __init__(self, jobs: JobStore, job: SweepTask, total: int) -> None:
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


def _triage_not_run(job: SweepTask, reason: str) -> None:
    """Record why a sweep asked to triage decided nothing (first reason wins)."""
    if job.triage is not None and not job.triage.error:
        job.triage.error = reason


def _triage_note(triage: TriageSummary | None) -> str:
    """The sentence a completed job's error carries when triage left rows blank."""
    if triage is None or triage.ok:
        return ""
    if triage.error:
        return (f"Triage stopped before deciding every row: {triage.error} Rows without a "
                f"decision stay blank and are triaged on a later sweep.")
    n = triage.undecided or len(triage.failures)
    return (f"Triage couldn't decide {n} row{'' if n == 1 else 's'} (see triage.failures); "
            f"they stay blank and are triaged on a later sweep.")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class _TriagePhase:
    """One sweep's triage: decide every row, write each decision, archive the REVIEWs.

    The rows are the ones this sweep inserted plus the known rows it saw whose
    Bot Triage is blank (`UpsertResult.untriaged`) — a row holding a decision,
    anyone's, is never judged again. Two stages:

    1. **The card**, for every row at once (the TypeSafe client's own ceiling
       bounds the fan-out). REJECT is written straight away.
    2. **The detail page**, for each card REVIEW, read through the archive's
       gate and pooled identities. Then, in this order:
       - the page could not be read (blocked, failed to load) → nothing is
         written and the row is reported as a failure; its Bot Triage stays
         blank, so a later sweep tries again;
       - the guard — asked on its own, never bundled with the triage question:
         bundled with the card and page text its score on real pages fell from
         0.95 to 0.55–0.68 — says the page is not the listing's real content
         (a login or NDA wall, a removed listing, an error page) → REVIEW is
         written as decided on the card, with nothing archived, so a site that
         always gates its detail pages is not retried forever;
       - otherwise the question is asked again on the card and the page. REVIEW
         → the page is appended to the row (idempotently) and THEN REVIEW is
         written, so every REVIEW with a readable page has its Source Content;
         REJECT → REJECT is written and nothing is archived.

    A TypeSafeError (after the client's own retries) stops the phase: no more
    questions are asked and the rows not yet decided stay blank. A failed
    Notion write fails that row only. Neither is raised — the scrape and the
    save succeeded, and the job says so (`_triage_note`). Only a cancellation
    propagates, after the record has been brought up to date.
    """

    def __init__(self, service: ScrapeService, job: SweepTask, plan: _TriagePlan,
                 store: ListingStore, upsert: UpsertResult) -> None:
        self._svc = service
        self._job = job
        self._plan = plan
        self._store = store
        self._target = plan.target
        self._triager = Triager(service._typesafe, plan.prompt)
        if job.triage is None:
            job.triage = TriageSummary(criteria_version=plan.version)
        self._summary = job.triage
        # (listing carrying its row id, is it backlog), each row once.
        self._rows: list[tuple[Listing, bool]] = []
        seen: set[str] = set()
        for listing, backlog in ([(l, False) for l in upsert.new_listings]
                                 + [(l, True) for l in upsert.untriaged]):
            row_id = listing.synced_row_id
            if row_id and row_id not in seen:
                seen.add(row_id)
                self._rows.append((listing, backlog))
        self._decided: dict[str, TriageDecision] = {}
        self._failed: dict[str, TriageFailure] = {}
        self._stopped: str | None = None
        # What each row went through, for the run's evidence (triage.json).
        self._records: dict[str, dict[str, Any]] = {}
        self._evidence = CONFIG.evidence_dir / job.id

    async def run(self) -> None:
        try:
            if not self._rows:
                return
            if self._target is None and not await self._prepare():
                return
            new = sum(1 for _, backlog in self._rows if not backlog)
            old = len(self._rows) - new
            what = _plural(new, "new listing") if new else ""
            if old:
                earlier = _plural(old, "earlier row")
                what = f"{what} and {earlier}" if what else earlier
            self._svc._phase(self._job, f"Triaging {what}…")

            cards = await asyncio.gather(*(self._card(listing) for listing, _ in self._rows))
            reviews = [(i, listing, card)
                       for i, ((listing, _), card) in enumerate(zip(self._rows, cards), 1)
                       if card is not None and card.decision == REVIEW]
            if reviews and not self._stopped:
                await self._details(reviews)
        finally:
            self._finish()

    async def _prepare(self) -> bool:
        """Resolve the triage target when `submit`'s preflight did not."""
        settings = self._svc._settings.load()
        try:
            self._target = await self._store.prepare_triage(
                self._job.db_id, settings.notion_column_map or None,
            )
        except Exception as exc:  # noqa: BLE001 — TriageUnavailable or a store failure
            self._stopped = f"couldn't find where to record decisions: {exc}"
            return False
        return True

    # -- the two stages --

    async def _card(self, listing: Listing) -> TriageDecision | None:
        if self._stopped:
            return None
        try:
            decision = await self._triager.card(listing)
        except TypeSafeError as exc:
            self._stop(exc)
            return None
        except Exception as exc:  # noqa: BLE001 — one row's trouble is that row's
            logger.exception("triage of %s failed", listing.url)
            self._fail(listing, f"The classifier's answer could not be used: {exc}")
            return None
        self._note(listing, card=decision.record())
        if decision.decision == REJECT:
            await self._write(listing, decision)
        return decision

    async def _details(self, reviews: list[tuple[int, Listing, TriageDecision]]) -> None:
        total = len(reviews)
        done = 0
        pages = _plural(total, "detail page")
        self._svc._phase(self._job, f"Reading {pages}…")
        # The archive's gate bounds the browsers; this bounds how many rows are
        # between "read" and "written" at once, so progress reads in order.
        sem = asyncio.Semaphore(max(1, self._svc._settings.load().task_budget))

        async def one(i: int, listing: Listing, card: TriageDecision) -> None:
            nonlocal done
            async with sem:
                await self._detail(i, listing, card)
            done += 1
            if done < total:
                self._svc._phase(self._job, f"Reading {pages}… ({done} of {total} done)")

        await asyncio.gather(*(one(i, listing, card) for i, listing, card in reviews))

    async def _detail(self, i: int, listing: Listing, card: TriageDecision) -> None:
        if self._stopped:
            return
        evidence = self._evidence / f"detail-{i:02d}"
        try:
            read = await self._svc._archive.read(listing.url, evidence,
                                                 owner=f"job:{self._job.id}")
        except Exception as exc:  # noqa: BLE001 — a launch failure is this row's
            logger.warning("reading the detail page %s failed: %s", listing.url, exc)
            self._fail(listing, f"Couldn't read the detail page: {exc}")
            return
        if not read.ok:
            self._fail(listing, f"Couldn't read the detail page: {read.failure}")
            return
        if self._stopped:
            return
        try:
            p_real = await archive_guard(self._svc._typesafe, read.markdown)
        except TypeSafeError as exc:
            self._stop(exc)
            return
        self._note(listing, guard=round(p_real, 4))
        if p_real < GUARD_THRESHOLD:
            # Not the listing's content (a wall, a removed listing, an error
            # page): decided on the card, and nothing is archived.
            await self._write(listing, card.on_card_only(p_real))
            return
        try:
            decision = await self._triager.detail(listing, read.markdown)
        except TypeSafeError as exc:
            self._stop(exc)
            return
        self._note(listing, detail=decision.record())
        if decision.decision == REVIEW:
            client = getattr(self._target, "client", None)
            try:
                await self._svc._archive.append(client, listing.synced_row_id,
                                                read.markdown, listing.url)
            except Exception as exc:  # noqa: BLE001 — REVIEW waits for its archive
                logger.warning("archiving %s into row %s failed: %s",
                               listing.url, listing.synced_row_id, exc)
                self._fail(listing, "Archiving the detail page into the row failed, so REVIEW "
                                    f"was not written: {exc}")
                return
        await self._write(listing, decision)

    # -- bookkeeping --

    async def _write(self, listing: Listing, decision: TriageDecision) -> None:
        row_id = listing.synced_row_id
        try:
            await self._store.write_triage(
                self._target, row_id, decision.decision, decision.reason,
                datetime.now(timezone.utc), decision.criteria_version,
            )
        except Exception as exc:  # noqa: BLE001 — a decision not saved is this row's failure
            logger.warning("writing triage for row %s failed: %s", row_id, exc)
            self._fail(listing, f"Saving the decision ({decision.decision}) to Notion "
                                f"failed: {exc}")
            return
        self._decided[row_id] = decision
        self._note(listing, final=decision.record())

    def _fail(self, listing: Listing, error: str) -> None:
        row_id = listing.synced_row_id
        self._failed[row_id] = TriageFailure(row_id=row_id, url=listing.url, error=error)
        self._note(listing, error=error)

    def _stop(self, exc: Exception) -> None:
        if self._stopped is None:
            logger.warning("triage for sweep %s stopped: %s", self._job.id, exc)
            self._stopped = str(exc)

    def _note(self, listing: Listing, **fields: Any) -> None:
        record = self._records.setdefault(listing.synced_row_id, {
            "row_id": listing.synced_row_id, "url": listing.url, "title": listing.title,
        })
        record.update(fields)

    def _finish(self) -> None:
        """Bring the job's record up to date with what was decided. Runs on
        every exit, a cancellation included."""
        summary = self._summary
        summary.criteria_version = self._plan.version
        decisions = [d.decision for d in self._decided.values()]
        summary.review = decisions.count(REVIEW)
        summary.reject = decisions.count(REJECT)
        summary.undecided = len(self._rows) - len(self._decided)
        summary.failures = list(self._failed.values())
        summary.backlog = [
            TriagedRow(row_id=listing.synced_row_id, url=listing.url,
                       decision=getattr(self._decided.get(listing.synced_row_id), "decision", ""))
            for listing, backlog in self._rows if backlog
        ]
        summary.error = self._stopped or summary.error
        summary.ok = summary.undecided == 0 and summary.error is None
        # The new rows are the job's listings; each carries what was written to
        # it. (Backlog rows are not listings — they are reported above.)
        listings = []
        for listing in self._job.listings:
            decision = self._decided.get(listing.synced_row_id)
            if decision is not None:
                listing = listing.model_copy(update={
                    "bot_triage": decision.decision, "triage_p_review": decision.p_review,
                })
            listings.append(listing)
        self._job.listings = listings
        if self._records:
            _write_json(self._evidence / "triage.json", {
                "criteria_version": self._plan.version,
                "stopped": self._stopped,
                "rows": list(self._records.values()),
            })


def _write_json(path: Path, data: dict) -> None:
    """Keep a record with the run's evidence; never fails the run over it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("could not write %s", path)


def _default_store(settings) -> ListingStore:
    """Resolved here, and only on the sync path, so importing this module never
    imports Notion."""
    from ..stores.notion import NotionStore

    return NotionStore(settings.notion_api_token)
