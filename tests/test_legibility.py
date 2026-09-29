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

import httpx
import pytest
import respx

from app.models import Listing
from app.services import legibility
from app.services.typesafe import API, Noul, RawAnswer, TypeSafeClient, TypeSafeUnavailable
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
    """Answers every question in one `ask` from a list of probabilities (one per
    question, in order; 0.95 past its end), or raises `error`. `calls` is one
    entry per request."""

    def __init__(self, answers=None, error: Exception | None = None):
        self._answers = list(answers or [])
        self._error = error
        self.calls: list[tuple[dict, dict]] = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self._error is not None:
            raise self._error
        return {name: Noul(probability=self._answers[i] if i < len(self._answers) else 0.95)
                for i, name in enumerate(questions)}


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
    async def test_a_price_with_its_currency_spelled_out_reads_as_an_amount(self):
        """Flippa prints "USD $650,000"; the live gate failed the page on it."""
        prices = ["USD $650,000", "US$1.2M", "USD $2,273,879", "CAD $450,000",
                  "AUD $2,000,000", "NZD $300,000", "EUR 300,000", "GBP £1,250,000",
                  "£1,250,000", "€300,000", "C$900,000"]
        cards = [_card(i, asking_price=p) for i, p in enumerate(prices)]
        verdict = await legibility.check(cards, page=1)
        assert verdict.ok and len(verdict.listings) == len(prices)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("price", [
        "$650,000 USD", "$650,000 usd", "$1.2 Million", "$1.2 million USD", "$850 Thousand",
        "$3.4 Mil", "$1.5MM", "$2M", "$650K", "$1,250,000 (Firm)", "$1,250,000 (Firm) USD",
        "$900,000 (negotiable) + Inventory", "CAD $450,000 CAD", "$1.1 Billion",
    ])
    async def test_common_price_formats_read_as_amounts(self, price):
        """Each of these failed a page before; each is plainly a price."""
        cards = [_card(i, asking_price=price) for i in range(3)]
        verdict = await legibility.check(cards, page=1)
        assert verdict.ok, verdict.reason

    @pytest.mark.parametrize("price", ["$650,000 USD", "$1.2 Million", "$1,250,000 (Firm)"])
    def test_the_store_s_parser_stays_strict(self, price):
        """Reading past a note is for deciding whether a field is a price; the
        number the store keeps is still only an exact amount."""
        from app.stores.money import parse_money

        assert parse_money(price) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("price", ["Call (555) 010-0100", "Listed (2024)", "3 bedrooms (2 baths)",
                                       "Million-dollar views, 4 acres"])
    async def test_a_note_in_brackets_does_not_make_anything_a_price(self, price):
        cards = [_card(i, asking_price=price) for i in range(3)]
        assert not (await legibility.check(cards, page=1)).ok

    @pytest.mark.asyncio
    async def test_a_monthly_figure_is_not_an_asking_price(self):
        cards = [_card(i, asking_price="USD $28,274 p/mo") for i in range(3)]
        verdict = await legibility.check(cards, page=1)
        assert not verdict.ok
        assert "Only 0 of 3 asking prices" in verdict.reason

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
        assert verdict.classifier_mean is None and verdict.classifier_asked == 0
        assert not {"classifier_asked", "classifier_dropped", "classifier_mean"} & set(
            verdict.record(1))

    @pytest.mark.asyncio
    async def test_every_card_goes_in_one_request(self):
        """One state holding every card, one yes/no per card: the classifier
        reads the state once and answers every question in it."""
        cards = [_card(i, excerpt="An established business. " * 40) for i in range(10)]
        fake = FakeClassifier([0.9, 0.8, 0.95])
        verdict = await legibility.check(cards, page=1, classifier=fake)

        assert verdict.ok and verdict.listings == cards
        assert len(fake.calls) == 1, "one request for the whole page"
        assert (verdict.classifier_asked, verdict.classifier_dropped) == (10, 0)
        assert verdict.classifier_mean == pytest.approx((0.9 + 0.8 + 0.95 + 7 * 0.95) / 10)

        state, questions = fake.calls[0]
        names = [f"card_{i}" for i in range(1, 11)]
        assert list(state["cards"]) == names and list(questions) == names
        titles = [card["title"] for card in state["cards"].values()]
        assert titles == [f"Profitable Business {i}" for i in range(10)], "page order"
        for name, question in questions.items():
            assert question["type"] == "noul"
            assert question["instructions"] == legibility.QUESTION.format(card=name)
            assert question["instructions"] == (
                f"{name} in the state is a listing of a business for sale.")
        first = state["cards"]["card_1"]
        assert first["asking_price"] == "$1,250,000"
        assert first["cash_flow"] == "$300,000"
        assert first["location"] == "Sacramento, CA"
        assert first["excerpt"] == cards[0].excerpt[:300].strip(), "forty cards, one request"
        assert "revenue" not in first, "blank fields are left out"

    @pytest.mark.asyncio
    async def test_cards_that_are_not_listings_are_dropped_and_the_page_kept(self):
        """BusinessesForSale's menu links at both ends of its list: two cards to
        leave out, not a reason to throw away the listings between them — when
        the source chose the cards itself (the generic reader)."""
        cards = ([_card(0, title="Sell Your Business"), _card(1, title="Login")]
                 + [_card(i) for i in range(2, 9)] + [_card(9, title="Email Alerts")])
        fake = FakeClassifier([0.04, 0.12] + [0.93] * 7 + [0.31])
        verdict = await legibility.check(cards, page=1, classifier=fake, drop_cards=True)

        assert verdict.ok and verdict.reason == ""
        assert verdict.listings == cards[2:9]
        assert verdict.dropped == 0, "the code checks dropped nothing"
        assert (verdict.classifier_asked, verdict.classifier_dropped) == (10, 3)
        record = verdict.record(1)
        assert record["kept"] == 7
        assert (record["classifier_asked"], record["classifier_low"],
                record["classifier_dropped"]) == (10, 3, 3)
        assert record["classifier_rejected"] == [
            {"title": "Sell Your Business", "p": 0.04}, {"title": "Login", "p": 0.12},
            {"title": "Email Alerts", "p": 0.31}]

    @pytest.mark.asyncio
    async def test_an_adapter_s_cards_are_never_dropped_one_by_one(self):
        """A site adapter (BizBuySell) read these cards with code written for the
        page: the classifier judges the page, and a page that passes keeps every
        card — a misjudged listing is not silently removed."""
        cards = ([_card(0, title="Laundromat — Owner Retiring"), _card(1, title="Coin Op")]
                 + [_card(i) for i in range(2, 9)] + [_card(9, title="Vending Route")])
        fake = FakeClassifier([0.04, 0.12] + [0.93] * 7 + [0.31])
        verdict = await legibility.check(cards, page=1, classifier=fake)

        assert verdict.ok and verdict.reason == ""
        assert verdict.listings == cards, "every card the adapter read is kept"
        record = verdict.record(1)
        assert record["kept"] == 10
        assert (record["classifier_asked"], record["classifier_low"],
                record["classifier_dropped"]) == (10, 3, 0)
        assert [r["title"] for r in record["classifier_rejected"]] == [
            "Laundromat — Owner Retiring", "Coin Op", "Vending Route"], "still on the record"

    @pytest.mark.asyncio
    async def test_an_adapter_s_page_still_fails_when_fewer_than_half_pass(self):
        fake = FakeClassifier([0.1, 0.3, 0.23, 0.9, 0.8, 0.49])
        verdict = await legibility.check([_card(i) for i in range(6)], page=2, classifier=fake)
        assert not verdict.ok and verdict.listings == []
        assert "Only 2 of 6 cards on page 2 read as business listings" in verdict.reason

    @pytest.mark.asyncio
    async def test_fewer_than_half_passing_fails_the_page_in_plain_words(self):
        fake = FakeClassifier([0.1, 0.3, 0.23, 0.9, 0.8, 0.49])
        verdict = await legibility.check([_card(i) for i in range(6)], page=2, classifier=fake)
        assert not verdict.ok
        assert verdict.listings == []
        assert verdict.reason == (
            "Only 2 of 6 cards on page 2 read as business listings (at least half should) "
            "— nothing from this page was kept."
        )
        record = verdict.record(2)
        assert (record["classifier_asked"], record["classifier_dropped"]) == (6, 4)
        assert record["classifier_mean"] == pytest.approx(0.47, abs=0.001)

    @pytest.mark.asyncio
    async def test_exactly_half_passing_is_enough(self):
        fake = FakeClassifier([0.9, 0.1, 0.5, 0.2])
        verdict = await legibility.check([_card(i) for i in range(4)], page=1, classifier=fake,
                                         drop_cards=True)
        assert verdict.ok
        assert [c.title for c in verdict.listings] == ["Profitable Business 0",
                                                       "Profitable Business 2"]

    @pytest.mark.asyncio
    async def test_a_long_page_is_asked_about_its_first_and_last_twenty_cards(self):
        """Forty cards in one request at most; a menu or footer read as cards
        sits at the ends. The cards between are kept on the code checks."""
        cards = [_card(i) for i in range(100)]
        fake = FakeClassifier([0.02] + [0.9] * 38 + [0.03])
        verdict = await legibility.check(cards, page=1, classifier=fake, drop_cards=True)

        state, questions = fake.calls[0]
        assert len(fake.calls) == 1 and len(questions) == legibility.MAX_CARDS == 40
        assert [c["title"] for c in state["cards"].values()] == [
            f"Profitable Business {i}" for i in (*range(20), *range(80, 100))]
        assert verdict.ok and verdict.listings == cards[1:99]
        assert (verdict.classifier_asked, verdict.classifier_dropped) == (40, 2)

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
    async def test_an_answer_of_the_wrong_kind_is_skipped_like_an_outage(self):
        class Odd:
            async def ask(self, state, questions):
                return {name: RawAnswer(type="score") for name in questions}

        verdict = await legibility.check([_card(1), _card(2)], page=1, classifier=Odd())
        assert verdict.ok and len(verdict.listings) == 2
        assert "card_1" in verdict.classifier_error

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


class TestOneRequestOnTheWire:
    """The real client against a faked endpoint: the whole page is one POST."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_page_is_a_single_request_with_a_question_per_card(self):
        names = [f"card_{i}" for i in range(1, 8)]
        route = respx.post(API).mock(return_value=httpx.Response(200, json={
            "model": "typesafe/jev-test",
            "answers": {name: {"type": "noul", "noul": 0.1 if name == "card_7" else 0.9}
                        for name in names},
        }))
        client = TypeSafeClient(lambda: "sk-or-test", lambda: "jev-latest")
        cards = [_card(i) for i in range(7)]
        verdict = await legibility.check(cards, page=1, classifier=client, drop_cards=True)

        assert verdict.ok and verdict.listings == cards[:6]
        assert verdict.classifier_dropped == 1
        assert route.call_count == 1
        body = json.loads(route.calls.last.request.content)
        assert sorted(body["state"]["cards"]) == names
        assert sorted(body["questions"]) == names
        assert all(q["type"] == "noul" for q in body["questions"].values())


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
