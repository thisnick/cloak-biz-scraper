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

from app.models import Listing, SweepTask
from app.services.jobs import JobStore
from app.services.profiles import ProfileStore
from app.services.scrape import (
    _SCRAPING_SUMMARY,
    _WAITING_SUMMARY,
    NotASweep,
    NotionNotConfigured,
    ScrapeService,
    describe,
)
from app.services.settings import SettingsService
from app.services.task_profiles import TaskProfilePool
from app.sources import UnsupportedURL
from app.stores.base import UpsertResult

SERP = "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"
SERP2 = "https://www.bizbuysell.com/california/san-francisco-bay-area-businesses-for-sale/"
BROKER = "https://www.bizbuysell.com/business-broker/jane-doe/acme-advisors/41243/"
# Right site, wrong job: a BizBuySell listing's own page.
DETAIL = "https://www.bizbuysell.com/business-opportunity/premier-restoration/2515728/"
# Sites with no adapter of their own.
WC = "https://www.websiteclosers.com/businesses-for-sale/"
DEALONOMY = "https://www.dealonomy.com/s"
KEY_HINT = "add an OpenRouter key under Settings → Decision API"


def _listing(listing_id: str, source: str = "bizbuysell_serp") -> Listing:
    return Listing(
        listing_id=listing_id,
        url=f"https://www.bizbuysell.com/business-opportunity/foo/{listing_id}/",
        normalized_url=f"bizbuysell.com/business-opportunity/foo/{listing_id}",
        title=f"Business {listing_id}",
        asking_price="$1,258,000",
        source=source,
    )


CARDS = [_listing("2485121")]


class FakeStore:
    """Records what it was asked to do. Its existence in a test is the point:
    if sync=false ever constructs one, `built` proves it.

    Models the real store's new/existing split so the sweep's sync semantics can
    be exercised: any listing whose id is in `existing_ids` is counted as already
    present and left OUT of `new_listings`; every other one comes back inserted,
    stamped with a `row-<id>` synced_row_id — the neutral row id the real Notion
    store reads off the /pages response.
    """

    built = 0

    def __init__(self, settings=None, existing_ids=None):
        FakeStore.built += 1
        self.upserts: list[tuple[str, list[Listing]]] = []
        self.column_maps: list = []
        self._existing = set(existing_ids or ())

    async def upsert_new(self, db_id, listings, column_map=None):
        self.upserts.append((db_id, listings))
        self.column_maps.append(column_map)
        new_listings: list[Listing] = []
        existing = 0
        for listing in listings:
            if listing.listing_id in self._existing:
                existing += 1
                continue
            new_listings.append(
                listing.model_copy(update={"synced_row_id": f"row-{listing.listing_id}"})
            )
        return UpsertResult(
            new=len(new_listings), existing=existing, db_id=db_id, new_listings=new_listings,
        )


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
    # `_sweep` is the per-source seam: (job, i, url, source, prog) -> result dict.
    # Stubbing it bypasses the pool and the browser while still exercising the
    # fan-out, admission gate and merge in _run/_sweep_url.
    svc._sweep = sweep or _ok
    return svc


async def _ok(job, i=0, url=SERP, source=None, prog=None):
    return {"blocked": False, "error": None, "data": {"listings": list(CARDS), "pages_crawled": 1}}


async def _drain(svc):
    """Let the background sweep finish."""
    for _ in range(200):
        if svc.in_flight == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("sweep never finished")


class TestStarting:
    @pytest.mark.parametrize("url", ["", "not a url", "ftp://example.com/listings/", DETAIL,
                                     "https://abc.xyz/investor/"])
    def test_a_url_nothing_can_read_never_creates_a_job(self, settings, jobs, url):
        """A job id for a URL we cannot read would be a promise of a result that
        can never come. (The last one is readable with a classifier key; there
        is none saved here.)"""
        with pytest.raises(UnsupportedURL):
            service(settings, jobs).start([url])
        assert jobs.all() == []

    def test_sync_without_notion_fails_before_any_browsing(self, settings, jobs):
        """Told now, not after a two-minute sweep that then has nowhere to go."""
        with pytest.raises(NotionNotConfigured) as exc:
            service(settings, jobs).start([SERP], sync=True)
        assert "sync=false" in str(exc.value), "name the way out"
        assert jobs.all() == []

    @pytest.mark.asyncio
    async def test_starting_returns_working_and_says_how_to_collect(self, settings, jobs):
        svc = service(settings, jobs)
        job = svc.start([SERP])
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
        job = svc.start([SERP], sync=False)
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

        job = svc.start([SERP], sync=True)
        await _drain(svc)

        assert store.upserts == [("db-configured", CARDS)]
        result = svc.result(job.id)
        assert result.synced.new == 1
        assert result.synced.db_id == "db-configured"

    @pytest.mark.asyncio
    async def test_the_configured_database_cannot_be_overridden_per_call(self, settings, jobs):
        """There is no db_id override any more: a sweep always syncs to the
        database configured under Settings, so passing one is rejected outright
        rather than quietly aiming the sweep somewhere else."""
        settings.update(notion_api_token="ntn_x", notion_db_id="db-configured")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)

        with pytest.raises(TypeError):
            svc.start([SERP], sync=True, db_id="db-override")

        job = svc.start([SERP], sync=True)
        await _drain(svc)
        assert store.upserts[0][0] == "db-configured", "sync always targets the configured db"
        assert svc.result(job.id).synced.db_id == "db-configured"

    def test_the_rest_request_exposes_no_db_id_field(self):
        """The REST façade dropped the override too: db_id is not a request field,
        and a stray one in the body is ignored rather than steering the sweep."""
        from app.routes.api import ScrapeRequest

        assert "db_id" not in ScrapeRequest.model_fields
        req = ScrapeRequest.model_validate(
            {"urls": [SERP], "sync": True, "db_id": "db-override"}
        )
        assert not hasattr(req, "db_id")

    @pytest.mark.asyncio
    async def test_the_scraper_hands_the_store_verbatim_money(self, settings, jobs):
        """The boundary, end to end: the scraper reports what the card said and
        the store decides what it means."""
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)
        svc.start([SERP], sync=True)
        await _drain(svc)

        assert store.upserts[0][1][0].asking_price == "$1,258,000"

    @pytest.mark.asyncio
    async def test_the_configured_map_is_passed_for_the_configured_database(self, settings, jobs):
        cmap = {"listing_title": "Deal", "url": "Link"}
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1", notion_column_map=cmap)
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)
        svc.start([SERP], sync=True)
        await _drain(svc)

        assert store.column_maps[0] == cmap



def _sweep_returning(listings):
    """A `_sweep` stub that yields a fixed set of listings from a single URL."""
    async def sweep(job, i, url, source, prog):
        return {"blocked": False, "error": None,
                "data": {"listings": list(listings), "pages_crawled": 1}}
    return sweep


