"""The legibility check: a page of cards that don't read as listings is refused.

What is pinned here is the judgement itself, with the classifier faked: which
cards are dropped, which pages fail and in what words, when the classifier is
asked and how many times, and that a classifier outage never fails a page the
code checks have passed. The sweep's use of the verdict (evidence directory,
no retry) is pinned in test_scrape.py.

The last class is the regression guard that matters most: BizBuySell pages —
the only source today — must pass, with and without a classifier.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.models import Listing
from app.services import legibility
from app.services.typesafe import TypeSafeUnavailable
from app.sources.bizbuysell import JS_BROKER, JS_CARDS, BizBuySellBroker, BizBuySellSerp


def _card(i: int, **fields) -> Listing:
    base = {
        "url": f"https://example.com/listing/{i}",
        "title": f"Profitable Business {i}",
        "location": "Sacramento, CA",
        "asking_price": "$1,250,000",
        "cashflow": "$300,000",
        "excerpt": f"An established business, number {i}, with loyal customers.",
        "source": "fake",
    }
    base.update(fields)
    return Listing(**base)


class FakeClassifier:
    """Answers `noul` from a list (one per call, in order) or raises `error`."""

    def __init__(self, answers=None, error: Exception | None = None):
        self._answers = list(answers or [])
        self._error = error
        self.calls: list[tuple[dict, str]] = []

    async def noul(self, state, instructions):
        self.calls.append((state, instructions))
        if self._error is not None:
            raise self._error
        return self._answers[len(self.calls) - 1] if self._answers else 0.95


class TestCodeChecks:
    @pytest.mark.asyncio
    async def test_cards_without_a_title_or_a_link_are_dropped(self):
        cards = [_card(i) for i in range(8)] + [_card(8, title=""), _card(9, url="")]
        verdict = await legibility.check(cards, page=1)
        assert verdict.ok
        assert verdict.dropped == 2
        assert [c.url for c in verdict.listings] == [c.url for c in cards[:8]]

    @pytest.mark.asyncio
    async def test_the_page_fails_when_under_70_percent_have_both(self):
        cards = [_card(i) for i in range(6)] + [_card(i, title="  ") for i in range(6, 10)]
        verdict = await legibility.check(cards, page=2)
        assert not verdict.ok
        assert verdict.listings == [], "nothing from a failed page is kept"
        assert "6 of 10" in verdict.reason
        assert "page 2" in verdict.reason
        assert "nothing from this page was kept" in verdict.reason

    @pytest.mark.asyncio
    async def test_exactly_70_percent_is_enough(self):
        cards = [_card(i) for i in range(7)] + [_card(i, title="") for i in range(7, 10)]
        verdict = await legibility.check(cards, page=1)
        assert verdict.ok and len(verdict.listings) == 7

    @pytest.mark.asyncio
    async def test_asking_prices_that_mostly_do_not_read_as_amounts_fail_the_page(self):
        cards = [
            _card(1, asking_price="Call 555-0100"),
            _card(2, asking_price="Listed 2024"),
            _card(3, asking_price="3 bedrooms"),
            _card(4, asking_price="$450,000"),
        ]
        verdict = await legibility.check(cards, page=3)
        assert not verdict.ok
        assert "1 of 4 asking prices" in verdict.reason
        assert "'Call 555-0100'" in verdict.reason, "quote an example so it can be checked"
        assert "page 3" in verdict.reason

    @pytest.mark.asyncio
    async def test_half_the_prices_reading_as_amounts_is_enough(self):
        cards = [
            _card(1, asking_price="$450,000"),
            _card(2, asking_price="1.2M"),
            _card(3, asking_price="Call 555-0100"),
            _card(4, asking_price="Listed 2024"),
        ]
        assert (await legibility.check(cards, page=1)).ok

    @pytest.mark.asyncio
    async def test_prices_without_a_digit_are_not_judged(self):
        """"Not Disclosed" is an honest answer, not a sign the field is wrong."""
        cards = [_card(i, asking_price="Not Disclosed") for i in range(5)] + [
            _card(9, asking_price="Call 555-0100"),
            _card(10, asking_price="$99,000"),
        ]
        assert (await legibility.check(cards, page=1)).ok

    @pytest.mark.asyncio
    async def test_a_qualified_price_still_reads_as_an_amount(self):
        """money.py refuses "$81,000 + Inventory" as a number to store; it is
        still plainly a price, and a one-listing broker page quoting it is fine."""
        verdict = await legibility.check(
            [_card(1, asking_price="$81,000 + Inventory")], page=1)
        assert verdict.ok

    @pytest.mark.asyncio
    async def test_ranges_in_revenue_or_cash_flow_never_fail_a_page(self):
        cards = [
            _card(i, revenue="$250K - $500K", cashflow="$100K-$250K", ebitda="1-2M")
            for i in range(5)
        ]
        assert (await legibility.check(cards, page=1)).ok


class TestClassifier:
    @pytest.mark.asyncio
    async def test_without_a_classifier_nothing_is_asked(self):
        verdict = await legibility.check([_card(i) for i in range(5)], page=1)
        assert verdict.ok
        assert verdict.classifier_mean is None
        assert "classifier_mean" not in verdict.record(1)

    @pytest.mark.asyncio
    async def test_three_cards_spread_across_the_page_are_asked_about(self):
        cards = [_card(i) for i in range(10)]
        fake = FakeClassifier([0.9, 0.8, 0.95])
        verdict = await legibility.check(cards, page=1, classifier=fake)

        assert verdict.ok
        assert verdict.classifier_samples == 3
        assert verdict.classifier_mean == pytest.approx((0.9 + 0.8 + 0.95) / 3)
        titles = [state["title"] for state, _ in fake.calls]
        assert titles == ["Profitable Business 0", "Profitable Business 4",
                          "Profitable Business 9"], "first, middle and last"
        state, question = fake.calls[0]
        assert question == legibility.QUESTION
        assert "business for sale" in question
        assert state["asking_price"] == "$1,250,000"
        assert state["cash_flow"] == "$300,000"
        assert state["location"] == "Sacramento, CA"
        assert "loyal customers" in state["excerpt"]
        assert "revenue" not in state, "blank fields are left out"

    @pytest.mark.asyncio
    async def test_a_short_page_is_asked_about_every_card(self):
        fake = FakeClassifier()
        await legibility.check([_card(1), _card(2)], page=1, classifier=fake)
        assert len(fake.calls) == 2

    @pytest.mark.asyncio
    async def test_a_low_mean_fails_the_page_in_plain_words(self):
        fake = FakeClassifier([0.1, 0.3, 0.23])
        verdict = await legibility.check([_card(i) for i in range(6)], page=2, classifier=fake)
        assert not verdict.ok
        assert verdict.listings == []
        assert verdict.reason == (
            "The cards on page 2 don't read as business listings (mean 0.21 on 3 samples) "
            "— nothing from this page was kept."
        )
        assert verdict.record(2)["classifier_mean"] == pytest.approx(0.21, abs=0.001)

    @pytest.mark.asyncio
    async def test_a_classifier_error_skips_the_classifier_and_keeps_the_page(self, caplog):
        """Best-effort: a BizBuySell page must not fail because Jev is down."""
        fake = FakeClassifier(error=TypeSafeUnavailable("The classifier did not answer."))
        with caplog.at_level(logging.WARNING, logger="cloakbiz.legibility"):
            verdict = await legibility.check([_card(i) for i in range(4)], page=1,
                                             classifier=fake)
        assert verdict.ok
        assert len(verdict.listings) == 4
        assert verdict.classifier_mean is None
        assert "did not answer" in verdict.classifier_error
        assert verdict.record(1)["classifier_error"] == verdict.classifier_error
        assert "classifier skipped" in caplog.text

    @pytest.mark.asyncio
    async def test_a_bug_is_not_mistaken_for_an_outage(self):
        with pytest.raises(KeyError):
            await legibility.check([_card(1)], page=1,
                                   classifier=FakeClassifier(error=KeyError("oops")))

    @pytest.mark.asyncio
    async def test_a_page_that_failed_the_code_checks_costs_no_classifier_calls(self):
        fake = FakeClassifier()
        verdict = await legibility.check([_card(i, title="") for i in range(5)], page=1,
                                         classifier=fake)
        assert not verdict.ok
        assert fake.calls == []


class TestZeroCards:
    @pytest.mark.asyncio
    async def test_an_empty_page_is_not_judged(self):
        fake = FakeClassifier()
        verdict = await legibility.check([], page=4, classifier=fake)
        assert verdict.ok and verdict.listings == [] and verdict.reason == ""
        assert fake.calls == []


# ── BizBuySell pages pass ────────────────────────────────────────────────────


class _CannedPage:
    """Hands back canned extractor output for the adapter's own JS, and nothing
    for anything else (the markdown libraries' injection)."""

    def __init__(self, js: str, payload: dict):
        self._js = js
        self._payload = payload

    async def evaluate(self, js: str):
        return json.dumps(self._payload) if js == self._js else None


# The SERP fixture's cards (tests/fixtures/serp_results.html), as JS_CARDS
# returns them: every shape of money the page ships, including undisclosed and
# missing prices.
_SERP_CARDS = [
    {"listing_id": lid, "url": f"https://www.bizbuysell.com/business-opportunity/x/{lid}/",
     "title": title, "location": loc, "asking_price": asking, "cashflow": cash,
     "ebitda": ebitda, "revenue": revenue, "excerpt": f"{title}. {loc}."}
    for lid, title, loc, asking, cash, ebitda, revenue in [
        ("2474658", "Rockin' Jump / Sky Zone Family Trampoline Park", "Contra Costa County, CA",
         "$2,000,000", None, "$664,984", None),
        ("2531780", "Pizza To Go — Owner Finance, Low Rent", "Sacramento, CA",
         "$279,000", "$122,000", None, None),
        ("2484641", "Auto Repair Franchise — 6 Bays", "Los Angeles County, CA",
         "$1,200,000", "$300,000", None, "$1,000,000"),
        ("2549572", "9 year-Excavation Demolition and Grading Contractor", "Los Angeles, CA",
         None, None, None, None),
        ("2537261", "Mission-Critical Engineering & Specialty Construction Company – $3.3M",
         "San Joaquin County, CA", "Not Disclosed", "$3,342,276", None, None),
        ("2281243", "20M Growing Lab, Full Service Lab", "California",
         "$29,000,000", "$6,000,000", None, None),
    ]
]


class TestBizBuySellPagesPass:
    @pytest.mark.asyncio
    async def test_a_serp_page_passes(self):
        page = _CannedPage(JS_CARDS, {"title": "Businesses For Sale", "blocked": False,
                                      "cards": _SERP_CARDS})
        result = await BizBuySellSerp().cards(page)
        assert len(result.listings) == 6
        for classifier in (None, FakeClassifier([0.97, 0.93, 0.99])):
            verdict = await legibility.check(result.listings, page=1, classifier=classifier)
            assert verdict.ok, verdict.reason
            assert verdict.listings == result.listings and verdict.dropped == 0

    @pytest.mark.asyncio
    async def test_a_broker_profile_passes(self):
        cards = [
            {"listing_id": "41243001", "title": "Established Neighborhood Cafe & Bakery",
             "location": "San Francisco, CA", "asking_price": "$1,258,000 + Inventory",
             "description": "A profitable cafe and bakery.",
             "url": "https://www.bizbuysell.com/business-opportunity/cafe/41243001/"},
            {"listing_id": "41243003", "title": "Family-Owned Dry Cleaner",
             "location": "Relocatable", "asking_price": "$675,000 + Inventory",
             "description": "A dry cleaning business.",
             "url": "https://www.bizbuysell.com/business-opportunity/cleaner/41243003/"},
        ]
        page = _CannedPage(JS_BROKER, {"title": "Krea Business", "blocked": False,
                                       "cards": cards})
        result = await BizBuySellBroker().cards(page)
        assert len(result.listings) == 2
        verdict = await legibility.check(result.listings, page=1,
                                         classifier=FakeClassifier([0.9, 0.9]))
        assert verdict.ok, verdict.reason


def _chromium_available() -> bool:
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

_FIXTURES = Path(__file__).parent / "fixtures"


@needs_chromium
class TestBizBuySellFixturesPass:
    """The saved pages through the real extractors, then the check."""

    async def _cards(self, fixture: str, source):
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content((_FIXTURES / fixture).read_text())
                return (await source.cards(page)).listings
            finally:
                await browser.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fixture,source", [
        ("serp_results.html", BizBuySellSerp()),
        ("broker_profile.html", BizBuySellBroker()),
    ])
    async def test_the_saved_page_passes(self, fixture, source):
        listings = await self._cards(fixture, source)
        assert listings
        verdict = await legibility.check(listings, page=1, classifier=FakeClassifier())
        assert verdict.ok, verdict.reason
        assert verdict.dropped == 0
        assert len(verdict.listings) == len(listings)
