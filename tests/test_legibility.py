"""The listing checks: the code checks on a page, and one request per listing.

What is pinned here is the judgement itself, with the classifier faked: which
cards are dropped, which pages fail and in what words, which cards are asked
about (only new ones, or stored ones with a blank Bot Triage) and in what one
request, how many requests are in flight at once, and that a classifier outage
never fails a page. The sweep's use of it (evidence directory, no retry,
triage reading the card decision) is pinned in test_scrape.py.

The last classes are the regression guard that matters most: BizBuySell pages
must pass, with and without a classifier, and never lose a card to it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.models import Listing
from app.services import legibility
from app.services.legibility import ELIGIBLE_QUESTION, ListingCheck
from app.services.triage import CRITERIA, LEAD_IN, STAGE_CARD, Triager, card_state
from app.services.typesafe import (
    API,
    TYPESAFE_PARALLEL,
    Choice,
    Noul,
    RawAnswer,
    TypeSafeAuthError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafeUnavailable,
)
from app.sources.bizbuysell import JS_BROKER, JS_CARDS, BizBuySellBroker, BizBuySellSerp
from app.stores.base import DedupeIndex


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
    """Answers each listing's one request by its title: P(eligible) from
    `eligible` (0.95 otherwise) and, when triage is asked, P(review) from
    `review` (0.9 otherwise). `errors` maps a title to the exception its request
    raises. Every request yields to the loop (as a real one does) and is
    recorded; `peak` is the most that were ever in flight at once."""

    def __init__(self, eligible=None, *, review=None, errors=None):
        self.eligible = dict(eligible or {})
        self.review = dict(review or {})
        self.errors = dict(errors or {})
        self.calls: list[tuple[dict, dict]] = []
        self.in_flight = self.peak = 0

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.001)
            title = state.get("title", "")
            if title in self.errors:
                raise self.errors[title]
            out = {"eligible": Noul(self.eligible.get(title, 0.95), "typesafe/jev-test")}
            if "triage" in questions:
                p = self.review.get(title, 0.9)
                out["triage"] = Choice("REVIEW" if p > 0.5 else "REJECT",
                                       {"REVIEW": p, "REJECT": round(1 - p, 6)},
                                       max(p, 1 - p), "typesafe/jev-test")
            return out
        finally:
            self.in_flight -= 1

    def titles(self) -> list[str]:
        return [state["title"] for state, _ in self.calls]


class TestCodeChecks:
    def test_cards_without_a_title_or_a_link_are_dropped(self):
        cards = [_card(i) for i in range(8)] + [_card(8, title=""), _card(9, url="")]
        verdict = legibility.check(cards, page=1)
        assert verdict.ok
        assert verdict.dropped == 2
        assert [c.url for c in verdict.listings] == [c.url for c in cards[:8]]

    def test_the_page_fails_when_under_70_percent_have_both(self):
        cards = [_card(i) for i in range(6)] + [_card(i, title="  ") for i in range(6, 10)]
        verdict = legibility.check(cards, page=2)
        assert not verdict.ok
        assert verdict.listings == [], "nothing from a failed page is kept"
        assert "6 of 10" in verdict.reason
        assert "page 2" in verdict.reason
        assert "nothing from this page was kept" in verdict.reason

    def test_exactly_70_percent_is_enough(self):
        cards = [_card(i) for i in range(7)] + [_card(i, title="") for i in range(7, 10)]
        verdict = legibility.check(cards, page=1)
        assert verdict.ok and len(verdict.listings) == 7

    def test_asking_prices_that_mostly_do_not_read_as_amounts_fail_the_page(self):
        cards = [
            _card(1, asking_price="Call 555-0100"),
            _card(2, asking_price="Listed 2024"),
            _card(3, asking_price="3 bedrooms"),
            _card(4, asking_price="$450,000"),
        ]
        verdict = legibility.check(cards, page=3)
        assert not verdict.ok
        assert "1 of 4 asking prices" in verdict.reason
        assert "'Call 555-0100'" in verdict.reason, "quote an example so it can be checked"
        assert "page 3" in verdict.reason

    def test_half_the_prices_reading_as_amounts_is_enough(self):
        cards = [
            _card(1, asking_price="$450,000"),
            _card(2, asking_price="1.2M"),
            _card(3, asking_price="Call 555-0100"),
            _card(4, asking_price="Listed 2024"),
        ]
        assert legibility.check(cards, page=1).ok

    def test_prices_without_a_digit_are_not_judged(self):
        """"Not Disclosed" is an honest answer, not a sign the field is wrong."""
        cards = [_card(i, asking_price="Not Disclosed") for i in range(5)] + [
            _card(9, asking_price="Call 555-0100"),
            _card(10, asking_price="$99,000"),
        ]
        assert legibility.check(cards, page=1).ok

    def test_a_qualified_price_still_reads_as_an_amount(self):
        """money.py refuses "$81,000 + Inventory" as a number to store; it is
        still plainly a price, and a one-listing broker page quoting it is fine."""
        verdict = legibility.check([_card(1, asking_price="$81,000 + Inventory")], page=1)
        assert verdict.ok

    def test_a_price_with_its_currency_spelled_out_reads_as_an_amount(self):
        """Flippa prints "USD $650,000"; the live gate failed the page on it."""
        prices = ["USD $650,000", "US$1.2M", "USD $2,273,879", "CAD $450,000",
                  "AUD $2,000,000", "NZD $300,000", "EUR 300,000", "GBP £1,250,000",
                  "£1,250,000", "€300,000", "C$900,000"]
        cards = [_card(i, asking_price=p) for i, p in enumerate(prices)]
        verdict = legibility.check(cards, page=1)
        assert verdict.ok and len(verdict.listings) == len(prices)

    @pytest.mark.parametrize("price", [
        "$650,000 USD", "$650,000 usd", "$1.2 Million", "$1.2 million USD", "$850 Thousand",
        "$3.4 Mil", "$1.5MM", "$2M", "$650K", "$1,250,000 (Firm)", "$1,250,000 (Firm) USD",
        "$900,000 (negotiable) + Inventory", "CAD $450,000 CAD", "$1.1 Billion",
    ])
    def test_common_price_formats_read_as_amounts(self, price):
        """Each of these failed a page before; each is plainly a price."""
        cards = [_card(i, asking_price=price) for i in range(3)]
        verdict = legibility.check(cards, page=1)
        assert verdict.ok, verdict.reason

    @pytest.mark.parametrize("price", ["$650,000 USD", "$1.2 Million", "$1,250,000 (Firm)"])
    def test_the_store_s_parser_stays_strict(self, price):
        """Reading past a note is for deciding whether a field is a price; the
        number the store keeps is still only an exact amount."""
        from app.stores.money import parse_money

        assert parse_money(price) is None

    @pytest.mark.parametrize("price", ["Call (555) 010-0100", "Listed (2024)", "3 bedrooms (2 baths)",
                                       "Million-dollar views, 4 acres"])
    def test_a_note_in_brackets_does_not_make_anything_a_price(self, price):
        cards = [_card(i, asking_price=price) for i in range(3)]
        assert not legibility.check(cards, page=1).ok

    def test_a_monthly_figure_is_not_an_asking_price(self):
        cards = [_card(i, asking_price="USD $28,274 p/mo") for i in range(3)]
        verdict = legibility.check(cards, page=1)
        assert not verdict.ok
        assert "Only 0 of 3 asking prices" in verdict.reason

    def test_ranges_in_revenue_or_cash_flow_never_fail_a_page(self):
        cards = [
            _card(i, revenue="$250K - $500K", cashflow="$100K-$250K", ebitda="1-2M")
            for i in range(5)
        ]
        assert legibility.check(cards, page=1).ok


class TestTheRequest:
    """ONE request per listing element, carrying every question about it."""

    @pytest.mark.asyncio
    async def test_the_state_is_the_card_and_the_questions_ride_together(self):
        listing = _card(1)
        fake = FakeClassifier()
        answer = await legibility.ask(fake, listing, Triager(fake, "Reject restaurants."))
        [(state, questions)] = fake.calls
        assert state == card_state(listing)
        assert state["price_to_earnings_multiple"] == "4.17x", "computed: it cannot divide"
        assert questions == {
            "eligible": {"type": "noul", "instructions": ELIGIBLE_QUESTION},
            "triage": {"type": "choice", "instructions": LEAD_IN + "Reject restaurants.",
                       "criteria": CRITERIA},
        }
        assert answer.eligible == 0.95 and answer.error == ""
        assert answer.triage.decision == "REVIEW" and answer.triage.stage == STAGE_CARD

    @pytest.mark.asyncio
    async def test_without_triage_it_asks_eligible_alone(self):
        fake = FakeClassifier()
        answer = await legibility.ask(fake, _card(1))
        [(_, questions)] = fake.calls
        assert list(questions) == ["eligible"] and answer.triage is None

    def test_the_eligibility_question_is_the_one_validated_live(self):
        assert ELIGIBLE_QUESTION == (
            "The state is one listing of a business that is currently for sale — not a sold, "
            "pending or under-contract listing, and not a menu link, an advertisement or some "
            "other page element.")

    @pytest.mark.asyncio
    async def test_an_answer_of_the_wrong_kind_is_a_classifier_error(self):
        class Odd:
            async def ask(self, state, questions):
                return {name: RawAnswer(type="score") for name in questions}

        with pytest.raises(TypeSafeError):
            await legibility.ask(Odd(), _card(1))


def _page(n: int, *titles: str) -> list[Listing]:
    return [_card(n * 100 + i, title=t) for i, t in enumerate(titles)]


def _index(*listings: Listing, decisions: dict[int, str] | None = None) -> DedupeIndex:
    """The store holding `listings`; `decisions` maps a position to its Bot Triage."""
    index = DedupeIndex(normalized_urls={l.url for l in listings})
    for i, decision in (decisions or {}).items():
        index.decisions_by_url[listings[i].url] = decision
    return index


def _stored(*listings: Listing) -> list[Listing]:
    """The same listings as the store keys them (the fixtures key by URL)."""
    return [l.model_copy(update={"normalized_url": l.url}) for l in listings]


class TestWhichListingsAreAsked:
    @pytest.mark.asyncio
    async def test_sync_false_asks_about_every_card_eligible_only(self):
        fake = FakeClassifier()
        cards = _page(1, "A", "B", "C")
        result = await ListingCheck(fake).page(cards, page=1, drop=True)
        assert fake.titles() == ["A", "B", "C"]
        assert all(list(q) == ["eligible"] for _, q in fake.calls)
        assert result.ok and result.listings == cards
        assert result.record == {"asked": 3, "answered": 3, "known": 0, "eligible": 3,
                                 "not_eligible": 0, "dropped": 0}

    @pytest.mark.asyncio
    async def test_only_new_cards_are_asked_and_known_ones_cost_nothing(self):
        cards = _stored(*_page(1, "Known", "New One", "Also Known"))
        fake = FakeClassifier()
        check = ListingCheck(fake, known=_index(cards[0], cards[2]))
        result = await check.page(cards, page=1, drop=True)
        assert fake.titles() == ["New One"]
        assert result.listings == cards and result.record["known"] == 2

    @pytest.mark.asyncio
    async def test_when_triaging_a_blank_stored_row_is_asked_too(self):
        """The backlog: stored, but its Bot Triage is blank. A row holding a
        decision — anyone's — costs nothing."""
        cards = _stored(*_page(1, "Blank Row", "Decided Row", "New Row", "Unread Row"))
        known = _index(*cards[:2], cards[3], decisions={0: "", 1: "REJECT"})
        fake = FakeClassifier()
        check = ListingCheck(fake, triager=Triager(fake, "Reject restaurants."), known=known)
        await check.page(cards, page=1, drop=True)
        assert fake.titles() == ["Blank Row", "New Row"], "a decision not read is not blank"
        assert all(set(q) == {"eligible", "triage"} for _, q in fake.calls)
        assert check.answer(cards[0]).triage.decision == "REVIEW"
        assert check.answer(cards[1]) is None

    @pytest.mark.asyncio
    async def test_without_triage_a_blank_stored_row_costs_nothing_either(self):
        cards = _stored(*_page(1, "Blank Row", "New Row"))
        fake = FakeClassifier()
        await ListingCheck(fake, known=_index(cards[0], decisions={0: ""})).page(
            cards, page=1, drop=True)
        assert fake.titles() == ["New Row"]

    @pytest.mark.asyncio
    async def test_a_card_is_asked_once_per_sweep(self):
        """Seen on two pages (or under two URLs, or on a retried attempt)."""
        fake = FakeClassifier()
        check = ListingCheck(fake)
        first = _page(1, "A", "B")
        await check.page(first, page=1, drop=True)
        await check.page([first[1], *_page(2, "C")], page=2, drop=True)
        assert fake.titles() == ["A", "B", "C"]