class TestSyncReturnsNewWithRowIds:
    """sync=true narrows the collected `listings` to just the rows this sweep
    inserted, each stamped with the store row id — so an agent can archive_page
    the fresh rows straight off the result. Already-known rows stay counted in
    `synced.existing` but drop out of `listings`. sync=false is unchanged."""

    @pytest.mark.asyncio
    async def test_sync_true_returns_only_new_listings_each_with_a_row_id(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore(existing_ids={"known"})
        found = [_listing("known"), _listing("fresh-a"), _listing("fresh-b")]
        svc = service(settings, jobs, store=lambda s: store, sweep=_sweep_returning(found))
        job = svc.start([SERP], sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        assert sorted(l.listing_id for l in result.listings) == ["fresh-a", "fresh-b"], (
            "only the newly-inserted rows are returned; the known one is omitted"
        )
        assert all(l.synced_row_id for l in result.listings), "each new listing carries a row id"
        assert {l.synced_row_id for l in result.listings} == {"row-fresh-a", "row-fresh-b"}
        assert result.synced.new == 2
        assert result.synced.existing == 1, "the known row is counted, not returned"

    @pytest.mark.asyncio
    async def test_sync_false_returns_all_found_with_empty_row_id(self, settings, jobs):
        found = [_listing("a"), _listing("b")]
        svc = service(settings, jobs, sweep=_sweep_returning(found))
        job = svc.start([SERP], sync=False)
        await _drain(svc)

        result = svc.result(job.id)
        assert sorted(l.listing_id for l in result.listings) == ["a", "b"], "all found"
        assert all(l.synced_row_id == "" for l in result.listings), "no store, so no row id"
        assert result.synced is None
        assert FakeStore.built == 0, "sync=false still builds no store"

    @pytest.mark.asyncio
    async def test_a_resweep_with_nothing_new_returns_empty_listings(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore(existing_ids={"a", "b"})
        found = [_listing("a"), _listing("b")]
        svc = service(settings, jobs, store=lambda s: store, sweep=_sweep_returning(found))
        job = svc.start([SERP], sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.listings == [], "nothing new to hand back"
        assert result.synced.new == 0
        assert result.synced.existing == 2
        # The crawl-breadth line still reports the whole find, not the zero new.
        assert "2 listing(s)" in result.summary

    @pytest.mark.asyncio
    async def test_multi_url_sync_returns_the_merged_new_set_with_row_ids(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore()

        async def by_url(job, i, url, source, prog):
            data = {
                SERP: [_listing("dup"), _listing("only-a")],
                SERP2: [_listing("dup"), _listing("only-b")],
            }[url]
            return {"blocked": False, "error": None,
                    "data": {"listings": list(data), "pages_crawled": 1}}

        svc = service(settings, jobs, store=lambda s: store, sweep=by_url)
        job = svc.start([SERP, SERP2], sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        ids = sorted(l.listing_id for l in result.listings)
        assert ids == ["dup", "only-a", "only-b"], "the deduped merged new set is returned"
        assert all(l.synced_row_id for l in result.listings), "each merged new row carries a row id"
        assert result.synced.new == 3


class _RaisingStore:
    """A store whose upsert blows up, to drive the sync-failure path. Building it
    still counts as a store construction so tests can assert it was reached."""

    def __init__(self, message):
        self._message = message

    async def upsert_new(self, db_id, listings, column_map=None):
        raise RuntimeError(self._message)


class TestSyncFailure:
    """The scrape succeeds but the Notion write fails. The owner's rule: this is a
    clean failure, NOT a success — drop the scraped-but-unsaved listings, say the
    save failed and nothing was saved, and carry the underlying store error."""

    @pytest.mark.asyncio
    async def test_a_save_failure_fails_the_job_and_drops_the_listings(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = _RaisingStore("the id is wrong or it has not been shared with your integration")
        svc = service(settings, jobs, store=lambda s: store)

        job = svc.start([SERP], sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert result.listings == [], "scraped-but-unsaved listings are not handed back"
        assert result.synced is None
        # Says the scrape worked, saving failed, nothing was saved...
        assert "Scraped 1 listing(s)" in result.error
        assert "saving to your Notion database failed" in result.error
        assert "nothing was saved" in result.error
        # ...and carries the underlying store error verbatim.
        assert "it has not been shared with your integration" in result.error
        # The one-line summary carries the same story.
        assert "saving to Notion failed" in result.summary
        assert "nothing saved" in result.summary

    @pytest.mark.asyncio
    async def test_a_scrape_failure_does_not_read_as_a_save_failure(self, settings, jobs):
        """A source that actually failed is a scrape failure — it must not borrow
        the sync-failure wording, which would wrongly claim the scrape succeeded."""
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        blocked = {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}

        async def sweep(job, i, url, source, prog):
            return blocked

        svc = service(settings, jobs, sweep=sweep)
        job = svc.start([SERP], sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert "saving to your Notion database failed" not in result.error
        assert "saving to Notion failed" not in result.summary


class TestFailure:
    @pytest.mark.asyncio
    async def test_a_block_is_recorded_as_a_failure_with_advice(self, settings, jobs):
        async def blocked(job, i, url, source, prog):
            return {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}

        svc = service(settings, jobs, sweep=blocked)
        job = svc.start([SERP])
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert "anti-bot" in result.error
        assert "try again" in result.error.lower()

    @pytest.mark.asyncio
    async def test_an_exception_lands_on_the_job_not_in_a_lost_task(self, settings, jobs):
        """A background task that raises into nothing leaves the job saying
        "working" forever."""
        async def boom(job, i, url, source, prog):
            raise RuntimeError("the wheels came off")

        svc = service(settings, jobs, sweep=boom)
        job = svc.start([SERP])
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert "the wheels came off" in result.error


class TestMultiUrlFanOut:
    """`urls` is a list: several sources fan out into ONE job, merged and deduped,
    and one source failing does not sink the others (browserd semantics)."""

    def _by_url(self, results: dict):
        """A `_sweep` stub that returns a canned result per URL, so a test can
        script mixed success/failure across the batch."""
        async def sweep(job, i, url, source, prog):
            return results[url]
        return sweep

    @pytest.mark.asyncio
    async def test_empty_list_is_refused_before_any_job(self, settings, jobs):
        with pytest.raises(ValueError) as exc:
            service(settings, jobs).start([])
        assert "empty" in str(exc.value).lower()
        assert jobs.all() == [], "no job for a batch that cannot run"

    @pytest.mark.asyncio
    async def test_all_urls_unsupported_raises_and_creates_no_job(self, settings, jobs):
        with pytest.raises(UnsupportedURL) as exc:
            service(settings, jobs).start(["https://abc.xyz/a", "https://abc.xyz/b"])
        assert jobs.all() == []
        assert KEY_HINT in str(exc.value), "the first URL's own reason, saying what fixes it"

    @pytest.mark.asyncio
    async def test_one_source_failing_leaves_the_others_completed(self, settings, jobs):
        ok = {"blocked": False, "error": None,
              "data": {"listings": [_listing("111")], "pages_crawled": 1}}
        blocked = {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}
        svc = service(settings, jobs,
                      sweep=self._by_url({SERP: ok, SERP2: blocked}))
        job = svc.start([SERP, SERP2])
        await _drain(svc)

        result = svc.result(job.id)
        # One good source is enough to complete, with the good source's listings.
        assert result.status == "completed"
        assert [l.listing_id for l in result.listings] == ["111"]
        # The failure is surfaced, not swallowed.
        assert "1 of 2 source(s) failed" in result.error
        assert "1 source(s) failed" in result.summary

    @pytest.mark.asyncio
    async def test_all_sources_failing_fails_the_job(self, settings, jobs):
        boom = {"blocked": False, "error": "kaboom", "data": {"listings": [], "pages_crawled": 0}}
        blocked = {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}
        svc = service(settings, jobs, sweep=self._by_url({SERP: boom, BROKER: blocked}))
        job = svc.start([SERP, BROKER])
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed"
        assert result.listings == []
        assert "2 of 2 source(s) failed" in result.error
        # The block among the failures still earns the retry advice.
        assert "try again" in result.error.lower()

    @pytest.mark.asyncio
    async def test_listings_are_merged_and_deduped_across_urls(self, settings, jobs):
        # The same listing_id appears on two different swept URLs — it must land
        # once. pages_crawled sums across the sources.
        a = {"blocked": False, "error": None,
             "data": {"listings": [_listing("dup"), _listing("only-a")], "pages_crawled": 2}}
        b = {"blocked": False, "error": None,
             "data": {"listings": [_listing("dup"), _listing("only-b")], "pages_crawled": 3}}
        svc = service(settings, jobs, sweep=self._by_url({SERP: a, SERP2: b}))
        job = svc.start([SERP, SERP2])
        await _drain(svc)

        result = svc.result(job.id)
        ids = sorted(l.listing_id for l in result.listings)
        assert ids == ["dup", "only-a", "only-b"], "the shared listing is not doubled"
        assert result.pages_crawled == 5, "pages sum across sources"
        assert "2 of 2 source(s) swept" in result.summary

    @pytest.mark.asyncio
    async def test_sync_upserts_the_merged_set_once(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        store = FakeStore()
        a = {"blocked": False, "error": None,
             "data": {"listings": [_listing("dup"), _listing("a")], "pages_crawled": 1}}
        b = {"blocked": False, "error": None,
             "data": {"listings": [_listing("dup"), _listing("b")], "pages_crawled": 1}}
        svc = service(settings, jobs, store=lambda s: store,
                      sweep=self._by_url({SERP: a, SERP2: b}))
        svc.start([SERP, SERP2], sync=True)
        await _drain(svc)

        # ONE upsert, of the deduped union — not one per URL.
        assert len(store.upserts) == 1, "the merged set is upserted once, not per source"
        db_id, listings = store.upserts[0]
        assert db_id == "db-1"
        assert sorted(l.listing_id for l in listings) == ["a", "b", "dup"]

    @pytest.mark.asyncio
    async def test_an_unsupported_url_among_valid_ones_is_a_recorded_failure(self, settings, jobs):
        ok = {"blocked": False, "error": None,
              "data": {"listings": [_listing("kept")], "pages_crawled": 1}}
        svc = service(settings, jobs, sweep=self._by_url({SERP: ok}))
        # The middle URL is not a supported listings page.
        job = svc.start([SERP, "https://abc.xyz/nope"])
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed"
        assert [l.listing_id for l in result.listings] == ["kept"]
        assert "1 of 2 source(s) failed" in result.error
        # The URL's own reason, not a generic "not supported".
        assert f"abc.xyz ({sources_hint()})" in result.error
        assert "not a supported listings page" not in result.error


class TestJobLabel:
    """`describe(job)` is the sweep task's own label formatter — verb · source
    label · count — colocated with the sweep and resolving the source's human
    name via the source registry, not a string baked into the template."""

    def test_multi_source_names_the_source_and_count(self):
        job = SweepTask(id="a", source="bizbuysell_serp", urls=[SERP, SERP2, BROKER])
        assert describe(job) == "Listing sweep · BizBuySell · 3 sources"

    def test_single_source_drops_the_count(self):
        job = SweepTask(id="b", source="bizbuysell_serp", urls=[SERP])
        # No "1 sources" noise for a single-URL sweep.
        assert describe(job) == "Listing sweep · BizBuySell"
        assert "sources" not in describe(job)

    def test_broker_source_uses_its_own_label(self):
        job = SweepTask(id="c", source="bizbuysell_broker", urls=[BROKER])
        assert describe(job) == "Listing sweep · BizBuySell broker"

    def test_unknown_source_falls_back_to_the_raw_id(self):
        """An old job whose source was retired must still render, not break — the
        registry returns the raw id when no adapter owns it."""
        job = SweepTask(id="d", source="craigslist_biz", urls=[SERP, SERP2])
        assert describe(job) == "Listing sweep · craigslist_biz · 2 sources"


class TestFanOutRespectsCapacity:
    """A single job with many URLs must never launch more browsers at once than
    the task budget — the same ceiling the single-sweep path enforced."""

    @pytest.mark.asyncio
    async def test_one_job_with_many_urls_stays_within_task_budget(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        settings.update(max_instances=4, interactive_reserve=1)
        assert settings.load().task_budget == 3

        release = asyncio.Event()
        concurrent = 0
        peak = 0

        async def retry(instances, *, profile, on_launch=None, **kw):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            try:
                await release.wait()
            finally:
                concurrent -= 1
            return {"blocked": False, "error": None,
                    "data": {"listings": [_listing(profile)], "pages_crawled": 1}}

        monkeypatch.setattr("app.services.scrape.scrape_with_retry", retry)
        profiles = ProfileStore(tmp_path / "profiles")
        pool = TaskProfilePool(profiles, settings)
        svc = ScrapeService(instances=object(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=pool)

        # Eight URLs in ONE job. All valid SERPs (distinct regions).
        urls = [f"https://www.bizbuysell.com/x{n}-businesses-for-sale/" for n in range(8)]
        job = svc.start(urls)

        # Let the admitted sources reach the parked launch and settle.
        for _ in range(200):
            await asyncio.sleep(0.005)
            if concurrent >= 3:
                break
        await asyncio.sleep(0.05)  # any wrongly-admitted extra would show up here

        assert peak <= 3, f"launched {peak} browsers at once, budget is 3"
        # The pool never mints more than the budget, even for eight URLs.
        pooled = [p.name for p in profiles.all() if p.name.startswith("task-")]
        assert len(pooled) <= 3, f"minted {pooled}, expected at most 3"

        release.set()
        await _drain(svc)
        result = svc.result(job.id)
        assert result.status == "completed"
        assert result.pages_crawled == 8, "all eight sources ran (serialised past the budget)"
        final = sorted(p.name for p in profiles.all() if p.name.startswith("task-"))
        assert final == ["task-1", "task-2", "task-3"], "eight URLs, three profiles"


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
        job = svc.start([SERP])
        await _drain(svc)

        assert captured["profile"] == "task-1", "launched on a pooled identity"
        assert captured["owner"] == f"job:{job.id}", "owner tag preserved"
        names = [p.name for p in profiles.all()]
        assert "task-1" in names
        assert not any(n.startswith("serp-") for n in names), "no per-URL profile minted"
        # Leases are keyed per source (job.id:index); a clean sweep returns them.
        assert pool.leased_by(f"{job.id}:0") == [], "lease returned after a clean sweep"

    @pytest.mark.asyncio
    async def test_a_crashed_sweep_releases_its_lease(
        self, settings, jobs, monkeypatch, tmp_path,
    ):
        """The finally path in _run: even a launch that explodes must not leak the
        lease and pin the profile as busy forever."""
        async def retry(instances, *, profile, **kw):
            raise RuntimeError("launch exploded")

        svc, pool, _ = self._pooled(settings, jobs, monkeypatch, tmp_path, retry)
        job = svc.start([SERP])
        await _drain(svc)

        assert svc.result(job.id).status == "failed"
        assert pool.leased_by(f"{job.id}:0") == [], "lease freed despite the crash"
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
            svc.start([SERP])

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
        job1 = svc.start([SERP])
        await launched.wait()          # job1 is past the gate and scraping
        job2 = svc.start([SERP])         # job2 must queue at the gate
        await asyncio.sleep(0.05)      # let job2 set its summary and block

        assert svc.result(job1.id).summary == _SCRAPING_SUMMARY, "running sweep: scraping"
        assert svc.result(job2.id).summary == _WAITING_SUMMARY, "queued sweep: waiting"
        assert svc.result(job2.id).status == "working", "still working, just queued"

        release.set()
        await _drain(svc)
        # Once it actually runs and completes, the waiting text is gone.
        assert svc.result(job2.id).status == "completed"
        assert "source(s) swept" in svc.result(job2.id).summary
        assert "source(s) swept" in svc.result(job1.id).summary


class TestCollecting:
    def test_an_unknown_job_is_none(self, settings, jobs):
        assert service(settings, jobs).result("nosuchjob") is None

    def test_collecting_an_archive_says_so_instead_of_faking_a_sweep(self, settings, jobs):
        """Every kind of task is minted from one id space, so an agent holding an
        archive's id is a reachable mistake — and the one answer it must never
        get is a ScrapeResult with an empty `listings`, which reads as a sweep
        that found nothing."""
        task = jobs.create(kind="archive", url="https://example.com/x", notion_page_id="page-1")

        with pytest.raises(NotASweep) as exc:
            service(settings, jobs).result(task.id)

        assert "archive task" in str(exc.value)
        assert "scrape_listings" in str(exc.value), "say which ids this does take"

    @pytest.mark.asyncio
    async def test_collecting_never_waits_for_the_sweep(self, settings, jobs):
        """Poll semantics: it answers with whatever is true right now."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(job, i, url, source, prog):
            started.set()
            await release.wait()
            return await _ok(job, i, url, source, prog)

        svc = service(settings, jobs, sweep=slow)
        job = svc.start([SERP])
        await started.wait()

        result = await asyncio.wait_for(asyncio.to_thread(svc.result, job.id), timeout=1)
        assert result.status == "working"
        assert result.listings == []

        release.set()
        await _drain(svc)
        assert svc.result(job.id).status == "completed"


# ── The sweep loop itself: paging hooks, per-page failures, legibility ───────
#
# These run the real _sweep_once (and, where retries matter, the real
# scrape_with_retry) against a fake browser and a scripted source, so what is
# pinned is the loop's own behaviour: how it navigates, when it stops, and what
# a page it cannot use leaves behind.

from app.config import CONFIG  # noqa: E402
from app.services.browsing import scrape_with_retry  # noqa: E402
from app.services.scrape import _RunProgress  # noqa: E402
from app.sources import CardPage  # noqa: E402

LIST = "https://example.com/businesses-for-sale"


def _gen(i: int, **fields) -> Listing:
    base = {"url": f"https://example.com/listing/{i}", "title": f"Business {i}",
            "asking_price": "$500,000", "source": "fake"}
    base.update(fields)
    return Listing(**base)


class _FakePage:
    """Where a real page would be: `url` follows every goto (or an advance)."""

    def __init__(self):
        self.url = "about:blank"
        self.gotos: list[str] = []

    async def goto(self, url, **kw):
        self.gotos.append(url)
        self.url = url

    async def wait_for_timeout(self, ms):
        pass


class _Ctx:
    def __init__(self, page):
        self.pages = [page]


class _FakeInst:
    def __init__(self, n: int, page):
        self.id = f"inst-{n}"
        self.proxy_ip = "1.2.3.4"
        self.context = _Ctx(page)
        self.touches = 0

    def touch(self):
        self.touches += 1


class _Profiles:
    def __init__(self):
        self.rotations = 0

    def rotate_session(self, profile):
        self.rotations += 1


class _FakeInstances:
    """Every launch hands back a fresh instance on a fresh page."""

    def __init__(self):
        self.profiles = _Profiles()
        self.launches = 0
        self.pages: list[_FakePage] = []

    async def launch(self, req, *, origin, owner, wait):
        self.launches += 1
        page = _FakePage()
        self.pages.append(page)
        return _FakeInst(self.launches, page)

    async def stop(self, iid):
        pass


class _Pool:
    def acquire(self, key):
        return "task-1"

    def release(self, key):
        pass


class _ScriptedSource:
    """Hands back one scripted CardPage per page read, counted from `begin`.

    `attempts` is a list of scripts, one per attempt; the last is reused. With
    `advance_to`, the source pages by its own `advance` (landing on that URL
    and answering `advance_result`); without it, it has no `advance` at all.
    """

    name = "fake"
    label = "Fake"

    def __init__(self, *attempts: list[CardPage], advance_to: str | None = None,
                 advance_result: bool = True, advance_raises: Exception | None = None):
        self._attempts = list(attempts)
        self.begins = 0
        self.reads = 0
        self.advanced: list[int] = []
        if advance_to is not None:
            async def advance(page, n):
                self.advanced.append(n)
                if advance_raises is not None:
                    raise advance_raises
                if advance_result:
                    page.url = f"{advance_to}#page-{n}"
                return advance_result
            self.advance = advance

    def begin(self):
        self.begins += 1
        self.reads = 0

    def page_url(self, url, n):
        return url if n == 1 else f"{url}?page={n}"

    async def cards(self, page):
        script = self._attempts[min(self.begins, len(self._attempts)) - 1]
        self.reads += 1
        return script[min(self.reads, len(script)) - 1]


class _PlainSource:
    """A source with neither `begin` nor `advance`: BizBuySell's shape."""

    name = "fake"

    def __init__(self, pages: list[CardPage]):
        self._pages = pages
        self.reads = 0

    def page_url(self, url, n):
        return url if n == 1 else f"{url}{n}/"

    async def cards(self, page):
        self.reads += 1
        return self._pages[min(self.reads, len(self._pages)) - 1]


def _job(jobs, max_pages=3) -> SweepTask:
    return jobs.create(source="fake", urls=[LIST], max_pages=max_pages, status="working")


async def _once(svc, job, source, tmp_path, page=None, inst=None):
    page = page or _FakePage()
    inst = inst or _FakeInst(1, page)
    res = await svc._sweep_once(inst, page, job, LIST, source, tmp_path / "ev")
    return res, page, inst


def _meta(path: Path) -> dict:
    import json

    return json.loads((path / "metadata.json").read_text())


class TestPagingHooks:
    @pytest.mark.asyncio
    async def test_a_source_without_advance_pages_by_url_as_before(self, settings, jobs, tmp_path):
        source = _PlainSource([CardPage([_gen(1), _gen(2)]), CardPage([_gen(3)]),
                               CardPage([_gen(4)])])
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, page, inst = await _once(svc, _job(jobs), source, tmp_path)

        assert page.gotos == [LIST, f"{LIST}2/", f"{LIST}3/"], "every page is page_url(url, n)"
        assert inst.touches == 3
        assert res["error"] is None
        assert [l.url for l in res["data"]["listings"]] == [_gen(i).url for i in (1, 2, 3, 4)]
        assert res["data"]["pages_crawled"] == 3

    @pytest.mark.asyncio
    async def test_advance_is_used_for_every_page_after_the_first(self, settings, jobs, tmp_path):
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)]), CardPage([_gen(3)])],
                                 advance_to="https://example.com/clicked")
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, page, inst = await _once(svc, _job(jobs), source, tmp_path)

        assert page.gotos == [LIST], "page 1 is page_url(url, 1); the rest are advance"
        assert source.advanced == [2, 3]
        assert inst.touches == 3, "the instance is kept alive on every page either way"
        assert len(res["data"]["listings"]) == 3

    @pytest.mark.asyncio
    async def test_advance_returning_false_stops_paging(self, settings, jobs, tmp_path):
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)])],
                                 advance_to="https://example.com/x", advance_result=False)
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)

        assert source.advanced == [2]
        assert source.reads == 1, "no next page, so nothing more is read"
        assert res["error"] is None and res["blocked"] is False
        assert res["data"]["pages_crawled"] == 1
        assert [l.url for l in res["data"]["listings"]] == [_gen(1).url]

    @pytest.mark.asyncio
    async def test_a_next_page_that_cannot_be_reached_is_said_and_earlier_pages_kept(
        self, settings, jobs, tmp_path,
    ):
        """Not the end of the list: the pages after it exist and were not read."""
        from app.sources import PageNotReached

        why = "the next-page control on page 1 (text 'Next') could not be clicked"
        source = _ScriptedSource([CardPage([_gen(1), _gen(2)]), CardPage([_gen(3)])],
                                 advance_to="https://example.com/x",
                                 advance_raises=PageNotReached(why))
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)

        assert res["error"] is None and res["blocked"] is False
        assert res["warning"] == f"stopped at page 2 and kept the 2 listing(s) from page 1: {why}"
        assert [l.url for l in res["data"]["listings"]] == [_gen(1).url, _gen(2).url]
        assert source.reads == 1

    @pytest.mark.asyncio
    async def test_the_page_limit_of_a_longer_list_is_noted_not_an_error(
        self, settings, jobs, tmp_path,
    ):
        """FCBB's list runs past page 20; its first 6 pages are the ones wanted."""
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)]), CardPage([_gen(3)])],
                                 advance_to="https://example.com/x")
        source.has_next_page = lambda: True
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs, max_pages=2), source, tmp_path)

        assert res["error"] is None and "warning" not in res
        assert res["more_pages"] is True
        assert len(res["data"]["listings"]) == 2
        assert _meta(tmp_path / "ev" / "final")["reason"] == "page limit"

    @pytest.mark.asyncio
    async def test_a_sweep_names_the_urls_not_fully_crawled(self, settings, jobs, monkeypatch):
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)]), CardPage([_gen(3)])],
                                 advance_to="https://example.com/x")
        source.has_next_page = lambda: True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        svc = ScrapeService(instances=_FakeInstances(), jobs=jobs, settings=settings,
                            task_profiles=_Pool())
        job = svc.start([LIST], max_pages=2)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed" and result.error is None
        assert result.not_fully_crawled == [LIST]
        host = LIST.split("/")[2]
        assert f"not fully crawled (more pages than max_pages=2): {host}" in result.summary
        assert "stopped early" not in result.summary
        assert svc._jobs.get(job.id).decisions[0]["not_fully_crawled"] is True

    @pytest.mark.asyncio
    async def test_a_list_read_to_its_end_is_not_named(self, settings, jobs, monkeypatch):
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)])],
                                 advance_to="https://example.com/x")
        source.has_next_page = lambda: False
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        svc = ScrapeService(instances=_FakeInstances(), jobs=jobs, settings=settings,
                            task_profiles=_Pool())
        job = svc.start([LIST], max_pages=2)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.not_fully_crawled == [] and "not fully crawled" not in result.summary

    @pytest.mark.asyncio
    @pytest.mark.parametrize("more", [False, None])
    async def test_a_limit_reached_at_the_end_or_by_a_source_that_cannot_tell_is_silent(
        self, settings, jobs, tmp_path, more,
    ):
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)])],
                                 advance_to="https://example.com/x")
        if more is not None:
            source.has_next_page = lambda: more
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs, max_pages=2), source, tmp_path)
        assert res["error"] is None and "warning" not in res and "more_pages" not in res

    @pytest.mark.asyncio
    async def test_a_list_that_ended_before_the_limit_is_silent(self, settings, jobs, tmp_path):
        """Paging stopped on a page of listings already seen, not at the limit."""
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(1)])],
                                 advance_to="https://example.com/x")
        source.has_next_page = lambda: True
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs, max_pages=3), source, tmp_path)
        assert res["error"] is None and "warning" not in res and "more_pages" not in res
        assert source.reads == 2

    @pytest.mark.asyncio
    async def test_evidence_records_where_the_browser_is_not_page_url(
        self, settings, jobs, tmp_path,
    ):
        """After a click, page_url(url, 2) is somewhere the browser never went."""
        source = _ScriptedSource(
            [CardPage([_gen(1)]), CardPage([], blocked=True)],
            advance_to="https://example.com/clicked",
        )
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)

        assert res["blocked"] is True
        meta = _meta(tmp_path / "ev" / "page-02-blocked")
        assert meta["url"] == "https://example.com/clicked#page-2"
        assert meta["reason"] == "blocked"

    @pytest.mark.asyncio
    async def test_begin_is_called_at_the_start_of_every_attempt(
        self, settings, jobs, tmp_path, monkeypatch,
    ):
        """The same source object serves every attempt, so its per-attempt state
        is reset each time — here, which page of its script it is on."""
        async def _instant(*_a, **_k):
            return None

        monkeypatch.setattr("app.services.browsing.asyncio.sleep", _instant)
        source = _ScriptedSource(
            [CardPage([_gen(1)]), CardPage([], blocked=True)],   # attempt 1: blocked on page 2
            [CardPage([_gen(1)]), CardPage([_gen(2)])],          # attempt 2: clean
        )
        instances = _FakeInstances()
        svc = ScrapeService(instances=instances, jobs=jobs, settings=settings, task_profiles=_Pool())
        job = _job(jobs, max_pages=2)

        res = await scrape_with_retry(
            instances, profile="task-1", owner="job:x", wait_ms=0, attempts=3,
            scrape_once=lambda inst, page: svc._sweep_once(inst, page, job, LIST, source,
                                                           tmp_path / "ev"),
        )

        assert source.begins == 2, "once per attempt"
        assert instances.launches == 2 and instances.profiles.rotations == 1
        assert res["error"] is None and res["blocked"] is False
        assert [l.url for l in res["data"]["listings"]] == [_gen(1).url, _gen(2).url], (
            "the second attempt started from page 1 of its own script"
        )


