"""Choosing an adapter by URL, and the dedupe keys it produces.

`normalize_url` is a **dedupe key**, so these are not cosmetic string tests: two
sightings of one listing that normalize differently become two rows in someone's
database, and nobody notices until it is full of duplicates.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import sources
from app.sources import UnsupportedURL
from app.sources.bizbuysell import JS_CARDS, BizBuySellBroker, BizBuySellSerp, listing_id_from
from app.sources.urls import canonical_url, normalize_url

MURALI = "https://www.bizbuysell.com/business-broker/murali-barathi/krea-business/41243/"
RICK = "https://www.bizbuysell.com/business-broker/rick-teh-emba-cbi/accel-business-advisors/36034/"

BAY_AREA = (
    "https://www.bizbuysell.com/california/san-francisco-bay-area-businesses-for-sale/"
    "?q=Z2lmcm9tPTc1MDAwMCZnaXRvPTEwMDAwMDAwJmx0PTMwLDQwLDgwJnBmcm9tPTc1MDAwMA%3D%3D"
)
SACRAMENTO = "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"


class TestNormalization:
    def test_the_plan_fixture(self):
        """Verbatim from the plan: query, fragment, www, and trailing slash all go."""
        url = "https://www.bizbuysell.com/business-opportunity/foo/2484566/?utm_source=x#section"
        assert normalize_url(url) == "bizbuysell.com/business-opportunity/foo/2484566"
        assert listing_id_from(url) == "2484566"

    @pytest.mark.parametrize(
        "variant",
        [
            "https://www.bizbuysell.com/business-opportunity/foo/2484566/",
            "http://www.bizbuysell.com/business-opportunity/foo/2484566",
            "https://bizbuysell.com/business-opportunity/foo/2484566/?utm_source=newsletter",
            "https://BizBuySell.com/business-opportunity/foo/2484566#details",
            "bizbuysell.com/business-opportunity/foo/2484566",
        ],
    )
    def test_every_way_of_writing_one_listing_agrees(self, variant):
        """Each of these is the same listing. Disagreeing here means duplicate rows."""
        assert normalize_url(variant) == "bizbuysell.com/business-opportunity/foo/2484566"

    def test_two_different_listings_do_not_collide(self):
        a = normalize_url("https://www.bizbuysell.com/business-opportunity/foo/2484566/")
        b = normalize_url("https://www.bizbuysell.com/business-opportunity/foo/2484567/")
        assert a != b

    @pytest.mark.parametrize("empty", [None, "", "   ", "https://"])
    def test_nothing_normalizes_to_nothing(self, empty):
        assert normalize_url(empty) is None

    def test_canonical_url_keeps_the_address_but_drops_the_baggage(self):
        assert (
            canonical_url("https://www.bizbuysell.com/business-opportunity/foo/2484566/?utm=x#a")
            == "https://www.bizbuysell.com/business-opportunity/foo/2484566/"
        )


class TestListingId:
    def test_from_a_profile_query(self):
        assert listing_id_from("https://www.bizbuysell.com/listings/Profile/?q=2484566") == "2484566"

    def test_a_non_bizbuysell_url_has_no_bizbuysell_id(self):
        assert listing_id_from("https://quietlight.com/listings/18829322/") is None

    def test_a_page_without_an_id_has_none(self):
        assert listing_id_from(SACRAMENTO) is None


class TestSourceLabels:
    """`name` is the machine id; `label` is the human display name. `label_for`
    resolves a stored id to the owning adapter's label, so labels live with the
    source and unknown ids fall back to the id itself."""

    def test_each_adapter_declares_a_distinct_id_and_label(self):
        serp, broker = BizBuySellSerp(), BizBuySellBroker()
        assert serp.name == "bizbuysell_serp" and serp.label == "BizBuySell"
        assert broker.name == "bizbuysell_broker" and broker.label == "BizBuySell broker"

    def test_label_for_resolves_a_known_id(self):
        assert sources.label_for("bizbuysell_serp") == "BizBuySell"
        assert sources.label_for("bizbuysell_broker") == "BizBuySell broker"

    def test_label_for_falls_back_to_the_raw_id_when_unknown(self):
        # A retired/foreign source id must render as itself, never raise.
        assert sources.label_for("craigslist_biz") == "craigslist_biz"
        assert sources.label_for("") == ""


class TestDispatch:
    @pytest.mark.parametrize("url", [BAY_AREA, SACRAMENTO])
    def test_a_serp_selects_the_bizbuysell_adapter(self, url):
        assert sources.for_url(url).name == "bizbuysell_serp"

    @pytest.mark.parametrize(
        "url",
        [
            "https://abc.xyz/investor/",
            "https://example.com/businesses-for-sale/",
            "https://quietlight.com/listings/18829322/",
            "",
            "not a url",
        ],
    )
    def test_an_unsupported_url_is_a_hard_error(self, url):
        """Never a best-effort attempt: a generic scrape of a page we do not
        understand returns an empty result that looks exactly like "no listings
        matched", and an agent would report that as fact."""
        with pytest.raises(UnsupportedURL):
            sources.for_url(url)

    def test_the_error_names_what_is_supported(self):
        """The plan's case. The message has to leave the reader able to act."""
        with pytest.raises(UnsupportedURL) as exc:
            sources.for_url("https://abc.xyz/investor/")
        message = str(exc.value)
        assert "abc.xyz/investor" in message
        assert "bizbuysell.com" in message
        assert "businesses-for-sale" in message
        # And point at the tool that does handle an arbitrary page.
        assert "archive_page" in message

    def test_a_listing_page_is_not_a_search_page(self):
        """A BizBuySell detail URL is the right site and the wrong job. Sweeping
        it would find only the cards it links to, which is not what was asked."""
        assert not BizBuySellSerp().matches(
            "https://www.bizbuysell.com/business-opportunity/premier-restoration/2515728/"
        )

    def test_a_broker_profile_is_not_a_search_page(self):
        """A broker profile is the right site and a different job — it is handled
        by BizBuySellBroker, not swept as a search feed."""
        broker = "https://www.bizbuysell.com/business-broker/andrew-rogerson/rogerson/20770/"
        assert not BizBuySellSerp().matches(broker)
        assert sources.for_url(broker).name == "bizbuysell_broker"

    def test_a_lookalike_domain_does_not_match(self):
        assert not BizBuySellSerp().matches("https://bizbuysell.com.evil.example/x-businesses-for-sale/")


