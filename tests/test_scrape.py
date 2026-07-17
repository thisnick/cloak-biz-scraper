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
from app.services.scrape import NotionNotConfigured, ScrapeService
from app.services.settings import SettingsService
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

    async def upsert_new(self, db_id, listings):
        self.upserts.append((db_id, listings))
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