class TestSeenUrls:
    @pytest.mark.asyncio
    async def test_a_page_of_dropped_cards_does_not_end_paging(self, settings, jobs, tmp_path):
        """Page 2 is all sold listings: nothing returned, but four new cards were
        on it, so it is not the end of the feed."""
        sold = [f"https://example.com/listing/sold-{i}" for i in range(4)]
        source = _PlainSource([
            CardPage([_gen(1), _gen(2)]),
            CardPage([], seen_urls=sold),
            CardPage([_gen(3)]),
        ])
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)

        assert source.reads == 3
        assert [l.url for l in res["data"]["listings"]] == [_gen(i).url for i in (1, 2, 3)]
        assert res["data"]["pages_crawled"] == 3

    @pytest.mark.asyncio
    async def test_without_seen_urls_an_empty_later_page_still_stops(
        self, settings, jobs, tmp_path,
    ):
        """BizBuySell's empty last page: unchanged, and never judged illegible."""
        source = _PlainSource([CardPage([_gen(1)]), CardPage([]), CardPage([_gen(9)])])
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)

        assert source.reads == 2
        assert res["error"] is None
        assert [c["page"] for c in res["data"]["legibility"]] == [1], "page 2 was not judged"

    @pytest.mark.asyncio
    async def test_already_seen_cards_still_end_paging(self, settings, jobs, tmp_path):
        """seen_urls widens what counts as seen; it does not make a repeat fresh."""
        source = _PlainSource([
            CardPage([_gen(1)], seen_urls=[_gen(1).url, "https://example.com/listing/sold"]),
            CardPage([], seen_urls=["https://example.com/listing/sold"]),
            CardPage([_gen(5)]),
        ])
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs), source, tmp_path)
        assert source.reads == 2


class TestPageFailures:
    """A page that loaded, was not a block, and still cannot be used."""

    def _svc(self, settings, jobs, instances, typesafe=None):
        return ScrapeService(instances=instances, jobs=jobs, settings=settings,
                             store_factory=FakeStore, task_profiles=_Pool(), typesafe=typesafe)

    @pytest.mark.asyncio
    async def test_a_source_error_fails_the_source_once_with_evidence(
        self, settings, jobs, monkeypatch,
    ):
        source = _ScriptedSource([CardPage([], error="No list of businesses for sale on this page",
                                           retry=False)])
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        instances = _FakeInstances()
        svc = self._svc(settings, jobs, instances)
        job = svc.start([LIST])
        await _drain(svc)

        assert instances.launches == 1, "retry=False: no second attempt"
        assert instances.profiles.rotations == 0, "and no new exit IP"
        result = svc.result(job.id)
        assert result.status == "failed"
        assert "No list of businesses for sale on this page" in result.error
        assert "example.com" in result.error
        evidence = (CONFIG.evidence_dir / job.id / "source-01"
                    / "page-01-no-list-of-businesses-for-sale-on-this-page")
        assert evidence.is_dir(), sorted(p.name for p in evidence.parent.iterdir())
        meta = _meta(evidence)
        assert meta["error"] == "No list of businesses for sale on this page"
        assert meta["url"] == LIST and meta["page"] == 1

    @pytest.mark.asyncio
    async def test_an_illegible_later_page_stops_paging_and_keeps_the_pages_before_it(
        self, settings, jobs, monkeypatch,
    ):
        """Page 2 reads wrong: page 1 was read and checked, so it is kept, and the
        stop is said in the job's error, its summary and its decisions."""
        untitled = [_gen(i, title="") for i in range(5)]
        source = _ScriptedSource([CardPage([_gen(1), _gen(2)]), CardPage(untitled),
                                  CardPage([_gen(3)])])
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        instances = _FakeInstances()
        svc = self._svc(settings, jobs, instances)
        job = svc.start([LIST], max_pages=3)
        await _drain(svc)

        assert instances.launches == 1 and instances.profiles.rotations == 0
        assert source.reads == 2, "paging stopped at the page that could not be used"
        result = svc.result(job.id)
        assert result.status == "completed"
        assert [l.url for l in result.listings] == [_gen(1).url, _gen(2).url]
        assert result.error.startswith("1 of 1 source(s) stopped early: example.com (stopped "
                                       "at page 2 and kept the 2 listing(s) from page 1: ")
        assert "0 of 5 cards on page 2" in result.error
        assert "1 source(s) stopped early (earlier pages kept)" in result.summary
        assert "2 listing(s) across 2 pages" in result.summary
        (decision,) = jobs.get(job.id).decisions
        assert "stopped at page 2" in decision["warning"] and "error" not in decision
        evidence = CONFIG.evidence_dir / job.id / "source-01" / "page-02-illegible"
        meta = _meta(evidence)
        assert meta["reason"] == "illegible" and meta["partial"] is True
        assert meta["url"] == f"{LIST}?page=2" and meta["found"] == 2

    @pytest.mark.asyncio
    async def test_a_source_error_on_a_later_page_keeps_the_pages_before_it(
        self, settings, jobs, monkeypatch,
    ):
        """The generic reader's classifier going down on page 3 (or finding no
        list there) costs page 3, not pages 1 and 2."""
        down = "Could not read the listings on page 3: the Decision API did not answer."
        source = _ScriptedSource([CardPage([_gen(1)]), CardPage([_gen(2)]),
                                  CardPage([], error=down, retry=False)])
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        instances = _FakeInstances()
        svc = self._svc(settings, jobs, instances)
        job = svc.start([LIST], max_pages=5)
        await _drain(svc)

        assert instances.launches == 1
        result = svc.result(job.id)
        assert result.status == "completed"
        assert [l.url for l in result.listings] == [_gen(1).url, _gen(2).url]
        assert "stopped at page 3 and kept the 2 listing(s) from pages 1–2: " + down \
            in result.error
        assert (CONFIG.evidence_dir / job.id / "source-01"
                / "page-03-could-not-read-the-listings-on-page-3-the-decision").is_dir()

    @pytest.mark.asyncio
    async def test_a_later_page_stop_under_sync_still_saves_the_earlier_pages(
        self, settings, jobs, monkeypatch,
    ):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        source = _ScriptedSource([CardPage([_gen(1), _gen(2)]),
                                  CardPage([_gen(i, title="") for i in range(3, 8)])])
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        svc = self._svc(settings, jobs, _FakeInstances())
        job = svc.start([LIST], max_pages=2, sync=True)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed" and result.synced.new == 2
        assert "stopped at page 2" in result.error

    @pytest.mark.asyncio
    async def test_an_adapter_s_illegible_first_page_is_retried_from_a_new_exit_ip(
        self, settings, jobs, monkeypatch,
    ):
        """A site adapter's page that reads wrong is most likely a soft block or a
        variant page served to a flagged IP, so it is retried like a block."""
        real_sleep = asyncio.sleep

        async def _quick(*_a, **_k):
            await real_sleep(0)  # the backoff, skipped — but still a yield

        monkeypatch.setattr("app.services.browsing.asyncio.sleep", _quick)
        untitled = [_gen(i, title="") for i in range(5)]
        source = _ScriptedSource([CardPage(untitled)], [CardPage([_gen(1)])])
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        instances = _FakeInstances()
        svc = self._svc(settings, jobs, instances)
        job = svc.start([LIST], max_pages=1)
        await _drain(svc)

        assert instances.launches == 2 and instances.profiles.rotations == 1
        result = svc.result(job.id)
        assert result.status == "completed" and result.error is None
        assert [l.url for l in result.listings] == [_gen(1).url]
        assert (CONFIG.evidence_dir / job.id / "source-01" / "page-01-illegible").is_dir()

    @pytest.mark.asyncio
    async def test_the_generic_reader_s_illegible_first_page_is_final(
        self, settings, jobs, monkeypatch,
    ):
        """The generic reader chose those cards with the classifier: a new exit IP
        shows the same page and the same judgement, so there is no retry."""
        untitled = [_gen(i, title="") for i in range(5)]
        source = _ScriptedSource([CardPage(untitled)], [CardPage([_gen(1)])])
        source.chooses_cards = True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        instances = _FakeInstances()
        svc = self._svc(settings, jobs, instances)
        job = svc.start([LIST], max_pages=1)
        await _drain(svc)

        assert instances.launches == 1 and instances.profiles.rotations == 0
        result = svc.result(job.id)
        assert result.status == "failed"
        assert "0 of 5 cards on page 1" in result.error

    @pytest.mark.asyncio
    async def test_the_classifier_is_asked_only_once_a_key_is_saved(
        self, settings, jobs, tmp_path,
    ):
        """No key: no request at all — a BizBuySell sweep makes no classifier
        call, as before the classifier existed. With one: a request per card."""
        from app.services.typesafe import Noul

        class Fake:
            def __init__(self):
                self.calls = 0

            async def ask(self, state, questions):
                self.calls += 1
                return {name: Noul(probability=0.05) for name in questions}

        fake = Fake()
        svc = self._svc(settings, jobs, None, typesafe=fake)
        cards = [_gen(1), _gen(2), _gen(3)]
        res, _, _ = await _once(svc, _job(jobs, max_pages=1), _PlainSource([CardPage(cards)]),
                                tmp_path)
        assert fake.calls == 0, "no key saved: the code checks alone decide"
        assert res["error"] is None and "eligibility" not in res["data"]["legibility"][0]

        settings.update(openrouter_api_key="sk-or-test")
        res, _, _ = await _once(svc, _job(jobs, max_pages=1), _PlainSource([CardPage(cards)]),
                                tmp_path)
        assert fake.calls == 3, "one request per card"
        assert res["retry"] is True, "an adapter's page that reads wrong gets a new exit IP"
        assert ("Only 0 of 3 cards on page 1 read as business listings currently for sale"
                in res["error"])
        assert (tmp_path / "ev" / "page-01-illegible").is_dir()

    @pytest.mark.asyncio
    async def test_a_classifier_outage_keeps_the_page(self, settings, jobs, tmp_path):
        """A BizBuySell sweep keeps working while the classifier is down."""
        from app.services.typesafe import TypeSafeUnavailable

        class Down:
            calls = 0

            async def ask(self, state, questions):
                Down.calls += 1
                await asyncio.sleep(0)  # in flight, as a real request is
                raise TypeSafeUnavailable("The Decision API did not answer.")

        settings.update(openrouter_api_key="sk-or-test")
        svc = self._svc(settings, jobs, None, typesafe=Down())
        res, _, _ = await _once(svc, _job(jobs, max_pages=1),
                                _PlainSource([CardPage([_gen(i) for i in range(8)])]), tmp_path)

        assert res["error"] is None
        assert len(res["data"]["listings"]) == 8
        assert Down.calls == 5, "the requests already in flight, and no more"
        eligibility = res["data"]["legibility"][0]["eligibility"]
        assert "did not answer" in eligibility["stopped"] and eligibility["unanswered"] == 8
        assert "did not answer" in _meta(tmp_path / "ev" / "final")["legibility"][0][
            "eligibility"]["stopped"], "recorded in the run's evidence"

    @pytest.mark.asyncio
    async def test_a_refused_request_on_a_bizbuysell_page_fails_nothing(
        self, settings, jobs, tmp_path,
    ):
        """A 400 for one card is that card's; the page and its card are kept."""
        from app.services.typesafe import Noul, TypeSafeError

        class Picky:
            async def ask(self, state, questions):
                if state["title"] == "Business 2":
                    raise TypeSafeError("The Decision API refused this request (HTTP 400).")
                return {"eligible": Noul(0.9)}

        settings.update(openrouter_api_key="sk-or-test")
        svc = self._svc(settings, jobs, None, typesafe=Picky())
        res, _, _ = await _once(svc, _job(jobs, max_pages=1),
                                _PlainSource([CardPage([_gen(i) for i in range(4)])]), tmp_path)
        assert res["error"] is None and len(res["data"]["listings"]) == 4
        assert res["data"]["legibility"][0]["eligibility"]["errors"] == 1

    @pytest.mark.asyncio
    async def test_cards_without_a_title_are_dropped_but_the_page_is_kept(
        self, settings, jobs, tmp_path,
    ):
        cards = [_gen(i) for i in range(9)] + [_gen(9, title="")]
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        res, _, _ = await _once(svc, _job(jobs, max_pages=1), _PlainSource([CardPage(cards)]),
                                tmp_path)
        assert res["error"] is None
        assert len(res["data"]["listings"]) == 9
        assert res["data"]["legibility"][0]["dropped"] == 1