class TestParallel:
    @pytest.mark.asyncio
    async def test_five_requests_at_a_time(self):
        assert TYPESAFE_PARALLEL == 5
        fake = FakeClassifier()
        await ListingCheck(fake).page(_page(1, *(f"Business {i}" for i in range(12))),
                                      page=1, drop=True)
        assert len(fake.calls) == 12 and fake.peak == TYPESAFE_PARALLEL


class TestWhatTheAnswersDo:
    @pytest.mark.asyncio
    async def test_the_generic_reader_s_cards_not_for_sale_are_dropped(self):
        """Synergy's "– Sold" tiles and BusinessesForSale's menu links: cards to
        leave out, not a reason to throw away the listings between them."""
        cards = _page(1, "Sell Your Business", "Bakery", "Dry Cleaner – Sold", "HVAC", "Deli")
        fake = FakeClassifier({"Sell Your Business": 0.31, "Dry Cleaner – Sold": 0.04})
        result = await ListingCheck(fake).page(cards, page=1, drop=True)
        assert result.ok and [c.title for c in result.listings] == ["Bakery", "HVAC", "Deli"]
        assert result.record["dropped"] == 2 and result.record["not_eligible"] == 2
        assert result.record["not_eligible_listings"] == [
            {"title": "Sell Your Business", "p": 0.31}, {"title": "Dry Cleaner – Sold", "p": 0.04}]

    @pytest.mark.asyncio
    async def test_an_adapter_s_cards_are_never_dropped_one_by_one(self):
        """BizBuySell's cards are read by code written for the page: the answers
        judge the page, and a page that passes keeps every card."""
        cards = _page(1, "Laundromat — Owner Retiring", "Coin Op", "HVAC", "Deli", "Dental Lab")
        fake = FakeClassifier({"Laundromat — Owner Retiring": 0.04, "Coin Op": 0.12})
        result = await ListingCheck(fake).page(cards, page=1, drop=False)
        assert result.ok and result.listings == cards, "every card the adapter read is kept"
        assert result.record["not_eligible"] == 2 and result.record["dropped"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drop", [True, False])
    async def test_a_page_where_fewer_than_half_are_for_sale_fails(self, drop):
        cards = _page(2, "Login", "Sell", "FAQ", "Bakery")
        fake = FakeClassifier({"Login": 0.1, "Sell": 0.2, "FAQ": 0.3})
        result = await ListingCheck(fake).page(cards, page=2, drop=drop)
        assert not result.ok and result.listings == []
        assert result.reason == (
            "Only 1 of 4 cards on page 2 read as business listings currently for sale (at "
            "least half should) — nothing from this page was kept.")

    @pytest.mark.asyncio
    async def test_exactly_half_is_enough(self):
        fake = FakeClassifier({"Login": 0.1, "Sell": 0.2})
        result = await ListingCheck(fake).page(_page(1, "Login", "Sell", "A", "B"),
                                               page=1, drop=True)
        assert result.ok and [c.title for c in result.listings] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_a_stored_card_counts_as_passing(self):
        """One new sold listing among nineteen known ones is not a page read wrong."""
        cards = _stored(*_page(1, *(f"Known {i}" for i in range(19)), "Bakery – Sold"))
        fake = FakeClassifier({"Bakery – Sold": 0.03})
        result = await ListingCheck(fake, known=_index(*cards[:19])).page(cards, page=1, drop=True)
        assert result.ok and len(result.listings) == 19 and fake.titles() == ["Bakery – Sold"]

    @pytest.mark.asyncio
    async def test_a_page_of_stored_cards_asks_nothing_and_passes(self):
        cards = _stored(*_page(1, "A", "B", "C"))
        fake = FakeClassifier()
        result = await ListingCheck(fake, known=_index(*cards)).page(cards, page=1, drop=True)
        assert result.ok and result.listings == cards and fake.calls == []

    @pytest.mark.asyncio
    async def test_too_few_cards_never_fail_a_page(self):
        """One sold listing left on a broker's profile is not a page read wrong."""
        assert legibility.MIN_JUDGED == 3
        fake = FakeClassifier({"Under Contract Deli": 0.05, "Sold Cafe": 0.02})
        adapter = await ListingCheck(fake).page(_page(1, "Under Contract Deli"), page=1, drop=False)
        assert adapter.ok and len(adapter.listings) == 1
        generic = await ListingCheck(fake).page(_page(1, "Under Contract Deli", "Sold Cafe"),
                                                page=3, drop=True)
        assert generic.ok and generic.listings == [], "still dropped, one by one"


class TestWhenTheClassifierFails:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [TypeSafeUnavailable("The classifier did not answer."),
                                       TypeSafeAuthError("OpenRouter rejected the key.")])
    async def test_an_outage_stops_the_requests_and_keeps_the_page(self, error):
        titles = ["A", "B"] + [f"Row {i}" for i in range(10)]
        fake = FakeClassifier(errors={"B": error})
        check = ListingCheck(fake)
        result = await check.page(_page(1, *titles), page=1, drop=True)
        assert result.ok and len(result.listings) == len(titles), "nothing unanswered is dropped"
        assert fake.titles() == titles[:TYPESAFE_PARALLEL], "no request after the outage"
        assert check.stopped == str(error)
        assert result.record["stopped"] == str(error)
        assert result.record["unanswered"] == len(titles) - TYPESAFE_PARALLEL + 1
        assert await check.page(_page(2, "Later"), page=2, drop=True) and len(fake.calls) == 5

    @pytest.mark.asyncio
    async def test_a_request_refused_for_one_card_is_that_card_s(self):
        fake = FakeClassifier({"Login": 0.1}, errors={"Bad": TypeSafeError("HTTP 400")})
        check = ListingCheck(fake)
        result = await check.page(_page(1, "Bad", "Login", "A", "B"), page=1, drop=True)
        assert result.ok and [c.title for c in result.listings] == ["Bad", "A", "B"]
        assert result.record["errors"] == 1 and check.stopped is None
        assert check.answer(result.listings[0]).error == "HTTP 400"

    @pytest.mark.asyncio
    async def test_a_bug_is_not_mistaken_for_an_outage(self):
        with pytest.raises(KeyError):
            await ListingCheck(FakeClassifier(errors={"A": KeyError("oops")})).page(
                _page(1, "A"), page=1, drop=True)