class TestPaging:
    def test_page_one_has_no_segment(self):
        assert BizBuySellSerp().page_url(SACRAMENTO, 1) == SACRAMENTO

    def test_later_pages_use_a_path_segment(self):
        assert BizBuySellSerp().page_url(SACRAMENTO, 3) == (
            "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/3/"
        )

    def test_the_query_survives_paging(self):
        """The filters live in ?q=. Losing them would silently sweep every
        listing in the region instead of the ones that were asked for."""
        assert "?q=Z2lmcm9t" in BizBuySellSerp().page_url(BAY_AREA, 2)

    def test_paging_a_url_that_is_already_paged(self):
        """Idempotent: page 2 of "page 3 of X" is page 2 of X, not page 3/2."""
        paged = BizBuySellSerp().page_url(SACRAMENTO, 3)
        assert BizBuySellSerp().page_url(paged, 2) == (
            "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/2/"
        )


class TestBrokerDispatch:
    @pytest.mark.parametrize("url", [MURALI, RICK])
    def test_a_broker_profile_selects_the_broker_adapter(self, url):
        assert sources.for_url(url).name == "bizbuysell_broker"

    @pytest.mark.parametrize("url", [MURALI, RICK])
    def test_a_broker_profile_is_never_the_search_adapter(self, url):
        """The two paths are disjoint, so exactly one adapter claims each URL —
        routing a broker URL to the SERP source would sweep only the handful of
        cards it links to, not the broker's for-sale book."""
        assert not BizBuySellSerp().matches(url)
        assert BizBuySellBroker().matches(url)

    @pytest.mark.parametrize("url", [SACRAMENTO, BAY_AREA])
    def test_a_search_page_is_never_the_broker_adapter(self, url):
        assert not BizBuySellBroker().matches(url)

    def test_a_listing_page_is_not_a_broker_profile(self):
        assert not BizBuySellBroker().matches(
            "https://www.bizbuysell.com/business-opportunity/premier-restoration/2515728/"
        )

    def test_a_broker_landing_without_an_id_does_not_match(self):
        """The profile pattern needs slug/company/id; a bare directory URL is not
        a specific broker and must not be swept as one."""
        assert not BizBuySellBroker().matches("https://www.bizbuysell.com/business-broker/")

    def test_a_lookalike_domain_does_not_match(self):
        assert not BizBuySellBroker().matches(
            "https://www.bizbuysell.com.evil.example/business-broker/x/y/1/"
        )