class _TitleJudge:
    """A classifier that answers each card's one request by its title: not
    eligible for the titles in `low`, eligible otherwise."""

    def __init__(self, low: set[str]):
        self.low = low
        self.calls = 0
        self.asked: list[str] = []

    async def ask(self, state, questions):
        from app.services.typesafe import Noul

        self.calls += 1
        self.asked.append(state["title"])
        return {"eligible": Noul(probability=0.05 if state["title"] in self.low else 0.93)}


class TestWhoMayDropCards:
    """A card judged not for sale now is removed only from a source that chose
    the cards; an adapter's page is judged as a page."""

    @pytest.mark.asyncio
    async def test_bizbuysell_cards_are_never_dropped_one_by_one(self, settings, jobs, tmp_path):
        import json

        from app.sources.bizbuysell import JS_CARDS, BizBuySellSerp

        titles = ["Laundromat — Owner Retiring", "HVAC Contractor", "Coin Op Car Wash",
                  "Dental Lab", "Pizza Franchise", "Machine Shop"]
        cards = [{"listing_id": str(2400000 + i),
                  "url": f"https://www.bizbuysell.com/business-opportunity/x/{2400000 + i}/",
                  "title": t, "location": "Oakland, CA", "asking_price": "$1,200,000",
                  "cashflow": "$300,000", "excerpt": f"{t}."} for i, t in enumerate(titles)]

        class SerpPage(_FakePage):
            async def evaluate(self, js, arg=None):
                if js != JS_CARDS:
                    return None
                return json.dumps({"title": "Businesses For Sale", "blocked": False,
                                   "cards": cards})

        settings.update(openrouter_api_key="sk-or-test")
        judge = _TitleJudge({"Laundromat — Owner Retiring", "Coin Op Car Wash"})
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings, typesafe=judge)
        res, _, _ = await _once(svc, _job(jobs, max_pages=1), BizBuySellSerp(), tmp_path,
                                page=SerpPage())

        assert judge.calls == 6, "one request per card"
        assert res["error"] is None
        assert [l.title for l in res["data"]["listings"]] == titles, "every card is kept"
        (check,) = res["data"]["legibility"]
        assert (check["eligibility"]["not_eligible"], check["eligibility"]["dropped"],
                check["kept"]) == (2, 0, 6)

    @pytest.mark.asyncio
    async def test_the_generic_reader_drops_what_is_not_for_sale_and_the_summary_says_so(
        self, settings, jobs, monkeypatch,
    ):
        """Menu links and sold tiles alike: there is no separate sold handling —
        the per-listing request's eligibility is it."""
        cards = ([_gen(0, title="Sell Your Business"), _gen(1, title="Pizzeria – Sold")]
                 + [_gen(i) for i in range(2, 8)])
        source = _ScriptedSource([CardPage(cards)])
        source.chooses_cards = True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        settings.update(openrouter_api_key="sk-or-test")
        svc = ScrapeService(instances=_FakeInstances(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=_Pool(),
                            typesafe=_TitleJudge({"Sell Your Business", "Pizzeria – Sold"}))
        job = svc.start([LIST], max_pages=1)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed" and result.error is None
        assert len(result.listings) == 6
        assert "Pizzeria – Sold" not in [l.title for l in result.listings]
        assert "2 left out as not currently for sale / not listings" in result.summary
        (entry,) = jobs.get(job.id).decisions
        eligibility = entry["legibility"][0]["eligibility"]
        assert eligibility["dropped"] == 2
        assert eligibility["not_eligible_listings"] == [
            {"title": "Sell Your Business", "p": 0.05}, {"title": "Pizzeria – Sold", "p": 0.05}]

    @pytest.mark.asyncio
    async def test_a_first_page_mostly_not_for_sale_fails_its_url(
        self, settings, jobs, monkeypatch,
    ):
        cards = [_gen(i, title=f"Sold {i}") for i in range(4)] + [_gen(9)]
        source = _ScriptedSource([CardPage(cards)])
        source.chooses_cards = True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        settings.update(openrouter_api_key="sk-or-test")
        judge = _TitleJudge({f"Sold {i}" for i in range(4)})
        instances = _FakeInstances()
        svc = ScrapeService(instances=instances, jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=_Pool(), typesafe=judge)
        job = svc.start([LIST], max_pages=2)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "failed" and instances.launches == 1, "final, not retried"
        assert ("Only 1 of 5 cards on page 1 read as business listings currently for sale"
                in result.error)
        assert (CONFIG.evidence_dir / job.id / "source-01" / "page-01-illegible").is_dir()

    @pytest.mark.asyncio
    async def test_a_later_page_with_nothing_for_sale_ends_paging_quietly(
        self, settings, jobs, monkeypatch,
    ):
        """An infinite scroll that runs from its listings into its sold ones."""
        sold = [_gen(i, title=f"Sold {i}") for i in range(10, 14)]
        source = _ScriptedSource([CardPage([_gen(1), _gen(2), _gen(3)]), CardPage(sold),
                                  CardPage([_gen(4)])])
        source.chooses_cards = True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        settings.update(openrouter_api_key="sk-or-test")
        svc = ScrapeService(instances=_FakeInstances(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=_Pool(),
                            typesafe=_TitleJudge({s.title for s in sold}))
        job = svc.start([LIST], max_pages=3)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed" and source.reads == 2
        assert [l.url for l in result.listings] == [_gen(i).url for i in (1, 2, 3)]
        assert result.error is None, "the end of what is for sale, not a page read wrong"
        legibility = svc._jobs.get(job.id).decisions[0]["legibility"]
        assert legibility[1]["sold_out"] is True and legibility[1]["ok"] is True

    @pytest.mark.asyncio
    async def test_a_later_page_mostly_sold_keeps_its_listings_for_sale(
        self, settings, jobs, monkeypatch,
    ):
        """Dealonomy's page 2 is its closed deals; failing the page as "not
        listings" would drop a listing for sale among them with the rest."""
        sold = [_gen(i, title=f"Sold {i}") for i in range(10, 13)]
        source = _ScriptedSource([CardPage([_gen(1), _gen(2), _gen(3)]),
                                  CardPage([*sold, _gen(4)]), CardPage([_gen(5)])])
        source.chooses_cards = True
        monkeypatch.setattr("app.sources.for_url", lambda url: source)
        settings.update(openrouter_api_key="sk-or-test")
        svc = ScrapeService(instances=_FakeInstances(), jobs=jobs, settings=settings,
                            store_factory=FakeStore, task_profiles=_Pool(),
                            typesafe=_TitleJudge({s.title for s in sold}))
        job = svc.start([LIST], max_pages=3)
        await _drain(svc)

        result = svc.result(job.id)
        assert result.status == "completed" and source.reads == 3
        assert [l.url for l in result.listings] == [_gen(i).url for i in (1, 2, 3, 4, 5)]
        assert result.error is None


# ── which source reads a URL, the classifier preflight, and diagnostics ──────


def sources_hint() -> str:
    from app.services.scrape import NEEDS_CLASSIFIER

    return NEEDS_CLASSIFIER


class FakeTypeSafe:
    """The classifier client, as far as `submit` uses it: `check()`, counted."""

    def __init__(self, error=None):
        from app.services.typesafe import TypeSafeCheck

        self.checks = 0
        self._result = (TypeSafeCheck(ok=True, message="Working: jev answered in 90 ms.")
                        if error is None else
                        TypeSafeCheck(ok=False, message=str(error), error=error))

    async def check(self, key=None, model=None):
        self.checks += 1
        return self._result

    async def ask(self, state, questions):  # pragma: no cover — never reached here
        raise AssertionError("a stubbed sweep asks the classifier nothing")


def generic_service(settings, jobs, *, typesafe=None, key=True, sweep=None):
    """A service whose `_sweep` records which source each URL was given."""
    if key:
        settings.update(openrouter_api_key="sk-or-test")
    svc = ScrapeService(instances=None, jobs=jobs, settings=settings,
                        store_factory=FakeStore, typesafe=typesafe or FakeTypeSafe())
    svc.swept = {}

    async def recording(job, i, url, source, prog):
        svc.swept[url] = source
        return {"blocked": False, "error": None,
                "data": {"listings": [_listing(f"{i}-1")], "pages_crawled": 1}}

    svc._sweep = sweep or recording
    return svc


class TestWhichSourceReadsAUrl:
    """An adapter when one matches; else the generic reader, with a key; never
    the generic reader for a page on a site that has an adapter."""

    @pytest.mark.asyncio
    async def test_a_site_without_an_adapter_is_read_generically_with_a_key(self, settings, jobs):
        from app.sources.generic import GenericSource

        typesafe = FakeTypeSafe()
        svc = generic_service(settings, jobs, typesafe=typesafe)
        job = svc.start([WC])
        await _drain(svc)

        source = svc.swept[WC]
        assert isinstance(source, GenericSource)
        assert source.url == WC and source.site == "websiteclosers.com"
        assert source._classifier is typesafe, "the shared client, not a copy"
        assert source.override is None
        assert job.source == "generic"
        assert svc.result(job.id).status == "completed"

    @pytest.mark.asyncio
    async def test_each_url_gets_its_own_reader(self, settings, jobs):
        """A GenericSource remembers what it decided on its page; two URLs must
        never share one."""
        svc = generic_service(settings, jobs)
        svc.start([WC, DEALONOMY])
        await _drain(svc)
        assert svc.swept[WC] is not svc.swept[DEALONOMY]
        assert svc.swept[DEALONOMY].site == "dealonomy.com"

    def test_without_a_key_the_refusal_says_where_the_key_goes(self, settings, jobs):
        svc = generic_service(settings, jobs, key=False)
        with pytest.raises(UnsupportedURL) as exc:
            svc.start([WC])
        assert KEY_HINT in str(exc.value)
        assert str(exc.value).startswith(f"Can't read listings from {WC!r}")
        assert jobs.all() == []

    @pytest.mark.parametrize("url", [
        DETAIL, "https://bizbuysell.com/business-opportunity/x/1/", "https://www.bizbuysell.com/",
    ])
    def test_a_site_with_an_adapter_never_falls_through(self, settings, jobs, url):
        """Even with a key: BizBuySell's adapters chose not to read this page, and
        the generic reader would sweep its "similar listings" rail as a search."""
        svc = generic_service(settings, jobs)
        with pytest.raises(UnsupportedURL) as exc:
            svc.start([url])
        message = str(exc.value)
        assert "bizbuysell.com is read by this app's own adapter" in message
        assert "businesses-for-sale" in message and "archive_page" in message
        assert jobs.all() == []

    @pytest.mark.parametrize("url", ["", "not a url", "ftp://example.com/x", "mailto:a@b.co",
                                     "https://"])
    def test_what_is_not_a_web_address_is_refused_even_with_a_key(self, settings, jobs, url):
        svc = generic_service(settings, jobs)
        with pytest.raises(UnsupportedURL) as exc:
            svc.start([url])
        assert "not a web address" in str(exc.value)

    @pytest.mark.asyncio
    async def test_every_refused_url_carries_its_own_reason_into_the_job_error(
        self, settings, jobs,
    ):
        svc = generic_service(settings, jobs, key=False)
        job = svc.start([SERP, DETAIL, "ftp://files.example/x", WC])
        await _drain(svc)

        error = svc.result(job.id).error
        assert "3 of 4 source(s) failed" in error
        assert "www.bizbuysell.com (on bizbuysell.com, only these pages are swept: " in error
        assert "files.example (not a web address — it must start with http:// or https://)" in error
        assert f"www.websiteclosers.com ({sources_hint()})" in error
        assert "not a supported listings page" not in error
        assert list(svc.swept) == [SERP], "refused URLs never reach the browser"

    @pytest.mark.asyncio
    async def test_the_longest_matching_override_in_code_is_handed_to_the_reader(
        self, settings, jobs, monkeypatch,
    ):
        import app.services.scrape as scrape_module
        from app.sources.overrides import SiteOverride

        monkeypatch.setattr(scrape_module, "SITE_OVERRIDES", (
            SiteOverride(match="websiteclosers.com", next_page="none"),
            SiteOverride(match="https://www.websiteclosers.com/businesses-for-sale",
                         next_page="https://www.websiteclosers.com/businesses-for-sale/page/{page}/"),
            SiteOverride(match="dealonomy.com", drop_status=["sold"]),
        ))
        svc = generic_service(settings, jobs)
        svc.start([WC, "https://www.websiteclosers.com/other/", "https://example.org/list"])
        await _drain(svc)

        assert svc.swept[WC].override.next_page.endswith("/page/{page}/")
        assert svc.swept["https://www.websiteclosers.com/other/"].override.next_page == "none"
        assert svc.swept["https://example.org/list"].override is None

    def test_a_generic_sweep_is_labelled_by_its_site(self):
        one = SweepTask(id="g1", source="generic", urls=[WC])
        assert describe(one) == "Listing sweep · websiteclosers.com"
        two = SweepTask(id="g2", source="generic", urls=[WC, DEALONOMY, SERP])
        assert describe(two) == "Listing sweep · websiteclosers.com and 1 more · 3 sources"
        same = SweepTask(id="g3", source="generic",
                         urls=[WC, "https://websiteclosers.com/businesses-for-sale/page/2/"])
        assert describe(same) == "Listing sweep · websiteclosers.com · 2 sources"


class TestPreflight:
    """`submit` checks the classifier's key once, before a job exists, when —
    and only when — some URL needs it."""

    @pytest.mark.asyncio
    async def test_no_generic_url_means_no_check(self, settings, jobs):
        typesafe = FakeTypeSafe()
        svc = generic_service(settings, jobs, typesafe=typesafe)
        job = await svc.submit([SERP, BROKER], max_pages=2)
        await _drain(svc)
        assert typesafe.checks == 0, "a BizBuySell-only call never pays for a check"
        assert job.max_pages == 2 and svc.result(job.id).status == "completed"

    @pytest.mark.asyncio
    async def test_no_key_is_refused_before_any_check(self, settings, jobs):
        typesafe = FakeTypeSafe()
        svc = generic_service(settings, jobs, typesafe=typesafe, key=False)
        with pytest.raises(UnsupportedURL) as exc:
            await svc.submit([WC])
        assert KEY_HINT in str(exc.value)
        assert typesafe.checks == 0 and jobs.all() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_type,message,transient", [
        ("TypeSafeAuthError", "OpenRouter rejected the key (HTTP 401). Check it was copied "
                              "whole.", False),
        ("TypeSafeCreditError", "The key works, but the OpenRouter account is out of credits "
                                "(HTTP 402).", False),
        ("TypeSafeUnavailable", "The Decision API could not answer; try again in a "
                                "few minutes.", True),
    ])
    async def test_a_failed_check_refuses_a_call_that_is_all_generic(
        self, settings, jobs, error_type, message, transient,
    ):
        import app.services.typesafe as typesafe_module
        from app.services.scrape import ClassifierNotReady

        typesafe = FakeTypeSafe(getattr(typesafe_module, error_type)(message))
        svc = generic_service(settings, jobs, typesafe=typesafe)
        with pytest.raises(ClassifierNotReady) as exc:
            await svc.submit([WC, DEALONOMY])
        assert message in str(exc.value), "the check's own words: each is fixed elsewhere"
        assert "these 2 pages" in str(exc.value)
        assert exc.value.transient is transient
        assert typesafe.checks == 1, "one check per call, not one per URL"
        assert jobs.all() == [] and svc.swept == {}

    @pytest.mark.asyncio
    async def test_a_mixed_batch_runs_bizbuysell_and_fails_the_generic_urls(
        self, settings, jobs,
    ):
        from app.services.typesafe import TypeSafeAuthError

        typesafe = FakeTypeSafe(TypeSafeAuthError("OpenRouter rejected the key (HTTP 401)."))
        svc = generic_service(settings, jobs, typesafe=typesafe)
        job = await svc.submit([SERP, WC])
        await _drain(svc)

        assert list(svc.swept) == [SERP], "the generic URL never reached the browser"
        result = svc.result(job.id)
        assert result.status == "completed"
        assert "1 of 2 source(s) failed" in result.error
        assert "www.websiteclosers.com (needs the Decision API" in result.error
        assert "OpenRouter rejected the key (HTTP 401)." in result.error
        assert job.decisions[1]["adapter"] is None

    @pytest.mark.asyncio
    async def test_a_working_check_starts_the_sweep(self, settings, jobs):
        from app.sources.generic import GenericSource

        typesafe = FakeTypeSafe()
        svc = generic_service(settings, jobs, typesafe=typesafe)
        job = await svc.submit([WC, SERP])
        await _drain(svc)
        assert typesafe.checks == 1
        assert isinstance(svc.swept[WC], GenericSource) and SERP in svc.swept
        assert job.source == "generic", "the first readable URL's source"

    @pytest.mark.asyncio
    async def test_submit_keeps_start_s_own_refusals(self, settings, jobs):
        svc = generic_service(settings, jobs)
        with pytest.raises(ValueError):
            await svc.submit([])
        with pytest.raises(NotionNotConfigured):
            await svc.submit([SERP], sync=True)
        assert jobs.all() == []


