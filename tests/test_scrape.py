"""The scrape service's contract, with the browser stubbed out.

The sweep itself (a real browser, a real proxy, a real BizBuySell page) is
verified by scripts/verify_scrape.py against the live site — it cannot be
faked usefully. What is worth pinning here is everything around it: that
starting returns instantly and tells the model how to collect, that sync=false
never so much as constructs a store, and that a failure is recorded rather than
raised into the void of a background task.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import Listing
from app.services.jobs import JobStore
from app.services.profiles import ProfileStore
from app.services.scrape import (
    _SCRAPING_SUMMARY,
    _WAITING_SUMMARY,
    NotionNotConfigured,
    ScrapeService,
)
from app.services.settings import SettingsService
from app.services.task_profiles import TaskProfilePool
from app.sources import UnsupportedURL
from app.stores.base import UpsertResult

SERP = "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"

CARDS = [
    Listing(
        listing_id="2485121",
        url="https://www.bizbuysell.com/business-opportunity/foo/2485121/",
        normalized_url="bizbuysell.com/business-opportunity/foo/2485121",
        title="A Business",
        asking_price="$1,258,000",
        excerpt="**A Business** — San Francisco, CA",
        source="bizbuysell_serp",
    )
]


class FakeStore:
    """Records what it was asked to do. Its existence in a test is the point:
    if sync=false ever constructs one, `built` proves it."""

    built = 0

    def __init__(self, settings=None):
        FakeStore.built += 1
        self.upserts: list[tuple[str, list[Listing]]] = []
        self.column_maps: list = []

    async def upsert_new(self, db_id, listings, column_map=None):
        self.upserts.append((db_id, listings))
        self.column_maps.append(column_map)
        return UpsertResult(new=len(listings), existing=0, db_id=db_id)


@pytest.fixture
def settings(tmp_path):
    return SettingsService(tmp_path / "settings.json", tmp_path / ".dek")


@pytest.fixture
def jobs(tmp_path):
    return JobStore(tmp_path / "jobs", boot_id="boot-1")


@pytest.fixture(autouse=True)
def reset_store_counter():
    FakeStore.built = 0
    yield


def service(settings, jobs, store=None, sweep=None):
    svc = ScrapeService(instances=None, jobs=jobs, settings=settings,
                        store_factory=store or FakeStore)
    svc._sweep = sweep or (lambda job, source: _ok(job))
    return svc


async def _ok(job):
    return {"blocked": False, "error": None, "data": {"listings": list(CARDS), "pages_crawled": 1}}


async def _drain(svc):
    """Let the background sweep finish."""
    for _ in range(200):
        if svc.in_flight == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("sweep never finished")


class TestStarting:
    def test_an_unsupported_url_never_creates_a_job(self, settings, jobs):
        """A job id for a URL we cannot read would be a promise of a result that
        can never come."""
        with pytest.raises(UnsupportedURL):
            service(settings, jobs).start("https://abc.xyz/investor/")
        assert jobs.all() == []

    def test_sync_without_notion_fails_before_any_browsing(self, settings, jobs):
        """Told now, not after a two-minute sweep that then has nowhere to go."""
        with pytest.raises(NotionNotConfigured) as exc:
            service(settings, jobs).start(SERP, sync=True)
        assert "sync=false" in str(exc.value), "name the way out"
        assert jobs.all() == []

    @pytest.mark.asyncio
    async def test_starting_returns_working_and_says_how_to_collect(self, settings, jobs):
        svc = service(settings, jobs)
        job = svc.start(SERP)
        assert job.status == "working"
        assert job.listings == [], "the listings are not in this response"
        assert f"job_id={job.id}" in job.summary
        assert "get_scrape_listing_results" in job.summary
        await _drain(svc)


class TestSyncFalse:
    @pytest.mark.asyncio
    async def test_nothing_is_written_and_no_store_is_built(self, settings, jobs):
        """The plan's line: sync=false needs no Notion. Not a Notion code path
        guarded by a flag — the absence of one, which is what lets someone who
        has never configured Notion still use this."""
        svc = service(settings, jobs)
        job = svc.start(SERP, sync=False)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed"
        assert len(result.listings) == 1
        assert result.synced is None, "null means 'never asked to', not 'wrote nothing'"
        assert FakeStore.built == 0, "sync=false must not construct a store at all"
        assert "Nothing was saved" in result.summary


class TestSyncTrue:
    @pytest.mark.asyncio
    async def test_listings_are_upserted_into_the_configured_database(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-configured")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)

        job = svc.start(SERP, sync=True)
        await _drain(svc)

        assert store.upserts == [("db-configured", CARDS)]
        result = svc.result(job.id)
        assert result.synced.new == 1
        assert result.synced.db_id == "db-configured"

    @pytest.mark.asyncio
    async def test_db_id_overrides_the_configured_database(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-configured")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)

        svc.start(SERP, sync=True, db_id="db-override")
        await _drain(svc)

        assert store.upserts[0][0] == "db-override"

    @pytest.mark.asyncio
    async def test_the_scraper_hands_the_store_verbatim_money(self, settings, jobs):
        """The boundary, end to end: the scraper reports what the card said and
        the store decides what it means."""
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)
        svc.start(SERP, sync=True)
        await _drain(svc)

        assert store.upserts[0][1][0].asking_price == "$1,258,000"

    @pytest.mark.asyncio
    async def test_the_configured_map_is_passed_for_the_configured_database(self, settings, jobs):
        cmap = {"listing_title": "Deal", "url": "Link"}
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1", notion_column_map=cmap)
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)
        svc.start(SERP, sync=True)
        await _drain(svc)

        assert store.column_maps[0] == cmap

    @pytest.mark.asyncio
    async def test_a_different_db_id_falls_back_to_identity_no_map(self, settings, jobs):
        """The stored map belongs to the configured database. A sweep aimed at a
        different one must not be judged against columns it never named."""
        settings.update(
            notion_api_token="ntn_x", notion_db_id="db-configured",
            notion_column_map={"listing_title": "Deal"},
        )
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)
        svc.start(SERP, sync=True, db_id="db-other")
        await _drain(svc)

        assert store.column_maps[0] is None


class TestFailure:
    @pytest.mark.asyncio
    async def test_a_block_is_recorded_as_a_failure_with_advice(self, settings, jobs):
        async def blocked(job, source):
            return {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}

        svc = service(settings, jobs, sweep=blocked)
        job = svc.start(SERP)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert "anti-bot" in result.error
        assert "try again" in result.error.lower()

    @pytest.mark.asyncio
    async def test_an_exception_lands_on_the_job_not_in_a_lost_task(self, settings, jobs):
        """A background task that raises into nothing leaves the job saying
        "working" forever."""
        async def boom(job, source):
            raise RuntimeError("the wheels came off")

        svc = service(settings, jobs, sweep=boom)
        job = svc.start(SERP)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert "the wheels came off" in result.error


class TestTaskProfiles:
    """The sweep leases a pooled task-N identity instead of minting serp-<path>,
    and returns it on every exit path."""

    def _pooled(self, settings, jobs, monkeypatch, tmp_path, retry):
        """A ScrapeService whose real _sweep runs against a real pool, with the
        browser launch (scrape_with_retry) replaced by `retry`."""
        monkeypatch.setattr("app.services.scrape.scrape_with_retry", retry)
        profiles = ProfileStore(tmp_path / "profiles")
        pool = TaskProfilePool(profiles, settings)
        # instances is a dummy: with a pool injected, __init__ never touches it,
        # and the patched scrape_with_retry never uses it.
        svc = ScrapeService(instances=object(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=pool)
        return svc, pool, profiles

    @pytest.mark.asyncio
    async def test_the_sweep_leases_a_task_profile_and_never_creates_serp(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        captured: dict = {}

        async def retry(instances, *, profile, owner, **kw):
            captured["profile"], captured["owner"] = profile, owner
            return {"blocked": False, "error": None,
                    "data": {"listings": list(CARDS), "pages_crawled": 1}}

        svc, pool, profiles = self._pooled(settings, jobs, monkeypatch, tmp_path, retry)
        job = svc.start(SERP)
        await _drain(svc)

        assert captured["profile"] == "task-1", "launched on a pooled identity"
        assert captured["owner"] == f"job:{job.id}", "owner tag preserved"
        names = [p.name for p in profiles.all()]
        assert "task-1" in names
        assert not any(n.startswith("serp-") for n in names), "no per-URL profile minted"
        assert pool.leased_by(job.id) == [], "lease returned after a clean sweep"

    @pytest.mark.asyncio
    async def test_a_crashed_sweep_releases_its_lease(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        """The finally path in _run: even a launch that explodes must not leak the
        lease and pin the profile as busy forever."""
        async def retry(instances, *, profile, **kw):
            raise RuntimeError("launch exploded")

        svc, pool, _ = self._pooled(settings, jobs, monkeypatch, tmp_path, retry)
        job = svc.start(SERP)
        await _drain(svc)

        assert svc.result(job.id).status == "failed"
        assert pool.leased_by(job.id) == [], "lease freed despite the crash"
        assert pool.acquire("next") == "task-1", "the freed profile is reused, not leaked"

    @pytest.mark.asyncio
    async def test_concurrent_sweeps_never_mint_more_than_task_budget_profiles(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        """The bound that b9f972c claimed but did not hold: start() spawns an
        unbounded task per sweep, so without the admission gate N concurrent
        sweeps each acquire+mint before any blocks on a slot. With task_budget=3,
        ten simultaneous sweeps must mint AT MOST 3 durable profiles."""
        settings.update(max_instances=4, interactive_reserve=1)
        assert settings.load().task_budget == 3

        release = asyncio.Event()
        in_retry = 0

        async def retry(instances, *, profile, **kw):
            # A sweep only reaches here once it is past the gate AND has leased a
            # profile. Park it so all admitted sweeps are in flight at once.
            nonlocal in_retry
            in_retry += 1
            await release.wait()
            return {"blocked": False, "error": None,
                    "data": {"listings": list(CARDS), "pages_crawled": 1}}

        svc, pool, profiles = self._pooled(settings, jobs, monkeypatch, tmp_path, retry)
        for _ in range(10):
            svc.start(SERP)

        # Let the admitted sweeps reach the (blocked) launch and settle.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if in_retry >= 3:
                break
        await asyncio.sleep(0.05)  # give any wrongly-admitted extras time to mint

        pooled = [p.name for p in profiles.all() if p.name.startswith("task-")]
        assert in_retry == 3, "only task_budget sweeps run past the gate at once"
        assert len(pooled) <= 3, f"minted {pooled}, expected at most 3"

        # Drain: the remaining seven reuse the three profiles, never minting more.
        release.set()
        await _drain(svc)
        final = sorted(p.name for p in profiles.all() if p.name.startswith("task-"))
        assert final == ["task-1", "task-2", "task-3"], "ten sweeps, three profiles"


class TestWaitingSummary:
    """A sweep blocked behind a full pool must say so. The status stays
    'working' (consumers unchanged), but the summary distinguishes 'queued' from
    'scraping' — otherwise a full pool looks identical to a stuck sweep."""

    def _pooled(self, settings, jobs, monkeypatch, tmp_path, retry):
        monkeypatch.setattr("app.services.scrape.scrape_with_retry", retry)
        profiles = ProfileStore(tmp_path / "profiles")
        pool = TaskProfilePool(profiles, settings)
        svc = ScrapeService(instances=object(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=pool)
        return svc

    @pytest.mark.asyncio
    async def test_queued_sweep_shows_waiting_and_running_sweep_shows_scraping(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        # task_budget = 1: only one sweep past the gate at a time, so a second
        # start() queues behind it.
        settings.update(max_instances=2, interactive_reserve=1)
        assert settings.load().task_budget == 1

        release = asyncio.Event()
        launched = asyncio.Event()

        class _Inst:
            id = "inst"
            proxy_ip = None

        async def retry(instances, *, profile, on_launch=None, **kw):
            # A browser is in hand — clear the "waiting" summary, then park so the
            # gate stays occupied while we inspect the queued sweep.
            on_launch(_Inst())
            launched.set()
            await release.wait()
            return {"blocked": False, "error": None,
                    "data": {"listings": list(CARDS), "pages_crawled": 1}}

        svc = self._pooled(settings, jobs, monkeypatch, tmp_path, retry)
        job1 = svc.start(SERP)
        await launched.wait()          # job1 is past the gate and scraping
        job2 = svc.start(SERP)         # job2 must queue at the gate
        await asyncio.sleep(0.05)      # let job2 set its summary and block

        assert svc.result(job1.id).summary == _SCRAPING_SUMMARY, "running sweep: scraping"
        assert svc.result(job2.id).summary == _WAITING_SUMMARY, "queued sweep: waiting"
        assert svc.result(job2.id).status == "working", "still working, just queued"

        release.set()
        await _drain(svc)
        # Once it actually runs and completes, the waiting text is gone.
        assert svc.result(job2.id).status == "completed"
        assert svc.result(job2.id).summary.startswith("Found")
        assert svc.result(job1.id).summary.startswith("Found")


class TestCollecting:
    def test_an_unknown_job_is_none(self, settings, jobs):
        assert service(settings, jobs).result("nosuchjob") is None

    @pytest.mark.asyncio
    async def test_collecting_never_waits_for_the_sweep(self, settings, jobs):
        """Poll semantics: it answers with whatever is true right now."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(job, source):
            started.set()
            await release.wait()
            return await _ok(job)

        svc = service(settings, jobs, sweep=slow)
        job = svc.start(SERP)
        await started.wait()

        result = await asyncio.wait_for(asyncio.to_thread(svc.result, job.id), timeout=1)
        assert result.status == "working"
        assert result.listings == []

        release.set()
        await _drain(svc)
        assert svc.result(job.id).status == "completed"