class TestBrokerPaging:
    def test_page_one_carries_the_paging_params(self):
        assert BizBuySellBroker().page_url(MURALI, 1) == (
            "https://www.bizbuysell.com/business-broker/murali-barathi/krea-business/41243/"
            "?bp_cfspg=1&bplt=10#bdProfileTabs"
        )

    def test_later_pages_bump_bp_cfspg(self):
        assert BizBuySellBroker().page_url(MURALI, 2) == (
            "https://www.bizbuysell.com/business-broker/murali-barathi/krea-business/41243/"
            "?bp_cfspg=2&bplt=10#bdProfileTabs"
        )

    def test_paging_is_idempotent_on_an_already_paged_url(self):
        """page 3 of "page 2 of X" is page 3 of X — the stale bp_cfspg is dropped,
        not stacked, and the page size is not doubled."""
        paged = BizBuySellBroker().page_url(MURALI, 2)
        assert BizBuySellBroker().page_url(paged, 3) == (
            "https://www.bizbuysell.com/business-broker/murali-barathi/krea-business/41243/"
            "?bp_cfspg=3&bplt=10#bdProfileTabs"
        )


class _FakeLocator:
    def __init__(self, count: int, on_click=None):
        self._count = count
        self._on_click = on_click

    async def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    async def click(self, **kwargs) -> None:
        if self._on_click:
            self._on_click()


class _FakePage:
    """A page whose `evaluate` hands back canned JS_BROKER output.

    The real JS is exercised against a browser in TestBrokerExtraction; this
    stands in for it so the Python mapping — verbatim money, the shared
    normalization, and the For-Sale fallback — can be pinned without one.
    """

    def __init__(self, payloads: list[dict], for_sale: bool = False):
        self._payloads = list(payloads)
        self._for_sale = for_sale
        self.evaluations = 0
        self.clicked = False

    async def evaluate(self, js: str):
        self.evaluations += 1
        payload = self._payloads[min(len(self._payloads) - 1, self.evaluations - 1)]
        return json.dumps(payload)

    def get_by_role(self, role: str, name=None):
        hit = self._for_sale and not self.clicked
        return _FakeLocator(1 if hit else 0, on_click=lambda: setattr(self, "clicked", True))

    def get_by_text(self, pattern):
        return _FakeLocator(0)

    async def wait_for_timeout(self, ms: int) -> None:
        pass