class _DecidingSource(_PlainSource):
    """A source that reports what it decided, as the generic reader does."""

    name = "generic"

    def __init__(self, pages):
        super().__init__(pages)
        self.decisions = []

    async def cards(self, page):
        self.decisions.append({"page": self.reads + 1, "listing_links": {"by": "jev"}})
        return await super().cards(page)

    def suggested_override(self):
        return {"match": "example.com", "next_page": "none"}


class TestDecisions:
    """How each URL was read, on the job for a person — never in ScrapeResult."""

    @pytest.mark.asyncio
    async def test_one_entry_per_url_in_order(self, settings, jobs):
        pages = [{"page": 1, "listing_links": {"by": "jev", "patterns": ["x/{*}"]}}]
        suggestion = {"match": "websiteclosers.com", "listing_links": ["x/{*}"]}
        checks = [{"page": 1, "ok": True, "kept": 1, "dropped": 0}]

        async def sweep(job, i, url, source, prog):
            data = {"listings": [_listing(str(i))], "pages_crawled": 1, "legibility": checks}
            if url == WC:
                data.update(pages=pages, suggested_override=suggestion)
            return {"blocked": False, "error": None, "data": data}

        svc = generic_service(settings, jobs, sweep=sweep)
        job = svc.start([WC, SERP, DETAIL])
        await _drain(svc)

        stored = jobs.get(job.id)
        assert [d["url"] for d in stored.decisions] == [WC, SERP, DETAIL]
        generic, serp, refused = stored.decisions
        assert generic == {"url": WC, "adapter": "generic", "pages": pages,
                           "legibility": checks, "suggested_override": suggestion}
        assert serp["adapter"] == "bizbuysell_serp" and serp["pages"] == []
        assert serp["suggested_override"] is None and serp["legibility"] == checks
        assert refused["adapter"] is None
        assert "only these pages are swept" in refused["error"]

    @pytest.mark.asyncio
    async def test_decisions_stay_out_of_the_scrape_result(self, settings, jobs):
        from app.models import ScrapeResult

        svc = generic_service(settings, jobs)
        job = svc.start([WC])
        await _drain(svc)
        assert jobs.get(job.id).decisions
        assert "decisions" not in ScrapeResult.model_fields
        assert "decisions" not in svc.result(job.id).model_dump()

    @pytest.mark.asyncio
    async def test_a_sweep_carries_the_source_s_decisions_back(self, settings, jobs, tmp_path):
        from app.sources.base import CardPage

        svc = ScrapeService(instances=None, jobs=jobs, settings=settings)
        source = _DecidingSource([CardPage([_gen(1), _gen(2)]), CardPage([_gen(3)])])
        res, _, _ = await _once(svc, _job(jobs, max_pages=2), source, tmp_path)
        assert [d["page"] for d in res["data"]["pages"]] == [1, 2]
        assert res["data"]["suggested_override"] == {"match": "example.com",
                                                     "next_page": "none"}

        plain, _, _ = await _once(svc, _job(jobs, max_pages=1),
                                  _PlainSource([CardPage([_gen(1)])]), tmp_path)
        assert "pages" not in plain["data"], "an adapter's choices are its code"

    @pytest.mark.asyncio
    async def test_a_failed_attempt_still_reports_what_was_decided(self, settings, jobs):
        source_seen = {}

        async def sweep(job, i, url, source, prog):
            source.decisions.append({"page": 1, "error": "the page could not be read"})
            source_seen["source"] = source
            raise RuntimeError("page crashed")

        svc = generic_service(settings, jobs, sweep=sweep)
        job = svc.start([WC])
        await _drain(svc)

        entry = jobs.get(job.id).decisions[0]
        assert entry["pages"] == [{"page": 1, "error": "the page could not be read"}]
        assert entry["error"] == "page crashed"
        assert entry["suggested_override"] == {"match": "websiteclosers.com"}


# ── triage ───────────────────────────────────────────────────────────────────
#
# A synced sweep that was given a triage_prompt, from its page loop to its last
# write, with every collaborator faked: the page loop runs for real over one
# scripted page of cards, the classifier answers each card's one request (and
# the guard and the detail question) by listing title, the archive's `read`
# serves a page per URL and its `append` records the write, and the store
# serves its index and records each decision. They share one `events` log, so
# the ORDER of writes — the part of this that matters most — is asserted
# directly.

import dataclasses  # noqa: E402
from pathlib import Path  # noqa: E402

from app.services.archive import GUARD_QUESTION, PageRead, guard_state  # noqa: E402
from app.services.triage import CRITERIA, criteria_version  # noqa: E402
from app.services.typesafe import (  # noqa: E402
    Choice,
    TypeSafeAuthError,
    TypeSafeCheck,
    TypeSafeCreditError,
    TypeSafeError,
    TypeSafeNotConfigured,
    TypeSafeUnavailable,
)
from app.stores.base import DedupeIndex, TriageTarget, TriageUnavailable  # noqa: E402
from app.services.typesafe import TYPESAFE_PARALLEL, Noul  # noqa: E402

PROMPT = "Reject restaurants.\nReject if the asking price is below $1M."
OUTAGE = "The Decision API did not answer after 4 attempts (HTTP 503)."
REFUSED = "The Decision API refused this request (HTTP 400: state too large)."


def _tl(n: int, title: str) -> Listing:
    return Listing(
        listing_id=f"t{n}", url=f"https://www.bizbuysell.com/business-opportunity/x/{n}/",
        normalized_url=f"bizbuysell.com/business-opportunity/x/{n}", title=title,
        location="Oakland, CA", asking_price="$2,000,000", cashflow="$500,000",
        source="bizbuysell_serp",
    )


@dataclasses.dataclass(frozen=True)
class _TriageTarget(TriageTarget):
    client: object = None


class TriageStore:
    """The store as a triaging sweep uses it.

    `existing` maps a listing id already in the store to its Bot Triage ("" =
    blank, which makes it backlog — exactly what the real store reports as
    `untriaged`). Every write records which job statuses were on disk at that
    moment, so "never completed before triage ends" is checked where it matters.
    """

    def __init__(self, events, jobs, *, existing=None, fail_writes=(), prepare_error=None):
        self.events = events
        self.jobs = jobs
        self.existing = dict(existing or {})
        self.fail_writes = set(fail_writes)
        self.prepare_error = prepare_error
        self.prepared = 0
        self.index_reads = 0
        self.client = object()
        self.writes: list[dict] = []
        self.statuses: list[str] = []

    async def index(self, db_id, column_map=None):
        self.index_reads += 1
        return DedupeIndex(listing_ids=set(self.existing), decisions_by_id=dict(self.existing))

    async def upsert_new(self, db_id, listings, column_map=None):
        new, untriaged, existing = [], [], 0
        for listing in listings:
            row = listing.model_copy(update={"synced_row_id": f"row-{listing.listing_id}"})
            if listing.listing_id in self.existing:
                existing += 1
                if self.existing[listing.listing_id] == "":
                    untriaged.append(row)
                continue
            new.append(row)
        self.events.append(("upsert",))
        return UpsertResult(new=len(new), existing=existing, db_id=db_id,
                            new_listings=new, untriaged=untriaged)

    async def prepare_triage(self, db_id, column_map=None):
        self.prepared += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return _TriageTarget(db_id=db_id, fields={"bot_triage": "Bot Triage"},
                             client=self.client)

    async def write_triage(self, target, row_id, decision, reason, triaged_at, criteria_version):
        assert target.client is self.client, "the prepared target, every time"
        self.statuses.extend(j.status for j in self.jobs.all())
        if row_id in self.fail_writes:
            raise RuntimeError("Notion refused this request (409 conflict_error)")
        self.writes.append({"row_id": row_id, "decision": decision, "reason": reason,
                            "at": triaged_at, "version": criteria_version})
        self.events.append(("write", row_id, decision))


class TriageClassifier:
    """Answers by listing title: `card`/`detail` give P(review) per title
    (default 0.9), `eligible` P(a business for sale now) (default 0.95),
    `guard` P(real content) (default 0.96). A card's one request is recorded
    as stage "card" when it carries the triage question and "eligible" when it
    does not. A (stage, title) in `down` makes the classifier fail at that
    request with `down_error` (an outage by default); one in `refuse` makes it
    refuse that one request (a plain TypeSafeError, e.g. a 400). Every answer
    yields to the loop first, as a real request does — without that, requests
    started "at once" would each run to completion before the next began."""

    def __init__(self, events, *, card=None, detail=None, guard=None, down=(), check_error=None,
                 refuse=(), down_error=None, eligible=None):
        self.events = events
        self.card = dict(card or {})
        self.detail = dict(detail or {})
        self.guard = dict(guard or {})
        self.eligible = dict(eligible or {})
        self.down = set(down)
        self.refuse = set(refuse)
        self.down_error = down_error or TypeSafeUnavailable(OUTAGE)
        self.check_error = check_error
        self.checks = 0
        self.asked: list[tuple[str, str]] = []
        # Card requests in flight right now, and the most there ever were.
        self.in_flight = self.peak = 0

    async def check(self, key=None, model=None):
        self.checks += 1
        if self.check_error is None:
            return TypeSafeCheck(ok=True, message="Working: jev answered in 90 ms.")
        return TypeSafeCheck(ok=False, message=str(self.check_error), error=self.check_error)

    async def ask(self, state, questions):
        """A card's one request: eligible, and the triage question when asked."""
        stage = "card" if "triage" in questions else "eligible"
        title = state["title"]
        assert "detail_page_text" not in state, "the card, and only the card"
        self.asked.append((stage, title))
        self.events.append(("ask", stage, title))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        if (stage, title) in self.down:
            raise self.down_error
        if (stage, title) in self.refuse:
            raise TypeSafeError(REFUSED)
        out = {"eligible": Noul(self.eligible.get(title, 0.95), "typesafe/jev-test")}
        if "triage" in questions:
            q = questions["triage"]
            out["triage"] = self._choice(self.card, title)
            assert q["criteria"] == CRITERIA and q["instructions"].endswith(PROMPT)
        return out

    def _choice(self, table, title):
        p = table.get(title, 0.9)
        return Choice(choice="REVIEW" if p > 0.5 else "REJECT",
                      probabilities={"REVIEW": p, "REJECT": round(1 - p, 6)},
                      confidence=max(p, 1 - p), model="typesafe/jev-test")

    async def choice(self, state, instructions, criteria):
        # Only the detail stage asks the triage question on its own.
        assert "detail_page_text" in state, "the card stage rides in the card's one request"
        stage = "detail"
        title = state["title"]
        self.asked.append((stage, title))
        self.events.append(("ask", stage, title))
        await asyncio.sleep(0)
        if (stage, title) in self.down:
            raise self.down_error
        if (stage, title) in self.refuse:
            raise TypeSafeError(REFUSED)
        return self._choice(self.detail, title)

    async def noul(self, state, instructions):
        # The guard is its own question: only the page's text, only the guard.
        assert instructions == GUARD_QUESTION
        assert set(state) == {"page_text"}, "not bundled with the card or the triage question"
        title = state["page_text"].splitlines()[0].lstrip("# ")
        self.asked.append(("guard", title))
        self.events.append(("guard", title))
        await asyncio.sleep(0)
        if ("guard", title) in self.down:
            raise self.down_error
        if ("guard", title) in self.refuse:
            raise TypeSafeError(REFUSED)
        return self.guard.get(title, 0.96)