class TestOneRequestOnTheWire:
    """The real client against a faked endpoint: one POST per listing."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_each_listing_is_one_request_five_at_a_time(self):
        in_flight = peak = 0

        async def answer(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            body = json.loads(request.content)
            p = 0.1 if body["state"]["title"] == "Profitable Business 6" else 0.9
            return httpx.Response(200, json={
                "model": "typesafe/jev-test",
                "answers": {"eligible": {"type": "noul", "noul": p},
                            "triage": {"type": "choice", "choice": "REVIEW",
                                       "probabilities": {"REVIEW": 0.8, "REJECT": 0.2},
                                       "confidence": 0.8}}})

        route = respx.post(API).mock(side_effect=answer)
        client = TypeSafeClient(lambda: "sk-or-test", lambda: "jev-latest")
        cards = [_card(i) for i in range(12)]
        check = ListingCheck(client, triager=Triager(client, "Reject restaurants."))
        result = await check.page(cards, page=1, drop=True)

        assert route.call_count == 12 and peak == TYPESAFE_PARALLEL
        assert result.ok and len(result.listings) == 11
        body = json.loads(route.calls[0].request.content)
        assert body["state"] == card_state(cards[0])
        assert sorted(body["questions"]) == ["eligible", "triage"]
        assert check.answer(cards[0]).triage.p_review == pytest.approx(0.8)


class TestZeroCards:
    def test_an_empty_page_is_not_judged(self):
        verdict = legibility.check([], page=4)
        assert verdict.ok and verdict.listings == [] and verdict.reason == ""

    @pytest.mark.asyncio
    async def test_an_empty_page_asks_nothing(self):
        fake = FakeClassifier()
        result = await ListingCheck(fake).page([], page=4, drop=True)
        assert result.ok and fake.calls == []


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
        verdict = legibility.check(result.listings, page=1)
        assert verdict.ok, verdict.reason
        assert verdict.listings == result.listings and verdict.dropped == 0
        judged = await ListingCheck(FakeClassifier()).page(verdict.listings, page=1, drop=False)
        assert judged.ok and judged.listings == result.listings

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
        verdict = legibility.check(result.listings, page=1)
        assert verdict.ok, verdict.reason
        judged = await ListingCheck(FakeClassifier()).page(verdict.listings, page=1, drop=False)
        assert judged.ok and judged.listings == result.listings


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
        verdict = legibility.check(listings, page=1)
        assert verdict.ok, verdict.reason
        assert verdict.dropped == 0
        assert len(verdict.listings) == len(listings)
        judged = await ListingCheck(FakeClassifier()).page(verdict.listings, page=1, drop=False)
        assert judged.ok and judged.listings == verdict.listings