class TestBrokerMapping:
    """The Python half of cards(): JS output in, Listing fields out."""

    ONE = {
        "title": "Established Neighborhood Cafe & Bakery",
        "location": "San Francisco, CA",
        "asking_price": "$1,258,000 + Inventory",
        "description": "A profitable cafe and bakery.",
        "listing_url": None,
        "url": "https://www.bizbuysell.com/business-opportunity/cafe/41243001/?utm_source=x",
        "listing_id": "41243001",
    }

    @pytest.mark.asyncio
    async def test_money_is_kept_verbatim(self):
        page = _FakePage([{"title": "Krea", "blocked": False, "cards": [self.ONE]}])
        result = await BizBuySellBroker().cards(page)
        assert result.listings[0].asking_price == "$1,258,000 + Inventory"

    @pytest.mark.asyncio
    async def test_fields_map_onto_the_shared_listing_shape(self):
        page = _FakePage([{"title": "Krea", "blocked": False, "cards": [self.ONE]}])
        listing = (await BizBuySellBroker().cards(page)).listings[0]
        assert listing.listing_id == "41243001"
        assert listing.url == "https://www.bizbuysell.com/business-opportunity/cafe/41243001/"
        assert listing.normalized_url == "bizbuysell.com/business-opportunity/cafe/41243001"
        assert listing.title == "Established Neighborhood Cafe & Bakery"
        assert listing.location == "San Francisco, CA"
        assert listing.excerpt == "A profitable cafe and bakery."
        assert listing.source == "bizbuysell_broker"

    @pytest.mark.asyncio
    async def test_broker_tiles_carry_no_profit_figures(self):
        """Documented, not a bug: a broker-profile tile shows only title,
        location, asking price, and description — cash flow, EBITDA, and revenue
        are not on the tile (verified live on real broker profiles), so the
        adapter never invents them. Locks that they stay empty rather than
        picking up some neighbouring number."""
        page = _FakePage([{"title": "Krea", "blocked": False, "cards": [self.ONE]}])
        listing = (await BizBuySellBroker().cards(page)).listings[0]
        assert listing.cashflow == ""
        assert listing.ebitda == ""
        assert listing.revenue == ""

    @pytest.mark.asyncio
    async def test_the_listing_id_is_recovered_from_the_url_when_absent(self):
        card = dict(self.ONE)
        card["listing_id"] = None
        page = _FakePage([{"cards": [card]}])
        assert (await BizBuySellBroker().cards(page)).listings[0].listing_id == "41243001"

    @pytest.mark.asyncio
    async def test_blocked_and_title_pass_through(self):
        page = _FakePage([{"title": "Pardon Our Interruption", "blocked": True, "cards": []}])
        result = await BizBuySellBroker().cards(page)
        assert result.blocked is True
        assert result.title == "Pardon Our Interruption"

    @pytest.mark.asyncio
    async def test_an_empty_page_clicks_for_sale_and_re_reads(self):
        """When the bp_cfspg URL lands off the For-Sale tab, cards() clicks it and
        reads again rather than reporting a listing-less broker."""
        empty = {"title": "Krea", "blocked": False, "cards": []}
        full = {"title": "Krea", "blocked": False, "cards": [self.ONE]}
        page = _FakePage([empty, full], for_sale=True)
        result = await BizBuySellBroker().cards(page)
        assert page.clicked is True
        assert page.evaluations == 2
        assert len(result.listings) == 1

    @pytest.mark.asyncio
    async def test_a_blocked_page_is_not_retried_as_a_missing_tab(self):
        """A block is not an inactive tab — clicking For-Sale on a challenge page
        would be a wasted interaction, and the block must be reported as one."""
        page = _FakePage([{"title": "Access Denied", "blocked": True, "cards": []}], for_sale=True)
        result = await BizBuySellBroker().cards(page)
        assert page.clicked is False
        assert result.blocked is True