class TriageArchive:
    """The archive's two halves as triage calls them. `pages` maps a URL to its
    PageRead; by default a URL reads as a real page headed by its title."""

    def __init__(self, events, jobs, titles, *, pages=None, fail_appends=(), hang=None):
        self.events = events
        self.jobs = jobs
        self.titles = titles
        self.pages = dict(pages or {})
        self.fail_appends = set(fail_appends)
        self.hang = hang
        self.reads: list[tuple[str, Path, str | None]] = []
        self.appends: list[tuple[object, str, str, str]] = []
        self.summaries: list[str] = []
        self.statuses: list[str] = []

    async def read(self, url, evidence_dir, *, owner=None):
        self.reads.append((url, Path(evidence_dir), owner))
        self.summaries.extend(j.summary for j in self.jobs.all())
        if self.hang is not None:
            self.hang[0].set()
            await self.hang[1].wait()
        if url in self.pages:
            return self.pages[url]
        title = self.titles[url]
        return PageRead(url=url, title=title, markdown=f"# {title}\n\nThe whole listing.",
                        attempts_used=1, evidence_dir=str(evidence_dir))

    async def append(self, client, page_id, markdown, url, heading="Source Content"):
        self.statuses.extend(j.status for j in self.jobs.all())
        if page_id in self.fail_appends:
            raise RuntimeError("Notion accepted 0 block(s) and then refused the rest")
        self.appends.append((client, page_id, markdown, url))
        self.events.append(("append", page_id))


class Rig:
    """A ScrapeService wired for triage, with every collaborator above. Each
    URL is swept by the real page loop over one page holding `listings`."""

    def __init__(self, settings, jobs, listings, *, key=True, notion=True, archive=True,
                 existing=None, fail_writes=(), prepare_error=None, card=None, detail=None,
                 guard=None, down=(), check_error=None, pages=None, fail_appends=(), hang=None,
                 refuse=(), down_error=None, eligible=None, chooses_cards=False):
        if notion:
            settings.update(notion_api_token="ntn_x", notion_db_id="db-1")
        if key:
            settings.update(openrouter_api_key="sk-or-test")
        self.events: list[tuple] = []
        self.jobs = jobs
        self.listings = listings
        self.store = TriageStore(self.events, jobs, existing=existing, fail_writes=fail_writes,
                                 prepare_error=prepare_error)
        self.stores_built = 0

        def factory(_settings):
            self.stores_built += 1
            return self.store

        self.classifier = TriageClassifier(self.events, card=card, detail=detail, guard=guard,
                                           down=down, check_error=check_error, refuse=refuse,
                                           down_error=down_error, eligible=eligible)
        self.archive = TriageArchive(self.events, jobs, {l.url: l.title for l in listings},
                                     pages=pages, fail_appends=fail_appends, hang=hang)
        self.svc = ScrapeService(instances=None, jobs=jobs, settings=settings,
                                 store_factory=factory, typesafe=self.classifier,
                                 archive=self.archive if archive else None)
        self.swept = 0

        async def sweep(job, i, url, source, prog):
            self.swept += 1
            page = _FakePage()
            cards = _PlainSource([CardPage(list(self.listings))])
            if chooses_cards:
                cards.chooses_cards = True
            return await self.svc._sweep_once(_FakeInst(1, page), page, job, url, cards,
                                              self.svc._evidence_dir(job, i))

        self.svc._sweep = sweep

    async def run(self, prompt=PROMPT, urls=(SERP,), sync=True, **limits):
        job = await self.svc.submit(list(urls), sync=sync, triage_prompt=prompt, **limits)
        await _drain(self.svc)
        return self.svc.result(job.id), self.jobs.get(job.id)

    def writes(self) -> dict[str, str]:
        return {w["row_id"]: w["decision"] for w in self.store.writes}

    def asked(self, stage: str) -> list[str]:
        return [title for s, title in self.classifier.asked if s == stage]


class TestOneRequestPerListing:
    """Once a page is down to its array of cards, every question about one card
    is asked together, in one request whose state is that card — and only
    about cards the store does not have, or has with a blank Bot Triage."""

    @pytest.mark.asyncio
    async def test_each_new_card_is_one_request_carrying_both_questions(self, settings, jobs):
        rows = [_tl(1, "Taqueria"), _tl(2, "HVAC Services"), _tl(3, "Plumbing")]
        rig = Rig(settings, jobs, rows, card={"Taqueria": 0.04})
        result, _ = await rig.run()

        assert rig.classifier.asked[:3] == [("card", t) for t in ("Taqueria", "HVAC Services",
                                                                  "Plumbing")]
        assert rig.asked("eligible") == [], "no second request for eligibility"
        assert rig.asked("card") == ["Taqueria", "HVAC Services", "Plumbing"], (
            "the triage phase reads the card decision; it does not ask again")
        assert rig.writes()["row-t1"] == "REJECT"
        assert rig.store.writes[0]["reason"] == "REJECT · P(review)=0.04 · card"
        assert rig.asked("detail") == ["HVAC Services", "Plumbing"]
        assert result.triage.ok and (result.triage.review, result.triage.reject) == (2, 1)

    @pytest.mark.asyncio
    async def test_known_rows_with_a_decision_cost_nothing_and_the_index_is_read_once(
        self, settings, jobs,
    ):
        rows = [_tl(1, "Decided Row"), _tl(2, "Blank Row"), _tl(3, "New Row")]
        rig = Rig(settings, jobs, rows, existing={"t1": "REJECT", "t2": ""})
        result, _ = await rig.run(urls=(SERP, SERP2))

        assert rig.store.index_reads == 1, "once per sweep, before the first page, not per URL"
        assert sorted(rig.asked("card")) == ["Blank Row", "New Row"], (
            "each asked once, though both URLs showed it")
        assert "row-t1" not in rig.writes()
        assert set(rig.writes()) == {"row-t2", "row-t3"}
        assert [r.row_id for r in result.triage.backlog] == ["row-t2"]

    @pytest.mark.asyncio
    async def test_when_the_index_cannot_be_read_every_card_is_asked(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Decided Row"), _tl(2, "New Row")],
                  existing={"t1": "REJECT"})

        async def broken(db_id, column_map=None):
            raise RuntimeError("Notion is having a moment")

        rig.store.index = broken
        result, _ = await rig.run()
        assert rig.asked("card") == ["Decided Row", "New Row"]
        assert rig.writes() == {"row-t2": "REVIEW"}, "the upsert's own read still protects t1"
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_sync_false_asks_eligible_only_and_reads_no_store(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "A"), _tl(2, "B")], existing={"t1": "REJECT"})
        job = await rig.svc.submit([SERP], sync=False)
        await _drain(rig.svc)
        assert rig.classifier.asked == [("eligible", "A"), ("eligible", "B")]
        assert rig.stores_built == 0 and rig.store.index_reads == 0
        assert len(rig.svc.result(job.id).listings) == 2

    @pytest.mark.asyncio
    async def test_without_a_key_nothing_is_asked_at_all(self, settings, jobs):
        """A BizBuySell sweep with no key makes no classifier call, and reads
        no index early: exactly as before the classifier existed."""
        rig = Rig(settings, jobs, [_tl(1, "A"), _tl(2, "B")], key=False)
        job = await rig.svc.submit([SERP], sync=True)
        await _drain(rig.svc)
        assert rig.classifier.asked == [] and rig.store.index_reads == 0
        result = rig.svc.result(job.id)
        assert result.status == "completed" and result.synced.new == 2

    @pytest.mark.asyncio
    async def test_a_generic_card_not_for_sale_is_neither_saved_nor_triaged(
        self, settings, jobs,
    ):
        rows = [_tl(1, "Deli – Sold"), _tl(2, "HVAC"), _tl(3, "Plumbing"), _tl(4, "Bakery")]
        rig = Rig(settings, jobs, rows, eligible={"Deli – Sold": 0.04}, chooses_cards=True)
        result, _ = await rig.run()
        assert result.synced.new == 3 and "row-t1" not in rig.writes()
        assert rig.asked("detail") == ["HVAC", "Plumbing", "Bakery"]
        assert "1 left out as not currently for sale / not listings" in result.summary

    @pytest.mark.asyncio
    async def test_a_bizbuysell_card_judged_not_for_sale_is_still_saved_and_triaged(
        self, settings, jobs,
    ):
        rows = [_tl(1, "Deli – Owner Retiring"), _tl(2, "HVAC"), _tl(3, "Plumbing")]
        rig = Rig(settings, jobs, rows, eligible={"Deli – Owner Retiring": 0.3},
                  card={"Deli – Owner Retiring": 0.02})
        result, _ = await rig.run()
        assert result.synced.new == 3 and rig.writes()["row-t1"] == "REJECT"
        assert "left out" not in result.summary


class TestTriageDecisions:
    @pytest.mark.asyncio
    async def test_a_card_reject_is_written_and_no_page_is_read(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Taqueria")], card={"Taqueria": 0.04})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t1": "REJECT"}
        (write,) = rig.store.writes
        assert write["reason"] == "REJECT · P(review)=0.04 · card"
        assert write["version"] == criteria_version(PROMPT)
        assert write["at"].tzinfo is not None
        assert rig.archive.reads == [] and rig.archive.appends == []
        assert result.status == "completed" and result.error is None
        (listing,) = result.listings
        assert listing.bot_triage == "REJECT" and listing.triage_p_review == pytest.approx(0.04)
        assert result.triage.ok and (result.triage.review, result.triage.reject) == (0, 1)

    @pytest.mark.asyncio
    async def test_review_then_a_detail_reject_writes_reject_and_archives_nothing(
        self, settings, jobs,
    ):
        rig = Rig(settings, jobs, [_tl(1, "Consultancy")], card={"Consultancy": 0.7},
                  detail={"Consultancy": 0.1})
        result, _ = await rig.run()

        assert [q for q in rig.classifier.asked] == [
            ("card", "Consultancy"), ("guard", "Consultancy"), ("detail", "Consultancy")]
        assert rig.archive.appends == [], "a REJECT is never archived"
        assert rig.writes() == {"row-t1": "REJECT"}
        assert rig.store.writes[0]["reason"] == "REJECT · P(review)=0.10 · card + detail page"
        assert result.listings[0].bot_triage == "REJECT"

    @pytest.mark.asyncio
    async def test_review_then_review_archives_the_page_then_writes_review(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Taqueria"), _tl(2, "HVAC Services")],
                  card={"Taqueria": 0.04, "HVAC Services": 0.8}, detail={"HVAC Services": 0.91})
        result, job = await rig.run()

        # The order that makes "every REVIEW has its Source Content" true.
        assert rig.events.index(("append", "row-t2")) < rig.events.index(
            ("write", "row-t2", "REVIEW"))
        ((client, page_id, markdown, url),) = rig.archive.appends
        assert client is rig.store.client, "the one client the whole phase shares"
        assert page_id == "row-t2" and url == _tl(2, "").url
        assert markdown == "# HVAC Services\n\nThe whole listing."
        assert rig.store.writes[-1]["reason"] == "REVIEW · P(review)=0.91 · card + detail page"
        # Read through the archive, under this job's evidence, as this job.
        ((read_url, evidence, owner),) = rig.archive.reads
        assert read_url == _tl(2, "").url
        assert evidence.parts[-2:] == (job.id, "detail-02")
        assert owner == f"job:{job.id}"
        assert result.triage.ok and (result.triage.review, result.triage.reject) == (1, 1)
        assert [l.bot_triage for l in result.listings] == ["REJECT", "REVIEW"]
        assert "triaged: 1 review, 1 reject" in result.summary
        # What each row went through is kept with the run's evidence.
        import json

        record = json.loads((Path(evidence).parent / "triage.json").read_text())
        rows = {r["row_id"]: r for r in record["rows"]}
        assert rows["row-t2"]["guard"] == 0.96 and rows["row-t2"]["final"]["decision"] == "REVIEW"
        assert record["criteria_version"] == criteria_version(PROMPT)

    @pytest.mark.asyncio
    async def test_a_wall_is_review_on_the_card_with_nothing_archived(self, settings, jobs):
        """An NDA or login wall, a removed listing, an error page: decided on the
        card and written, so a site that always gates its pages is not retried
        on every sweep — and nothing that is not the listing gets archived."""
        rig = Rig(settings, jobs, [_tl(1, "Gated Deal")], card={"Gated Deal": 0.88},
                  guard={"Gated Deal": 0.03})
        result, _ = await rig.run()

        assert ("detail", "Gated Deal") not in rig.classifier.asked
        assert rig.archive.appends == []
        assert rig.writes() == {"row-t1": "REVIEW"}
        assert rig.store.writes[0]["reason"] == (
            "REVIEW · P(review)=0.88 · card only — detail page not readable (P=0.03)")
        assert result.triage.ok and result.error is None

    @pytest.mark.asyncio
    async def test_a_page_that_will_not_load_leaves_the_row_blank_and_says_so(
        self, settings, jobs,
    ):
        blocked = _tl(1, "Blocked Listing")
        rig = Rig(settings, jobs, [blocked, _tl(2, "Fine Listing")],
                  pages={blocked.url: PageRead(url=blocked.url, blocked=True, attempts_used=3)})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t2": "REVIEW"}, "the blocked row stays blank"
        (failure,) = result.triage.failures
        assert failure.row_id == "row-t1" and failure.url == blocked.url
        assert "served an anti-bot page" in failure.error
        assert result.triage.undecided == 1 and not result.triage.ok
        assert result.status == "completed"
        assert "Triage couldn't decide 1 row (see triage.failures)" in result.error
        assert "triaged on a later sweep" in result.error
        assert result.listings[0].bot_triage == "" and result.listings[0].triage_p_review is None
        assert "1 left blank for a later sweep" in result.summary


