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

from app.models import Job, Listing
from app.services.jobs import JobStore
from app.services.profiles import ProfileStore
from app.services.scrape import (
    _SCRAPING_SUMMARY,
    _WAITING_SUMMARY,
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
    stamped with a `page-<id>` page id — the neutral row id the real Notion store
    reads off the /pages response.
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
            new_listings.append(listing.model_copy(update={"page_id": f"page-{listing.listing_id}"}))
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
    def test_an_unsupported_url_never_creates_a_job(self, settings, jobs):
        """A job id for a URL we cannot read would be a promise of a result that
        can never come."""
        with pytest.raises(UnsupportedURL):
            service(settings, jobs).start(["https://abc.xyz/investor/"])
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
    async def test_db_id_overrides_the_configured_database(self, settings, jobs):
        settings.update(notion_api_token="ntn_x", notion_db_id="db-configured")
        store = FakeStore()
        svc = service(settings, jobs, store=lambda s: store)

        svc.start([SERP], sync=True, db_id="db-override")
        await _drain(svc)

        assert store.upserts[0][0] == "db-override"

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
        svc.start([SERP], sync=True, db_id="db-other")
        await _drain(svc)

        assert store.column_maps[0] is None


def _sweep_returning(listings):
    """A `_sweep` stub that yields a fixed set of listings from a single URL."""
    async def sweep(job, i, url, source, prog):
        return {"blocked": False, "error": None,
                "data": {"listings": list(listings), "pages_crawled": 1}}
    return sweep


class TestSyncReturnsNewWithPageIds:
    """sync=true narrows the collected `listings` to just the rows this sweep
    inserted, each stamped with the store page id — so an agent can archive_page
    the fresh rows straight off the result. Already-known rows stay counted in
    `synced.existing` but drop out of `listings`. sync=false is unchanged."""

    @pytest.mark.asyncio
    async def test_sync_true_returns_only_new_listings_each_with_a_page_id(self, settings, jobs):
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
        assert all(l.page_id for l in result.listings), "each new listing carries a page id"
        assert {l.page_id for l in result.listings} == {"page-fresh-a", "page-fresh-b"}
        assert result.synced.new == 2
        assert result.synced.existing == 1, "the known row is counted, not returned"

    @pytest.mark.asyncio
    async def test_sync_false_returns_all_found_with_empty_page_id(self, settings, jobs):
        found = [_listing("a"), _listing("b")]
        svc = service(settings, jobs, sweep=_sweep_returning(found))
        job = svc.start([SERP], sync=False)
        await _drain(svc)

        result = svc.result(job.id)
        assert sorted(l.listing_id for l in result.listings) == ["a", "b"], "all found"
        assert all(l.page_id == "" for l in result.listings), "no store, so no page id"
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
    async def test_multi_url_sync_returns_the_merged_new_set_with_ids(self, settings, jobs):
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
        assert all(l.page_id for l in result.listings), "each merged new row carries a page id"
        assert result.synced.new == 3


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
        with pytest.raises(UnsupportedURL):
            service(settings, jobs).start(["https://abc.xyz/a", "https://abc.xyz/b"])
        assert jobs.all() == []

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


class TestJobLabel:
    """`describe(job)` is the sweep task's own label formatter — verb · source
    label · count — colocated with the sweep and resolving the source's human
    name via the source registry, not a string baked into the template."""

    def test_multi_source_names_the_source_and_count(self):
        job = Job(id="a", source="bizbuysell_serp", urls=[SERP, SERP2, BROKER])
        assert describe(job) == "Listing sweep · BizBuySell · 3 sources"

    def test_single_source_drops_the_count(self):
        job = Job(id="b", source="bizbuysell_serp", urls=[SERP])
        # No "1 sources" noise for a single-URL sweep.
        assert describe(job) == "Listing sweep · BizBuySell"
        assert "sources" not in describe(job)

    def test_broker_source_uses_its_own_label(self):
        job = Job(id="c", source="bizbuysell_broker", urls=[BROKER])
        assert describe(job) == "Listing sweep · BizBuySell broker"

    def test_unknown_source_falls_back_to_the_raw_id(self):
        """An old job whose source was retired must still render, not break — the
        registry returns the raw id when no adapter owns it."""
        job = Job(id="d", source="craigslist_biz", urls=[SERP, SERP2])
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