class TestSerpJavascriptFallbacks:
    """Field routing in JS_CARDS without depending on an installed browser."""

    def test_compact_card_and_labeled_lines_keep_their_field_boundaries(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("node is required to execute the in-page card extractor")

        cards = [
            {
                "id": "2549590",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "nadcap-and-as9100d-aerospace-cnc-manufacturer/2549590/"
                ),
                "title": "Nadcap & AS9100D Aerospace CNC Manufacturer",
                "locationClass": "$4,800,000",
                "innerText": (
                    "Nadcap & AS9100D Aerospace CNC Manufacturer\n"
                    "$4,800,000 - Orange County, CA\n"
                    "$4,800,000\nOrange County, CA\nZaihua Hu\nCURB"
                ),
            },
            {
                "id": "2484641",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "auto-repair-franchise-6-bays/2484641/"
                ),
                "title": "Auto Repair Franchise",
                "locationClass": "Los Angeles County, CA",
                "asking": "$1,200,000",
                "cash": "Cash Flow: $300,000",
                "innerText": (
                    "Auto Repair Franchise\nLos Angeles County, CA\n$1,200,000\n"
                    "Cash Flow: $300,000\nRevenue: $1,000,000\n"
                    "Presented by Valley Business Brokers"
                ),
            },
            {
                "id": "2549572",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "9-year-excavation-demolition-and-grading-contractor/2549572/"
                ),
                "title": "9 year-Excavation Demolition and Grading Contractor",
                "locationClass": None,
                "innerText": (
                    "9 year-Excavation Demolition and Grading Contractor\n"
                    "Los Angeles, CA\nLos Angeles, CA\n"
                    "Dan McKirdy\nSelect Business Sales"
                ),
            },
            {
                "id": "2537261",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "mission-critical-engineering-and-specialty-construction-company-3-3m/2537261/"
                ),
                "title": "Mission-Critical Engineering & Specialty Construction Company – $3.3M",
                "locationClass": "San Joaquin County, CA",
                "asking": "Not Disclosed",
                "cash": "Cash Flow: $3,342,276",
                "innerText": (
                    "Mission-Critical Engineering & Specialty Construction Company – $3.3M\n"
                    "San Joaquin County, CA\nNot Disclosed\nCash Flow: $3,342,276"
                ),
            },
            {
                "id": "2456287",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "multi-provider-primary-care-and-sleep-medicine-practice-in-southern-ca/2456287/"
                ),
                "title": "Multi-Provider Primary Care & Sleep Medicine Practice in Southern CA",
                "locationClass": "San Bernardino, CA",
                "asking": "Not Disclosed",
                "cash": "EBITDA: $874,000",
                "innerText": (
                    "Multi-Provider Primary Care & Sleep Medicine Practice in Southern CA\n"
                    "San Bernardino, CA\nNot Disclosed\nEBITDA: $874,000"
                ),
            },
            {
                "id": "2281243",
                "url": (
                    "https://www.bizbuysell.com/business-opportunity/"
                    "20m-growing-lab-full-service-lab-with-major-insurances-contracts-and/2281243/"
                ),
                "title": "20M Growing Lab, Full Service Lab with Major Insurance’s Contracts and",
                "locationClass": "California",
                "asking": "$29,000,000",
                "cash": "Cash Flow: $6,000,000",
                "innerText": (
                    "20M Growing Lab, Full Service Lab with Major Insurance’s Contracts and\n"
                    "California\n2\n$29,000,000\nCash Flow: $6,000,000"
                ),
            },
        ]
        harness = r"""
const extractor = JSON.parse(process.argv[1]);
const specs = JSON.parse(process.argv[2]);
const element = (textContent) => ({textContent});
const anchors = specs.map(spec => ({
  href: spec.url,
  id: spec.id,
  innerText: spec.innerText,
  textContent: spec.innerText,
  innerHTML: '',
  getAttribute: name => name === 'aria-label' ? null : null,
  querySelector: sel => {
    if (sel === '.title') return element(spec.title);
    if (sel === '.location') return element(spec.locationClass);
    if (sel.startsWith('p.asking-price')) return spec.asking ? element(spec.asking) : null;
    if (sel === '.cash-flow') return spec.cash ? element(spec.cash) : null;
    return null;
  },
}));
global.window = {__cbsMarkdown: () => ''};
global.document = {
  title: 'Businesses For Sale',
  body: {innerText: ''},
  querySelectorAll: () => anchors,
};
global.location = {href: 'https://www.bizbuysell.com/california-businesses-for-sale/'};
process.stdout.write(eval(extractor));
"""
        completed = subprocess.run(
            [node, "-e", harness, json.dumps(JS_CARDS), json.dumps(cards)],
            check=True,
            capture_output=True,
            text=True,
        )
        by_id = {x["listing_id"]: x for x in json.loads(completed.stdout)["cards"]}

        compact = by_id["2549590"]
        assert compact["asking_price"] == "$4,800,000"
        assert compact["location"] == "Orange County, CA"

        ordinary = by_id["2484641"]
        assert ordinary["revenue"] == "$1,000,000"
        assert ordinary["cashflow"] == "$300,000"

        no_price = by_id["2549572"]
        assert no_price["asking_price"] is None
        assert no_price["location"] == "Los Angeles, CA"

        undisclosed_cash = by_id["2537261"]
        assert undisclosed_cash["asking_price"] == "Not Disclosed"
        assert undisclosed_cash["cashflow"] == "$3,342,276"
        assert undisclosed_cash["ebitda"] is None

        undisclosed_ebitda = by_id["2456287"]
        assert undisclosed_ebitda["asking_price"] == "Not Disclosed"
        assert undisclosed_ebitda["cashflow"] is None
        assert undisclosed_ebitda["ebitda"] == "$874,000"

        state_only = by_id["2281243"]
        assert state_only["location"] == "California"
        assert state_only["asking_price"] == "$29,000,000"