class TestWhichRowsAreTriaged:
    @pytest.mark.asyncio
    async def test_a_row_that_already_has_a_decision_is_never_touched(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Decided Before"), _tl(2, "Brand New")],
                  existing={"t1": "REJECT"})
        result, _ = await rig.run()

        assert all(title != "Decided Before" for _, title in rig.classifier.asked)
        assert "row-t1" not in rig.writes()
        assert rig.writes() == {"row-t2": "REVIEW"}
        assert result.triage.backlog == []

    @pytest.mark.asyncio
    async def test_a_blank_existing_row_is_healed_and_reported_in_the_backlog(
        self, settings, jobs,
    ):
        rig = Rig(settings, jobs, [_tl(1, "Left Blank Earlier"), _tl(2, "Brand New")],
                  existing={"t1": ""}, card={"Left Blank Earlier": 0.02})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t1": "REJECT", "row-t2": "REVIEW"}
        assert [l.synced_row_id for l in result.listings] == ["row-t2"], "backlog is not new"
        (healed,) = result.triage.backlog
        assert (healed.row_id, healed.decision) == ("row-t1", "REJECT")
        assert healed.url == _tl(1, "").url
        assert result.synced.new == 1 and result.synced.existing == 1

    @pytest.mark.asyncio
    async def test_without_a_prompt_only_eligibility_is_asked_and_nothing_is_prepared(
        self, settings, jobs,
    ):
        rig = Rig(settings, jobs, [_tl(1, "Anything"), _tl(9, "Known Blank")],
                  existing={"t9": ""})
        job = await rig.svc.submit([SERP], sync=True)
        await _drain(rig.svc)
        result = rig.svc.result(job.id)

        assert rig.classifier.checks == 0, "a BizBuySell-only call never pays for a check"
        assert rig.classifier.asked == [("eligible", "Anything")], (
            "the new card only, and no triage question: a blank Bot Triage matters to triage")
        assert rig.store.prepared == 0
        assert rig.archive.reads == [] and rig.store.writes == []
        assert result.triage is None and job.triage is None
        assert result.listings[0].bot_triage == "" and "triaged" not in result.summary


class TestTriageFailures:
    @pytest.mark.asyncio
    async def test_an_outage_mid_run_completes_the_job_and_leaves_the_rest_blank(
        self, settings, jobs,
    ):
        """Every card's request is started at once, five at a time; only the
        ones already in flight when the classifier went down are asked — the
        rest never spend the client's retries on an outage. The page is kept,
        the rows are saved, and what was decided before stays decided."""
        titles = ["First", "Second"] + [f"Row {i}" for i in range(3, TYPESAFE_PARALLEL + 6)]
        rows = [_tl(i, t) for i, t in enumerate(titles, 1)]
        rig = Rig(settings, jobs, rows, card={"First": 0.05}, down={("card", "Second")})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t1": "REJECT"}, "what was decided before it went down stays"
        assert rig.asked("card") == titles[:TYPESAFE_PARALLEL], "no request after the outage"
        assert result.synced.new == len(rows), "every card was kept and saved"
        assert rig.archive.reads == []
        assert result.status == "completed", "the scrape and the save succeeded"
        assert result.triage.error == OUTAGE and not result.triage.ok
        assert result.triage.undecided == len(rows) - 1
        assert "Triage stopped before deciding every row" in result.error and OUTAGE in result.error
        assert [l.bot_triage for l in result.listings] == ["REJECT"] + [""] * (len(rows) - 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [
        TypeSafeAuthError("OpenRouter rejected the key (HTTP 401)."),
        TypeSafeCreditError("The OpenRouter account is out of credits (HTTP 402)."),
        TypeSafeUnavailable(OUTAGE),
        TypeSafeNotConfigured("No OpenRouter API key is saved."),
    ])
    async def test_a_classifier_that_cannot_answer_anything_stops_triage(
        self, settings, jobs, error,
    ):
        rows = [_tl(1, "First"), _tl(2, "Second")]
        rig = Rig(settings, jobs, rows, down={("card", "First")}, down_error=error)
        result, _ = await rig.run()
        assert result.triage.error == str(error)
        assert rig.archive.reads == [], "a stop skips the detail stage"
        assert result.triage.failures == []

    @pytest.mark.asyncio
    async def test_a_request_refused_for_one_row_fails_that_row_only(self, settings, jobs):
        """A 400 for one listing (say its state is too large) recurs on every
        sweep while the row stays blank; stopping all of triage for it would
        stall every other row with it."""
        rows = [_tl(1, "Bad Row"), _tl(2, "Good Row"), _tl(3, "Taqueria")]
        rig = Rig(settings, jobs, rows, card={"Taqueria": 0.04},
                  refuse={("card", "Bad Row")})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t2": "REVIEW", "row-t3": "REJECT"}
        assert [p for _, p, _, _ in rig.archive.appends] == ["row-t2"], "the detail stage ran"
        (failure,) = result.triage.failures
        assert failure.row_id == "row-t1"
        assert failure.error == f"The classifier couldn't judge this listing: {REFUSED}"
        assert result.triage.error is None and result.triage.undecided == 1
        assert "Triage couldn't decide 1 row (see triage.failures)" in result.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stage", ["guard", "detail"])
    async def test_a_refusal_at_the_detail_stage_is_that_row_s_too(self, settings, jobs, stage):
        rows = [_tl(1, "Bad Row"), _tl(2, "Good Row")]
        rig = Rig(settings, jobs, rows, refuse={(stage, "Bad Row")})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t2": "REVIEW"}
        (failure,) = result.triage.failures
        assert failure.row_id == "row-t1" and REFUSED in failure.error
        assert result.triage.error is None

    @pytest.mark.asyncio
    async def test_an_outage_at_the_guard_stops_triage_too(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Only")], down={("guard", "Only")})
        result, _ = await rig.run()
        assert rig.store.writes == [] and rig.archive.appends == []
        assert result.status == "completed" and result.triage.error == OUTAGE

    @pytest.mark.asyncio
    async def test_a_failed_write_is_that_row_s_failure_and_the_rest_continue(
        self, settings, jobs,
    ):
        rig = Rig(settings, jobs, [_tl(1, "Will Fail"), _tl(2, "Will Pass")],
                  card={"Will Fail": 0.01, "Will Pass": 0.02}, fail_writes={"row-t1"})
        result, _ = await rig.run()

        assert rig.writes() == {"row-t2": "REJECT"}
        (failure,) = result.triage.failures
        assert failure.row_id == "row-t1"
        assert "Saving the decision (REJECT) to Notion failed" in failure.error
        assert "409 conflict_error" in failure.error
        assert result.status == "completed" and result.triage.reject == 1
        assert result.listings[0].bot_triage == "", "not written, so not reported as written"

    @pytest.mark.asyncio
    async def test_a_failed_archive_leaves_review_unwritten(self, settings, jobs):
        """REVIEW is written only once the page is on the row; a row whose
        archive failed stays blank and a later sweep tries it again."""
        rig = Rig(settings, jobs, [_tl(1, "Good Business")], fail_appends={"row-t1"})
        result, _ = await rig.run()

        assert rig.store.writes == []
        (failure,) = result.triage.failures
        assert "Archiving the detail page into the row failed, so REVIEW was not written" \
            in failure.error


class TestDetailReadCap:
    @pytest.mark.asyncio
    async def test_a_sweep_reads_at_most_25_detail_pages_and_leaves_the_rest_for_later(
        self, settings, jobs,
    ):
        """Each read is about a minute of a pooled browser: a first sweep of a big
        site would otherwise hold the pool for an hour."""
        from app.services.scrape import MAX_DETAIL_READS

        assert MAX_DETAIL_READS == 25
        rows = [_tl(i, f"Business {i}") for i in range(1, MAX_DETAIL_READS + 4)]
        rig = Rig(settings, jobs, rows + [_tl(99, "Taqueria")], card={"Taqueria": 0.04})
        result, job = await rig.run()

        assert len(rig.archive.reads) == MAX_DETAIL_READS
        assert [u for u, _, _ in rig.archive.reads] == [r.url for r in rows[:MAX_DETAIL_READS]]
        assert list(rig.writes().values()).count("REVIEW") == MAX_DETAIL_READS
        assert rig.writes()["row-t99"] == "REJECT", "card REJECTs are not capped"
        t = result.triage
        assert (t.review, t.reject, t.deferred, t.undecided) == (MAX_DETAIL_READS, 1, 3, 3)
        assert not t.ok and t.error is None and t.failures == []
        assert result.status == "completed" and result.error is None, "a limit, not a failure"
        assert "3 left blank for a later sweep (3 past the 25 detail pages this sweep reads)" \
            in result.summary
        assert [l.bot_triage for l in result.listings[-4:-1]] == ["", "", ""]


def _cards_by_url(svc, cards_for: dict[str, list[Listing]]) -> None:
    """Sweep each URL with the real page loop over one page of its own cards."""

    async def sweep(job, i, url, source, prog):
        page = _FakePage()
        return await svc._sweep_once(_FakeInst(1, page), page, job, url,
                                     _PlainSource([CardPage(list(cards_for[url]))]),
                                     svc._evidence_dir(job, i))

    svc._sweep = sweep


async def _until(condition, what: str) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


class _HeldClassifier:
    """Holds every card request until `release` is set, counting what is in
    flight per sweep (the first word of the card's title) and in all."""

    def __init__(self):
        from collections import Counter

        self.release = asyncio.Event()
        self.in_flight: Counter = Counter()
        self.peak: Counter = Counter()

    async def ask(self, state, questions):
        who = state["title"].split()[0]
        for key in (who, "all"):
            self.in_flight[key] += 1
            self.peak[key] = max(self.peak[key], self.in_flight[key])
        try:
            await self.release.wait()
            return {"eligible": Noul(0.95, "typesafe/jev-test")}
        finally:
            for key in (who, "all"):
                self.in_flight[key] -= 1


class TestPerCallLimits:
    """`max_detail_reads` and `classifier_parallel`: two limits a call may set
    for its own sweep. The defaults are what a sweep always did; each value is
    recorded on the task; a value out of range is refused before anything
    starts, never clamped."""

    def test_the_defaults_are_unchanged(self):
        import inspect

        from app.services.scrape import MAX_DETAIL_READS, MAX_DETAIL_READS_CEILING
        from app.services.typesafe import TYPESAFE_MAX_PARALLEL

        assert (MAX_DETAIL_READS, MAX_DETAIL_READS_CEILING) == (25, 200)
        assert (TYPESAFE_PARALLEL, TYPESAFE_MAX_PARALLEL) == (5, 20)
        for method in (ScrapeService.submit, ScrapeService.start):
            params = inspect.signature(method).parameters
            assert params["max_detail_reads"].default == MAX_DETAIL_READS
            assert params["classifier_parallel"].default == TYPESAFE_PARALLEL
        # A record written before the fields existed ran with the defaults.
        old = SweepTask.model_validate({"id": "old"})
        assert (old.max_detail_reads, old.classifier_parallel) == (MAX_DETAIL_READS,
                                                                   TYPESAFE_PARALLEL)

    @pytest.mark.asyncio
    async def test_a_sweep_that_names_neither_runs_and_records_the_defaults(
        self, settings, jobs,
    ):
        rows = [_tl(i, f"Business {i}") for i in range(1, 13)]
        rig = Rig(settings, jobs, rows)
        result, job = await rig.run()
        assert (job.max_detail_reads, job.classifier_parallel) == (25, 5)
        assert rig.classifier.peak == TYPESAFE_PARALLEL
        assert len(rig.archive.reads) == 12 and result.triage.ok

    @pytest.mark.asyncio
    @pytest.mark.parametrize("n", [2, 8])
    async def test_classifier_parallel_is_this_sweep_s_gate(self, settings, jobs, n):
        rows = [_tl(i, f"Business {i}") for i in range(1, 21)]
        rig = Rig(settings, jobs, rows)
        job = await rig.svc.submit([SERP], classifier_parallel=n)
        await _drain(rig.svc)
        assert len(rig.asked("eligible")) == 20
        assert rig.classifier.peak == n
        stored = jobs.get(job.id)
        assert (stored.classifier_parallel, stored.max_detail_reads) == (n, 25)

    @pytest.mark.asyncio
    async def test_classifier_parallel_gates_a_triaging_sweep_too(self, settings, jobs):
        rows = [_tl(i, f"Business {i}") for i in range(1, 21)]
        rig = Rig(settings, jobs, rows)
        result, job = await rig.run(classifier_parallel=8)
        assert len(rig.asked("card")) == 20 and rig.classifier.peak == 8
        assert job.classifier_parallel == 8 and result.triage.ok

    @pytest.mark.asyncio
    async def test_max_detail_reads_reads_that_many_and_leaves_the_rest_for_later(
        self, settings, jobs,
    ):
        rows = [_tl(i, f"Business {i}") for i in range(1, 5)]
        rig = Rig(settings, jobs, rows)
        result, job = await rig.run(max_detail_reads=2)

        assert [u for u, _, _ in rig.archive.reads] == [r.url for r in rows[:2]]
        assert rig.writes() == {"row-t1": "REVIEW", "row-t2": "REVIEW"}
        t = result.triage
        assert (t.review, t.reject, t.deferred, t.undecided) == (2, 0, 2, 2)
        assert not t.ok and t.error is None and t.failures == []
        assert result.status == "completed" and result.error is None, "a limit, not a failure"
        assert "2 left blank for a later sweep (2 past the 2 detail pages this sweep reads)" \
            in result.summary
        assert [l.bot_triage for l in result.listings] == ["REVIEW", "REVIEW", "", ""]
        assert (job.max_detail_reads, job.classifier_parallel) == (2, TYPESAFE_PARALLEL)

    @pytest.mark.asyncio
    async def test_a_higher_max_detail_reads_reads_past_25(self, settings, jobs):
        rows = [_tl(i, f"Business {i}") for i in range(1, 29)]
        rig = Rig(settings, jobs, rows)
        result, job = await rig.run(max_detail_reads=30)
        assert len(rig.archive.reads) == 28
        assert result.triage.deferred == 0 and result.triage.ok and result.triage.review == 28
        assert job.max_detail_reads == 30

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name,value,bounds", [
        ("max_detail_reads", 0, "from 1 to 200"),
        ("max_detail_reads", 201, "from 1 to 200"),
        ("max_detail_reads", -3, "from 1 to 200"),
        ("classifier_parallel", 0, "from 1 to 20"),
        ("classifier_parallel", 21, "from 1 to 20"),
    ])
    async def test_out_of_range_is_refused_before_anything_starts(
        self, settings, jobs, name, value, bounds,
    ):
        rig = Rig(settings, jobs, [_tl(1, "Only")])
        with pytest.raises(ValueError) as exc:
            await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT, **{name: value})
        message = str(exc.value)
        assert f"{name}={value} is out of range" in message and bounds in message
        with pytest.raises(ValueError, match=f"{name}={value} is out of range"):
            rig.svc.start([SERP], **{name: value})
        assert jobs.all() == [], "no job was written"
        assert rig.classifier.checks == 0 and rig.store.prepared == 0, "refused first, for free"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [2.5, "8", True, None])
    async def test_a_value_that_is_not_a_whole_number_is_refused(self, settings, jobs, value):
        rig = Rig(settings, jobs, [_tl(1, "Only")])
        with pytest.raises(ValueError, match="classifier_parallel must be a whole number"):
            await rig.svc.submit([SERP], classifier_parallel=value)
        assert jobs.all() == []

    @pytest.mark.asyncio
    async def test_the_bounds_themselves_are_allowed(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Only")])
        for reads, parallel in ((1, 1), (200, 20)):
            job = await rig.svc.submit([SERP], max_detail_reads=reads,
                                       classifier_parallel=parallel)
            assert (job.max_detail_reads, job.classifier_parallel) == (reads, parallel)
        await _drain(rig.svc)

    @pytest.mark.asyncio
    async def test_two_sweeps_each_get_their_own_gate(self, settings, jobs):
        """A per-sweep limit is per sweep: two running at once with
        classifier_parallel=2 each have four requests in flight — two of each,
        and no more — rather than sharing one gate of two."""
        settings.update(openrouter_api_key="sk-or-test")
        held = _HeldClassifier()
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings, typesafe=held)
        _cards_by_url(svc, {SERP: [_tl(i, f"A {i}") for i in range(1, 11)],
                            SERP2: [_tl(i, f"B {i}") for i in range(101, 111)]})

        await svc.submit([SERP], classifier_parallel=2)
        await svc.submit([SERP2], classifier_parallel=2)
        await _until(lambda: held.in_flight["all"] == 4, "four requests in flight")
        await asyncio.sleep(0.05)
        assert (held.in_flight["A"], held.in_flight["B"], held.in_flight["all"]) == (2, 2, 4)
        held.release.set()
        await _drain(svc)
        assert (held.peak["A"], held.peak["B"], held.peak["all"]) == (2, 2, 4)

    @pytest.mark.asyncio
    async def test_the_process_wide_ceiling_bounds_two_sweeps(self, settings, jobs):
        """The real client, one per process, with two sweeps at 15 each: the
        client's ceiling holds the total to 20, both sweeps among them."""
        import json

        import httpx
        import respx
        from collections import Counter

        from app.services.typesafe import API, TYPESAFE_MAX_PARALLEL, TypeSafeClient

        release = asyncio.Event()
        in_flight: Counter = Counter()
        peak: Counter = Counter()

        async def answer(request):
            who = json.loads(request.content)["state"]["title"].split()[0]
            for key in (who, "all"):
                in_flight[key] += 1
                peak[key] = max(peak[key], in_flight[key])
            try:
                await release.wait()
            finally:
                for key in (who, "all"):
                    in_flight[key] -= 1
            return httpx.Response(200, json={
                "model": "typesafe/jev-test",
                "answers": {"eligible": {"type": "noul", "noul": 0.95}}})

        settings.update(openrouter_api_key="sk-or-test")
        client = TypeSafeClient(lambda: "sk-or-test", lambda: "jev-latest")
        svc = ScrapeService(instances=None, jobs=jobs, settings=settings, typesafe=client)
        _cards_by_url(svc, {SERP: [_tl(i, f"A {i}") for i in range(1, 31)],
                            SERP2: [_tl(i, f"B {i}") for i in range(101, 131)]})

        with respx.mock:
            route = respx.post(API).mock(side_effect=answer)
            await svc.submit([SERP], classifier_parallel=15)
            await svc.submit([SERP2], classifier_parallel=15)
            await _until(lambda: in_flight["all"] == TYPESAFE_MAX_PARALLEL,
                         "the ceiling reached")
            await asyncio.sleep(0.05)
            assert in_flight["all"] == TYPESAFE_MAX_PARALLEL == 20, "never past the ceiling"
            assert 0 < in_flight["A"] <= 15 and 0 < in_flight["B"] <= 15
            release.set()
            await _drain(svc)

        assert route.call_count == 60
        assert peak["all"] == 20 and peak["A"] <= 15 and peak["B"] <= 15