_FIXTURE = Path(__file__).parent / "fixtures" / "broker_profile.html"
_SERP_FIXTURE = Path(__file__).parent / "fixtures" / "serp_results.html"


def _chromium_available() -> bool:
    """Whether a Playwright chromium is installed — asked without launching one."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
        return bool(path) and Path(path).exists()
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="the real JS extractor needs a Playwright chromium"
)


@needs_chromium
class TestBrokerExtraction:
    """JS_BROKER against a saved broker page, in a real browser.

    Like the martian tests, this runs where the answer is real and skips where it
    is not: asserting what the selectors extract from remembered structure is the
    exact mistake — the whole point is that the page's markup is what it is.
    """

    async def _cards(self):
        from playwright.async_api import async_playwright

        html = _FIXTURE.read_text()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await BizBuySellBroker().cards(page)
            finally:
                await browser.close()

    @pytest.mark.asyncio
    async def test_every_listing_card_is_extracted(self):
        result = await self._cards()
        ids = sorted(listing.listing_id for listing in result.listings)
        assert ids == ["41243001", "41243002", "41243003"]

    @pytest.mark.asyncio
    async def test_the_product_backed_card_reads_its_clean_fields(self):
        result = await self._cards()
        cafe = next(x for x in result.listings if x.listing_id == "41243001")
        assert cafe.title == "Established Neighborhood Cafe & Bakery"
        assert cafe.location == "San Francisco, CA"
        # The "Asking Price:" label is stripped; the "+ Inventory" the site wrote
        # is not — that is the verbatim-money contract, on the broker path too.
        assert cafe.asking_price == "$1,258,000 + Inventory"
        assert cafe.url == (
            "https://www.bizbuysell.com/business-opportunity/"
            "established-neighborhood-cafe-bakery/41243001/"
        )
        assert cafe.normalized_url == (
            "bizbuysell.com/business-opportunity/established-neighborhood-cafe-bakery/41243001"
        )
        assert "profitable cafe and bakery" in cafe.excerpt

    @pytest.mark.asyncio
    async def test_not_disclosed_is_kept_as_written(self):
        result = await self._cards()
        auto = next(x for x in result.listings if x.listing_id == "41243002")
        assert auto.asking_price == "Not Disclosed"

    @pytest.mark.asyncio
    async def test_a_card_without_structured_data_falls_back_to_its_tile(self):
        """41243003 has no ld+json Product, so its name/price/description come
        from the tile markup alone."""
        result = await self._cards()
        cleaner = next(x for x in result.listings if x.listing_id == "41243003")
        assert cleaner.title == "Family-Owned Dry Cleaner"
        assert cleaner.location == "Relocatable"
        assert cleaner.asking_price == "$675,000"
        assert "dry cleaning business" in cleaner.excerpt

    @pytest.mark.asyncio
    async def test_a_clean_page_is_not_reported_as_blocked(self):
        result = await self._cards()
        assert result.blocked is False
        assert result.title.startswith("Krea Business")


@needs_chromium
class TestSerpExtraction:
    """JS_CARDS against a saved search-results page, in a real browser.

    The cash-flow/EBITDA routing lives in the in-page JS, so a Python-level
    mapping test cannot see it — the only honest test runs the real extractor
    against the real markup. The fixture's `.cash-flow` elements are copied from
    live California SERP cards (label text and all), so what these assert is what
    the site ships. Every case here FAILS against the pre-fix code, where the
    EBITDA figure was filed as cash flow (one number under two names), and PASSES
    after routing by the element's own label.
    """

    async def _by_id(self):
        from playwright.async_api import async_playwright

        html = _SERP_FIXTURE.read_text()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html)
                result = await BizBuySellSerp().cards(page)
            finally:
                await browser.close()
        return {x.listing_id: x for x in result.listings}

    @pytest.mark.asyncio
    async def test_an_ebitda_card_is_ebitda_with_no_phantom_cash_flow(self):
        """The bug, pinned: an "EBITDA: $X" figure is EBITDA, and cash flow —
        which the card never stated — stays empty."""
        card = (await self._by_id())["2474658"]
        assert card.ebitda == "$664,984"
        assert card.cashflow == ""

    @pytest.mark.asyncio
    async def test_a_cash_flow_card_is_cash_flow_with_no_phantom_ebitda(self):
        card = (await self._by_id())["2531780"]
        assert card.cashflow == "$122,000"
        assert card.ebitda == ""

    @pytest.mark.asyncio
    async def test_an_sde_figure_routes_to_cash_flow(self):
        """SDE is a cash-flow label; it is cash flow, not EBITDA."""
        card = (await self._by_id())["2530911"]
        assert card.cashflow == "$88,000"
        assert card.ebitda == ""

    @pytest.mark.asyncio
    async def test_no_figure_ever_lands_in_two_fields(self):
        """The core invariant across the whole page: the one profit figure a card
        states occupies exactly one of cash flow / EBITDA, never both."""
        for card in (await self._by_id()).values():
            assert not (card.cashflow and card.ebitda), card.listing_id

    @pytest.mark.asyncio
    async def test_revenue_is_absent_on_ordinary_cards(self):
        """SERP cards do not show revenue, so it stays empty — not back-filled
        from the asking price or the profit figure."""
        by_id = await self._by_id()
        assert by_id["2474658"].revenue == ""
        assert by_id["2531780"].revenue == ""
        assert by_id["2530924"].revenue == ""

    @pytest.mark.asyncio
    async def test_the_franchise_revenue_line_still_extracts(self):
        """The rare franchise tile states revenue as a trailing line; the
        existing Revenue: regex still lifts it, alongside a real cash flow and no
        EBITDA."""
        card = (await self._by_id())["2484641"]
        assert card.revenue == "$1,000,000"
        assert card.cashflow == "$300,000"
        assert card.ebitda == ""

    @pytest.mark.asyncio
    async def test_a_labeled_fallback_stops_at_the_end_of_its_rendered_line(self):
        """The broker attribution follows Revenue in the rendered card, but is
        not part of the revenue value."""
        card = (await self._by_id())["2484641"]
        assert card.revenue == "$1,000,000"

    @pytest.mark.asyncio
    async def test_compact_card_recovers_unclassed_price_and_location(self):
        """Some broker-backed cards put the price in `.location` and repeat
        both values as plain lines. Route by value shape instead of class alone.
        """
        card = (await self._by_id())["2549590"]
        assert card.asking_price == "$4,800,000"
        assert card.location == "Orange County, CA"

    @pytest.mark.asyncio
    async def test_a_money_value_is_never_accepted_as_a_location(self):
        card = (await self._by_id())["2549463"]
        assert card.asking_price == "$8,000,000"
        assert card.location == "Orange County, CA"

    @pytest.mark.asyncio
    async def test_compact_card_without_price_still_recovers_location(self):
        card = (await self._by_id())["2549572"]
        assert card.asking_price == ""
        assert card.location == "Los Angeles, CA"

    @pytest.mark.asyncio
    async def test_not_disclosed_price_can_coexist_with_cash_flow(self):
        card = (await self._by_id())["2537261"]
        assert card.asking_price == "Not Disclosed"
        assert card.cashflow == "$3,342,276"
        assert card.ebitda == ""

    @pytest.mark.asyncio
    async def test_not_disclosed_price_can_coexist_with_ebitda(self):
        card = (await self._by_id())["2456287"]
        assert card.asking_price == "Not Disclosed"
        assert card.cashflow == ""
        assert card.ebitda == "$874,000"

    @pytest.mark.asyncio
    async def test_a_state_only_location_is_preserved(self):
        card = (await self._by_id())["2281243"]
        assert card.location == "California"
        assert card.asking_price == "$29,000,000"

    @pytest.mark.asyncio
    async def test_an_asking_only_card_leaves_every_profit_field_empty(self):
        card = (await self._by_id())["2530924"]
        assert card.asking_price == "$195,000"
        assert card.cashflow == ""
        assert card.ebitda == ""
        assert card.revenue == ""

    @pytest.mark.asyncio
    async def test_a_clean_page_is_not_reported_as_blocked(self):
        from playwright.async_api import async_playwright

        html = _SERP_FIXTURE.read_text()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html)
                result = await BizBuySellSerp().cards(page)
            finally:
                await browser.close()
        assert result.blocked is False
        assert len(result.listings) == 11