class TestOverlappingSweeps:
    @pytest.mark.asyncio
    async def test_a_row_another_sweep_is_triaging_is_left_to_it(self, settings, jobs):
        """Two sweeps that overlap both see a blank row; the second leaves it to
        the first rather than judging, archiving and writing it twice."""
        reading, release = asyncio.Event(), asyncio.Event()
        rig = Rig(settings, jobs, [_tl(1, "Shared Row")], existing={"t1": ""},
                  hang=(reading, release))
        first = await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT)
        await asyncio.wait_for(reading.wait(), 2)
        assert rig.svc._triaging == {"row-t1"}

        second = await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT)
        for _ in range(200):
            if jobs.get(second.id).status != "working":
                break
            await asyncio.sleep(0.01)
        other = rig.svc.result(second.id)
        assert other.status == "completed" and other.error is None
        assert other.triage.in_flight == 1 and other.triage.undecided == 0 and other.triage.ok
        assert other.triage.backlog == []
        assert "1 left to another sweep triaging them" in other.summary
        # Each sweep's page loop asked its card's one request (the row was
        # blank when each started); only the first judges its page and writes.
        assert rig.asked("card") == ["Shared Row", "Shared Row"]
        assert [u for u, _, _ in rig.archive.reads] == [_tl(1, "").url]

        release.set()
        await _drain(rig.svc)
        assert rig.writes() == {"row-t1": "REVIEW"} and len(rig.store.writes) == 1
        assert len(rig.archive.appends) == 1
        assert rig.svc.result(first.id).triage.in_flight == 0
        assert rig.svc._triaging == set(), "released when the phase ends"

    @pytest.mark.asyncio
    async def test_a_row_is_released_when_its_phase_is_cancelled(self, settings, jobs):
        reading, release = asyncio.Event(), asyncio.Event()
        rig = Rig(settings, jobs, [_tl(1, "Taqueria")], hang=(reading, release))
        await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT)
        await asyncio.wait_for(reading.wait(), 2)
        (task,) = rig.svc._running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.svc._triaging == set()


class TestTriageLifecycle:
    @pytest.mark.asyncio
    async def test_the_job_is_never_completed_on_disk_until_triage_ends(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Taqueria"), _tl(2, "HVAC"), _tl(3, "Plumbing")],
                  card={"Taqueria": 0.04})
        result, _ = await rig.run()

        assert rig.store.statuses and set(rig.store.statuses) == {"working"}
        assert rig.archive.statuses and set(rig.archive.statuses) == {"working"}
        assert result.status == "completed"
        # And while it worked, the record said what it was doing.
        assert "Reading 2 detail pages…" in rig.archive.summaries
        assert result.triage.ok

    @pytest.mark.asyncio
    async def test_the_phase_is_saved_where_a_poll_can_see_it(self, settings, jobs):
        seen: list[str] = []
        rig = Rig(settings, jobs, [_tl(1, "Brand New"), _tl(2, "Left Blank")],
                  existing={"t2": ""}, card={"Brand New": 0.01, "Left Blank": 0.01})

        async def write(target, row_id, *args):
            seen.extend(j.summary for j in jobs.all())
            rig.store.writes.append({"row_id": row_id, "decision": args[0]})

        rig.store.write_triage = write
        await rig.run()
        assert "Triaging 1 new listing and 1 earlier row…" in seen

    @pytest.mark.asyncio
    async def test_the_saved_rows_are_on_the_record_before_triage_starts(self, settings, jobs):
        on_disk = []
        rig = Rig(settings, jobs, [_tl(1, "Taqueria")], card={"Taqueria": 0.04})
        real = rig.store.write_triage

        async def write(*args):
            job = jobs.all()[0]
            on_disk.append((job.status, job.synced, [l.synced_row_id for l in job.listings]))
            await real(*args)

        rig.store.write_triage = write
        await rig.run()
        ((status, synced, row_ids),) = on_disk
        assert status == "working" and synced is not None and synced.new == 1
        assert row_ids == ["row-t1"]

    @pytest.mark.asyncio
    async def test_a_cancelled_sweep_records_what_it_saved(self, settings, jobs):
        reading, release = asyncio.Event(), asyncio.Event()
        rig = Rig(settings, jobs, [_tl(1, "Taqueria"), _tl(2, "HVAC")],
                  card={"Taqueria": 0.04}, hang=(reading, release))
        job = await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT)
        await asyncio.wait_for(reading.wait(), 2)

        (task,) = rig.svc._running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        record = jobs.get(job.id)
        assert record.status == "failed", "not left 'working' for a poll to wait on forever"
        assert "Saved 2 new rows; triage was interrupted" in record.error
        assert "rows without a decision are triaged on a later sweep" in record.error
        assert record.triage.reject == 1 and record.triage.undecided == 1
        assert record.triage.error and not record.triage.ok
        assert record.listings[0].bot_triage == "REJECT"

    @pytest.mark.asyncio
    async def test_the_preflight_s_store_saves_and_triages(self, settings, jobs):
        """Prepared once, in submit; the run reuses that store (one client)."""
        rig = Rig(settings, jobs, [_tl(1, "Anything")])
        await rig.run()
        assert rig.stores_built == 1 and rig.store.prepared == 1
        assert rig.store.index_reads == 1, "the index before the first page, on the same store"
        assert [e for e in rig.events if e[0] != "ask"][0] == ("upsert",)

    @pytest.mark.asyncio
    async def test_start_with_a_prompt_prepares_the_target_in_the_run(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Taqueria")], card={"Taqueria": 0.04})
        job = rig.svc.start([SERP], sync=True, triage_prompt=PROMPT)
        assert job.triage is not None and job.triage.criteria_version == criteria_version(PROMPT)
        await _drain(rig.svc)
        assert rig.store.prepared == 1 and rig.writes() == {"row-t1": "REJECT"}

    @pytest.mark.asyncio
    async def test_a_target_the_run_cannot_prepare_leaves_every_row_blank(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "Taqueria")],
                  prepare_error=TriageUnavailable("This database has no 'Bot Triage' column."))
        rig.svc.start([SERP], sync=True, triage_prompt=PROMPT)
        await _drain(rig.svc)
        result = rig.svc.result(jobs.all()[0].id)
        assert result.status == "completed"
        assert rig.asked("card") == [], "no triage question with nowhere to record the answer"
        assert rig.asked("eligible") == ["Taqueria"]
        assert "no 'Bot Triage' column" in result.triage.error
        assert result.triage.undecided == 1

    @pytest.mark.asyncio
    async def test_a_sweep_that_saved_nothing_says_triage_did_not_run(self, settings, jobs):
        rig = Rig(settings, jobs, [])

        async def blocked(job, i, url, source, prog):
            return {"blocked": True, "error": None, "data": {"listings": [], "pages_crawled": 1}}

        rig.svc._sweep = blocked
        result, _ = await rig.run()
        assert result.status == "failed"
        assert result.triage.error == "Nothing was triaged: every source failed."


class TestTriageRefusals:
    """Everything that would leave a triaging sweep unable to decide is said
    before the job exists — including for a BizBuySell-only batch."""

    async def _refused(self, rig, **kw):
        from app.services.scrape import TriageNotConfigured

        kw.setdefault("sync", True)
        kw.setdefault("triage_prompt", PROMPT)
        with pytest.raises(TriageNotConfigured) as exc:
            await rig.svc.submit([SERP], **kw)
        assert rig.jobs.all() == [] and rig.swept == 0
        assert rig.store.writes == []
        return exc.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt", ["", "   \n\t "])
    @pytest.mark.parametrize("sync", [True, False])
    async def test_a_blank_prompt_is_no_prompt(self, settings, jobs, prompt, sync):
        """Agents fill optional string parameters with "" as often as they leave
        them out: either way no triage was asked for, so nothing is refused —
        not even sync=false, which triage alone would need."""
        rig = Rig(settings, jobs, [_tl(1, "x")])
        result, job = await rig.run(prompt=prompt, sync=sync)
        assert result.status == "completed" and result.error is None
        assert result.triage is None and job.triage is None
        assert rig.classifier.checks == 0 and rig.asked("card") == []
        assert rig.store.prepared == 0 and rig.store.writes == []

    @pytest.mark.asyncio
    async def test_start_treats_a_blank_prompt_as_none_too(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")])
        job = rig.svc.start([SERP], sync=False, triage_prompt="  ")
        await _drain(rig.svc)
        assert job.triage is None and rig.svc.result(job.id).status == "completed"

    @pytest.mark.asyncio
    async def test_sync_false(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")])
        exc = await self._refused(rig, sync=False)
        assert "needs sync=true" in str(exc)
        assert rig.classifier.checks == 0

    @pytest.mark.asyncio
    async def test_no_notion_database_is_the_sync_refusal(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")], notion=False)
        with pytest.raises(NotionNotConfigured) as exc:
            await rig.svc.submit([SERP], sync=True, triage_prompt=PROMPT)
        assert "no Notion database is set up" in str(exc.value)

    @pytest.mark.asyncio
    async def test_no_classifier_key(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")], key=False)
        exc = await self._refused(rig)
        assert "no OpenRouter key is saved" in str(exc)
        assert "Add one under Settings → Decision API" in str(exc)
        assert rig.classifier.checks == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error,transient", [
        (TypeSafeAuthError("OpenRouter rejected the key (HTTP 401)."), False),
        (TypeSafeCreditError("The OpenRouter account is out of credits (HTTP 402)."), False),
        (TypeSafeUnavailable("The Decision API could not answer."), True),
    ])
    async def test_a_key_that_fails_its_check_even_for_bizbuysell_only(
        self, settings, jobs, error, transient,
    ):
        """Decision 5: the whole call, BizBuySell or not — otherwise it would
        save rows and leave every one of them undecided."""
        rig = Rig(settings, jobs, [_tl(1, "x")], check_error=error)
        exc = await self._refused(rig)
        assert str(error) in str(exc) and "Can't start this sweep with triage" in str(exc)
        assert exc.transient is transient and exc.cause is error
        assert rig.classifier.checks == 1 and rig.store.prepared == 0

    @pytest.mark.asyncio
    async def test_a_database_with_nowhere_to_record_a_decision(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")], prepare_error=TriageUnavailable(
            "This database has no 'Bot Triage' column, so there is nowhere to record triage "
            "decisions."))
        exc = await self._refused(rig)
        assert str(exc).startswith("Can't triage into your Notion database: This database has "
                                   "no 'Bot Triage' column")
        assert not exc.transient

    @pytest.mark.asyncio
    async def test_a_database_that_cannot_be_read(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")],
                  prepare_error=RuntimeError("Notion rejected the API token."))
        exc = await self._refused(rig)
        assert "Notion rejected the API token." in str(exc)

    @pytest.mark.asyncio
    async def test_a_server_without_its_page_reader(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")], archive=False)
        exc = await self._refused(rig)
        assert "detail pages" in str(exc)

    @pytest.mark.asyncio
    async def test_start_s_own_refusals_come_first_and_cost_nothing(self, settings, jobs):
        rig = Rig(settings, jobs, [_tl(1, "x")])
        with pytest.raises(ValueError):
            await rig.svc.submit([], sync=True, triage_prompt=PROMPT)
        with pytest.raises(UnsupportedURL):
            await rig.svc.submit([DETAIL], sync=True, triage_prompt=PROMPT)
        assert rig.classifier.checks == 0 and rig.store.prepared == 0

    def test_it_is_a_notion_refusal_to_every_facade(self):
        from app.mcp_server import REFUSALS
        from app.services.scrape import TriageNotConfigured

        assert issubclass(TriageNotConfigured, NotionNotConfigured)
        assert issubclass(TriageNotConfigured, REFUSALS)
