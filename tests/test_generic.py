"""The generic listing reader: its decisions, its overrides, and its probe.

Most of this file runs everywhere, CI included: `GenericSource` is driven with
canned probe output (the JSON `JS_PROBE` returns) and a fake classifier that
records every request. That is where the contract lives — which group is read,
what fills a Listing and what is left empty, which cards are dropped, how the
next page is reached, what an error looks like — and, because the classifier
reads its state once and answers every question in it, that each decision is
ONE request per page, not one per field or per link.

The last classes need a Playwright chromium: they run the real `JS_PROBE` on
listing pages saved on 2026-09-28 (tests/fixtures/generic; the Empire Flippers,
Flippa, BusinessBroker.net, Sunbelt, BusinessesForSale and QuietLight pages were
saved through the cloaked browser after scrolling to the bottom), with the
network blocked, and pin what the probe finds on markup it has never been tuned
to — the whole point of the source.
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import extract
from app.services.typesafe import Choice, Noul, TypeSafeAuthError
from app.sources import PageNotReached, generic
from app.sources.generic import JS_PROBE, GenericSource
from app.sources.overrides import (
    MONEY_ROLES,
    ROLES,
    Role,
    SiteOverride,
    override_for,
)

SITE = "https://brokers.example"
LIST_URL = f"{SITE}/businesses-for-sale/"
PATTERN = "brokers.example/listing/{*}"


# ── canned probe output ──────────────────────────────────────────────────────


def _card(n: int, *, path: str = "/listing/biz-{n}", labeled=None, slots=None, hrefs=None,
          link_text=None, heading=None, card_id=None) -> dict:
    href = f"{SITE}{path.format(n=n)}"
    return {
        "card": card_id or f"g0c{n}",
        "href": href,
        "hrefs": [href] if hrefs is None else hrefs,
        "link_text": f"Business {n}" if link_text is None else link_text,
        "heading": f"Business {n}" if heading is None else heading,
        "text": f"Business {n} Austin, TX Asking Price: $1,{n}00,000 Cash Flow: ${n}50,000",
        "excerpt": f"**Business {n}**\n\nAsking Price: $1,{n}00,000",
        "labeled": ({"Asking Price": f"$1,{n}00,000", "Cash Flow": f"${n}50,000"}
                    if labeled is None else labeled),
        "slots": ({"div.card>h3#0": f"Business {n}", "div.card>p.loc#0": "Austin, TX"}
                  if slots is None else slots),
    }


def _group(pattern: str = PATTERN, cards=None, *, links=None, chrome=0.0, text_chars=200,
           varying_keys=(), paths_unique=True) -> dict:
    cards = [_card(n) for n in range(1, 5)] if cards is None else cards
    return {
        "pattern": pattern,
        "links": len(cards) if links is None else links,
        "chrome": chrome,
        "text_chars": text_chars,
        "varying_keys": list(varying_keys),
        "paths_unique": paths_unique,
        "examples": [c["text"] for c in cards[:3]],
        "cards": cards,
    }


def _nav(pattern: str = "brokers.example/{*}", links: int = 8, chrome: float = 0.2) -> dict:
    cards = [_card(n, path="/page-{n}", labeled={}, slots={"li#0": f"Page {n}"},
                   card_id=f"g1c{n}") for n in range(1, links + 1)]
    return _group(pattern, cards, chrome=chrome, text_chars=10)


def _probe(groups=None, *, pager=(), title="Businesses for sale | Brokers", url=LIST_URL,
           body="Businesses for sale. " * 400) -> dict:
    return {
        "url": url,
        "title": title,
        "body": body[:5000],
        "body_chars": len(body),
        "groups": [_group(), _nav()] if groups is None else groups,
        "pager": list(pager),
    }


def _pager(n: int, url: str | None, *appears_as: str, selector: str | None = None,
           numbered: bool = False) -> dict:
    return {"id": f"n{n}", "url": url, "script_only": url is None,
            "appears_as": list(appears_as) or ["text 'Next'"], "selector": selector,
            "numbered": numbered}


NEXT_URL = f"{LIST_URL}page/2/"


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeJev:
    """A classifier that answers by question kind and records every request.

    `group` is a substring of the pattern to pick ("none" picks none). `fields`
    maps a field's label, or its first value, to (role, confidence); anything
    else is "other" at 0.99. `next` maps a link's URL, or text in its
    `appears_as`, to P(next page). There is no fourth kind: whether a card is
    for sale now is asked per card by the sweep (services/legibility.py).
    """

    def __init__(self, *, group: str = "/listing/", group_confidence: float = 0.97,
                 fields=None, next=None, error: Exception | None = None,
                 error_on: str | None = None):
        self.group = group
        self.group_confidence = group_confidence
        self.fields = {
            "Asking Price": ("asking_price", 0.97),
            "Cash Flow": ("cash_flow_sde", 0.95),
            "Business 1": ("title", 0.96),
            "Austin, TX": ("location", 0.92),
        } if fields is None else fields
        self.next = {NEXT_URL: 0.97} if next is None else next
        self.error = error
        self.error_on = error_on
        self.requests: list[tuple[str, dict, dict]] = []

    @staticmethod
    def kind(questions: dict) -> str:
        first = next(iter(questions))
        if first == "listing_group":
            return "group"
        return {"field": "fields", "link": "next"}[first.split("_")[0]]

    def kinds(self) -> collections.Counter:
        return collections.Counter(kind for kind, _, _ in self.requests)

    def of(self, kind: str) -> list[tuple[dict, dict]]:
        return [(s, q) for k, s, q in self.requests if k == kind]

    async def ask(self, state, questions):
        kind = self.kind(questions)
        self.requests.append((kind, state, questions))
        if self.error is not None and self.error_on in (None, kind):
            raise self.error
        return getattr(self, f"_{kind}")(state, questions)

    def _group(self, state, questions):
        criteria = questions["listing_group"]["criteria"]
        pick = "none"
        if self.group != "none":
            pick = next((k for k, v in criteria.items() if k != "none" and self.group in v),
                        "none")
        return {"listing_group": Choice(pick, {pick: self.group_confidence},
                                        self.group_confidence, "typesafe/jev-test")}

    def _fields(self, state, questions):
        out = {}
        for name in questions:
            field = state["fields"][name]
            label = field["text_just_before_this_field"]
            first = field["values_on_some_cards"][0]
            role, conf = self.fields.get(label) or self.fields.get(first) or ("other", 0.99)
            out[name] = Choice(role, {role: conf}, conf, "typesafe/jev-test")
        return out

    def _next(self, state, questions):
        out = {}
        for name in questions:
            link = state["links"][name]
            p = self.next.get(link["url"], 0.03)
            for key, prob in self.next.items():
                if any(key in a for a in link["appears_as"]):
                    p = max(p, prob)
            out[name] = Noul(p, "typesafe/jev-test")
        return out


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str):
        self.page, self.selector = page, selector

    async def count(self) -> int:
        if self.selector in self.page.missing:
            return 0
        return self.page.counts.get(self.selector, 1)

    @property
    def first(self) -> "FakeLocator":
        return self

    async def is_visible(self) -> bool:
        return self.selector not in self.page.hidden

    async def click(self, **kw) -> None:
        if self.selector in self.page.lost:
            # What a humanized click reports when the site re-renders the
            # element it had just scrolled into view (FacetWP on Synergy).
            raise RuntimeError("Element lost after scrolling into view")
        if self.selector in self.page.moving and not kw.get("force"):
            raise RuntimeError("element position is still changing")
        if self.selector in self.page.no_pointer:
            raise RuntimeError("element is outside of the viewport")
        self.page.clicks.append(self.selector)

    async def evaluate(self, script, arg=None):
        if self.selector in self.page.lost:
            raise RuntimeError("Element is not attached to the DOM")
        self.page.clicks.append(f"script:{self.selector}")


class FakePage:
    """Answers JS_PROBE with the next canned probe (the last one repeats)."""

    def __init__(self, *probes: dict, url: str = LIST_URL):
        self.probes = list(probes)
        self.url = url
        self.probe_args: list[dict] = []
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.missing: set[str] = set()
        self.lost: set[str] = set()      # found, but the click fails
        self.hidden: set[str] = set()    # found, not visible
        self.moving: set[str] = set()    # fails the "stable" check unless forced
        self.no_pointer: set[str] = set()  # any pointer click fails; el.click() works
        self.counts: dict[str, int] = {}  # how many elements a selector finds (default 1)

    async def evaluate(self, script, arg=None):
        if script != JS_PROBE:  # extract.inject's libraries
            return None
        self.probe_args.append(arg)
        probe = self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]
        return json.dumps(probe)

    async def goto(self, url, **_kw):
        self.gotos.append(url)
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def wait_for_load_state(self, *_a, **_kw):
        return None


class _StillPage(FakePage):
    """A page whose links, height and address never change after a click —
    or change as `sizes` says, one measurement at a time (the last repeats)."""

    def __init__(self, *probes: dict, sizes: list[list] | None = None):
        super().__init__(*probes)
        self.sizes = list(sizes or [[20, 1500, LIST_URL, 7]])

    async def evaluate(self, script, arg=None):
        if script == generic._JS_PAGE_SIZE:
            return self.sizes.pop(0) if len(self.sizes) > 1 else self.sizes[0]
        return await super().evaluate(script, arg)

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0.001)


async def _read(probe: dict, jev=None, override=None, url: str = LIST_URL):
    jev = jev or FakeJev()
    source = GenericSource(url, jev, override)
    source.begin()
    page = FakePage(probe)
    result = await source.cards(page)
    return result, source, jev, page


# ── the listing group ────────────────────────────────────────────────────────


class TestListingGroup:
    @pytest.mark.asyncio
    async def test_the_chosen_group_becomes_the_listings(self):
        result, source, jev, _ = await _read(_probe())
        assert [l.url for l in result.listings] == [f"{SITE}/listing/biz-{n}" for n in range(1, 5)]
        assert result.error == "" and not result.blocked
        assert source.decisions[0]["listing_links"] == {
            "by": "jev", "patterns": [PATTERN], "confidence": 0.97,
            "model": "typesafe/jev-test", "candidates": 2,
        }

    @pytest.mark.asyncio
    async def test_one_choice_question_names_every_candidate_and_none(self):
        _, _, jev, _ = await _read(_probe())
        [(state, questions)] = jev.of("group")
        assert state == {"page_title": "Businesses for sale | Brokers", "page_url": LIST_URL}
        question = questions["listing_group"]
        assert question["type"] == "choice"
        assert question["instructions"] == generic.GROUP_QUESTION
        criteria = question["criteria"]
        assert set(criteria) == {"group_A", "group_B", "none"}
        assert criteria["none"] == "None of these groups is a list of businesses for sale"
        listing = next(v for v in criteria.values() if PATTERN in v)
        assert listing.startswith(f"4 links like {PATTERN}; examples: Business 1 ")
        assert listing.count(" || ") == 2

    @pytest.mark.asyncio
    async def test_examples_are_cut_to_260_characters(self):
        cards = [_card(n) for n in range(1, 4)]
        group = _group(cards=cards)
        group["examples"] = ["#" * 1000] * 3
        _, _, jev, _ = await _read(_probe([group]))
        [(_, questions)] = jev.of("group")
        assert questions["listing_group"]["criteria"]["group_A"].count("#") == 3 * 260

    @pytest.mark.asyncio
    async def test_small_chrome_and_unread_groups_are_never_offered(self):
        groups = [
            _group(),
            _nav("brokers.example/tiny/{*}", links=2),               # too few links
            _nav("brokers.example/menu/{*}", links=9, chrome=0.8),   # mostly site chrome
            {**_nav("brokers.example/rail/{*}", links=5), "cards": None},  # not detailed
        ]
        _, source, jev, _ = await _read(_probe(groups))
        [(_, questions)] = jev.of("group")
        offered = [v for k, v in questions["listing_group"]["criteria"].items() if k != "none"]
        assert len(offered) == 1 and PATTERN in offered[0]
        assert source.decisions[0]["listing_links"]["candidates"] == 1

    @pytest.mark.asyncio
    async def test_at_most_twelve_candidates_biggest_first(self):
        groups = [_nav(f"brokers.example/g{i}/{{*}}", links=3 + i) for i in range(20)]
        _, _, jev, _ = await _read(_probe(groups), FakeJev(group="none"))
        criteria = jev.of("group")[0][1]["listing_group"]["criteria"]
        assert len(criteria) == 13
        assert criteria["group_A"].startswith("22 links like brokers.example/g19/")

    @pytest.mark.asyncio
    async def test_none_on_page_one_is_a_loud_error_without_retry(self):
        result, source, jev, _ = await _read(_probe(), FakeJev(group="none"))
        assert result.listings == []
        assert result.error == f"Found no list of businesses for sale on {LIST_URL}."
        assert result.retry is False
        # Nothing else is asked about a page with no list on it.
        assert jev.kinds() == {"group": 1}
        assert source.decisions[0]["listing_links"]["patterns"] == []

    @pytest.mark.asyncio
    async def test_no_candidates_at_all_asks_nothing(self):
        result, source, jev, _ = await _read(_probe([_nav(chrome=0.9)]))
        assert result.error == f"Found no list of businesses for sale on {LIST_URL}."
        assert result.retry is False
        assert jev.requests == []
        assert source.decisions[0]["listing_links"] == {"by": "probe", "patterns": [],
                                                        "candidates": 0}

    @pytest.mark.asyncio
    async def test_none_on_a_later_page_says_so_and_ends_the_list(self):
        """Reached through a link judged to be the next page, a page with no list
        is worth a word: the sweep keeps the pages before it and warns (see
        test_scrape.py's TestLaterPageFailures), rather than ending silently.
        (Page 1's list is not on it, so it is asked about afresh.)"""
        jev = FakeJev()
        source = GenericSource(LIST_URL, jev)
        page = FakePage(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]),
                        _probe([_nav()], url=NEXT_URL))
        first = await source.cards(page)
        assert len(first.listings) == 4
        assert await source.advance(page, 2) is True
        jev.group = "none"
        second = await source.cards(page)
        assert second.listings == [] and not second.blocked
        assert second.error == f"Found no list of businesses for sale on {NEXT_URL} (page 2)."
        assert second.retry is False
        assert source.decisions[1]["page"] == 2
        assert source.decisions[1]["listing_links"]["missing"] == {
            "decided_on_page": 1, "patterns": [PATTERN]}
        assert await source.advance(page, 3) is False


class TestListingGroupOverride:
    @pytest.mark.asyncio
    async def test_override_patterns_are_read_without_asking(self):
        override = SiteOverride(match="brokers.example", listing_links=[PATTERN])
        result, source, jev, page = await _read(_probe(), override=override)
        assert len(result.listings) == 4
        assert "group" not in jev.kinds()
        assert source.decisions[0]["listing_links"] == {"by": "override", "patterns": [PATTERN]}
        # The pinned patterns travel into the probe, so their cards are read
        # even when the group would not have been a candidate.
        assert page.probe_args[0] == {"next_number": 2, "patterns": [PATTERN]}

    @pytest.mark.asyncio
    async def test_several_patterns_are_one_list_and_a_shared_card_is_one_listing(self):
        detail = _group(PATTERN, [_card(n) for n in (1, 2, 3)])
        unlock = _group("brokers.example/listing/unlock/{*}",
                        [_card(3, path="/listing/unlock/biz-{n}/more", card_id="g0c3"),
                         _card(4, path="/listing/unlock/biz-{n}", card_id="g1c4")])
        override = SiteOverride(match="brokers.example",
                                listing_links=[PATTERN, "brokers.example/listing/unlock/{*}"])
        result, _, jev, _ = await _read(_probe([detail, unlock]), override=override)
        assert [l.url for l in result.listings] == [
            f"{SITE}/listing/biz-1", f"{SITE}/listing/biz-2", f"{SITE}/listing/biz-3",
            f"{SITE}/listing/unlock/biz-4",
        ]

    @pytest.mark.asyncio
    async def test_an_override_matching_nothing_fails_page_one_and_names_what_is_there(self):
        override = SiteOverride(match="brokers.example", listing_links=["brokers.example/old/{*}"])
        result, _, jev, _ = await _read(_probe(), override=override)
        assert result.retry is False
        assert "brokers.example/old/{*}" in result.error
        assert PATTERN in result.error
        assert jev.requests == []


# ── the rest of the chosen list ──────────────────────────────────────────────

OFFICE = "brokers.example/{*}/details/{*}"


def _shaped(group: dict, shape: str = "ARTICLE.tile", containers=("l1",)) -> dict:
    """A canned group with the card shape and containers the probe reports."""
    for card in group["cards"]:
        card["pos"] = int(card["href"].rsplit("-", 1)[1])
    return {**group, "card_shape": shape, "containers": list(containers)}


def _one_list_two_shapes(**office) -> tuple[dict, dict]:
    """Sunbelt: one list whose odd cards link /listing/…, even ones /<office>/details/…."""
    listing = _shaped(_group(PATTERN, [_card(n) for n in (1, 3, 5)]))
    other = _shaped(_group(OFFICE, [_card(n, path="/reno/details/biz-{n}", card_id=f"g1c{n}")
                                    for n in (2, 4, 6)]), **office)
    return listing, other


class TestTheWholeList:
    @pytest.mark.asyncio
    async def test_same_tiles_in_the_same_place_are_one_list_without_asking_again(self):
        listing, office = _one_list_two_shapes()
        result, source, jev, _ = await _read(_probe([listing, office, _nav()]))
        # Read in page order, whichever group each card came from.
        assert [l.url for l in result.listings] == [
            f"{SITE}/listing/biz-1", f"{SITE}/reno/details/biz-2", f"{SITE}/listing/biz-3",
            f"{SITE}/reno/details/biz-4", f"{SITE}/listing/biz-5", f"{SITE}/reno/details/biz-6",
        ]
        assert jev.kinds()["group"] == 1
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == [PATTERN, OFFICE]
        assert links["same_list"] == [OFFICE]
        assert source.suggested_override()["listing_links"] == [PATTERN, OFFICE]

    @pytest.mark.parametrize("change", [
        {"shape": "DIV.promo"},        # another kind of tile
        {"containers": ("l9",)},       # somewhere else on the page
    ])
    @pytest.mark.asyncio
    async def test_another_tile_or_another_place_is_another_list(self, change):
        listing, office = _one_list_two_shapes(**change)
        result, source, _, _ = await _read(_probe([listing, office]))
        assert len(result.listings) == 3
        assert source.decisions[0]["listing_links"]["patterns"] == [PATTERN]
        assert "same_list" not in source.decisions[0]["listing_links"]

    @pytest.mark.asyncio
    async def test_an_ad_slotted_into_the_list_is_not_part_of_it(self):
        """BizQuest's franchise ads: the same tile in the same list, other fields."""
        listing, _ = _one_list_two_shapes()
        ads = _shaped(_group("brokers.example/franchise/{*}", [
            _card(n, path="/franchise/brand-{n}", card_id=f"g1c{n}",
                  labeled={"Min. Cash Req": "$100,000"}, slots={"div.card>h3#0": f"Brand {n}"})
            for n in (2, 4, 6)]))
        result, source, _, _ = await _read(_probe([listing, ads]))
        assert [l.url for l in result.listings] == [f"{SITE}/listing/biz-{n}" for n in (1, 3, 5)]
        assert source.decisions[0]["listing_links"]["patterns"] == [PATTERN]

    @pytest.mark.asyncio
    async def test_an_override_is_read_as_pinned_and_never_widened(self):
        listing, office = _one_list_two_shapes()
        override = SiteOverride(match="brokers.example", listing_links=[PATTERN])
        result, _, _, _ = await _read(_probe([listing, office]), override=override)
        assert len(result.listings) == 3


def _watch_cards(ns, *, same_tiles: bool) -> list[dict]:
    """A "Watch" button on every tile, linking /watch_item?id=N."""
    cards = []
    for n in ns:
        card = _card(n, path=f"/watch_item?id={n}&type=listing", link_text="Watch", heading="",
                     card_id=f"g1c{n}" if same_tiles else f"g0c{n}")
        card["hrefs"] = [f"{SITE}/listing/biz-{n}", card["href"]]
        cards.append(card)
    return cards


WATCH = "brokers.example/watch_item?id&type"


class TestActionLinks:
    @pytest.mark.parametrize("same_tiles", [True, False])
    @pytest.mark.asyncio
    async def test_a_picked_action_group_is_read_through_the_detail_links(self, same_tiles):
        """Flippa: the classifier picked the Watch buttons, and every title was "Watch"."""
        watch = _group(WATCH, _watch_cards(range(1, 5), same_tiles=same_tiles))
        detail = _group(PATTERN, [_card(n, card_id=f"g1c{n}") for n in range(1, 5)])
        jev = FakeJev(group="watch_item", fields={})  # nothing confident: titles fall back
        result, source, _, _ = await _read(_probe([watch, detail]), jev)
        assert [l.url for l in result.listings] == [f"{SITE}/listing/biz-{n}" for n in range(1, 5)]
        assert [l.title for l in result.listings] == [f"Business {n}" for n in range(1, 5)]
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == [PATTERN]
        assert links["instead_of"] == WATCH
        assert source.suggested_override()["listing_links"] == [PATTERN]

    @pytest.mark.asyncio
    async def test_only_the_detail_links_inside_the_action_cards_are_read(self):
        """BusinessesForSale: the classifier picked the "Contact seller" links,
        and the listings' own shape, /us/{*}, is also the site's menu — "Sell
        Your Business", "Login" and "FAQs" were read as 12 more listings."""
        contact = []
        for n in range(1, 5):
            card = _card(n, path="/us/biz-{n}/contact", link_text="Contact seller", heading="",
                         card_id=f"g1c{n}")
            card["hrefs"] = [f"{SITE}/us/biz-{n}.aspx", card["href"]]
            if n == 2:  # a second detail link in the same tile is not a second listing
                card["hrefs"].append(f"{SITE}/us/biz-2-photos")
            contact.append(card)
        menu = [_card(i, path=f"/us/{slug}", link_text=title, heading="", labeled={}, slots={},
                      card_id=f"g0m{i}")
                for i, (slug, title) in enumerate([("sell-your-business", "Sell Your Business"),
                                                   ("login", "Login"), ("faq", "FAQs")])]
        tiles = [_card(n, path="/us/biz-{n}.aspx", card_id=f"g1c{n}") for n in range(1, 5)]
        photos = _card(2, path="/us/biz-2-photos", link_text="12 photos", card_id="g0p2")
        detail = _group("brokers.example/us/{*}", [*menu[:2], *tiles[:2], photos, *tiles[2:],
                                                    menu[2]])
        contacts = "brokers.example/us/{*}/contact"
        probe = _probe([detail, _group(contacts, contact)])
        result, source, _, _ = await _read(probe, FakeJev(group="us/{*}/contact;", fields={}))

        assert [l.url for l in result.listings] == [f"{SITE}/us/biz-{n}.aspx" for n in range(1, 5)]
        assert [l.title for l in result.listings] == [f"Business {n}" for n in range(1, 5)]
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == ["brokers.example/us/{*}"]
        assert (links["instead_of"], links["left_out"]) == (contacts, 4)
        # Pinned, the detail pattern would bring the menu back: the suggestion
        # pins the action pattern, which is read the same way — pasted back,
        # the same four listings, without asking.
        suggested = source.suggested_override()["listing_links"]
        assert suggested == [contacts]
        again = FakeJev(fields={})
        pinned = SiteOverride(match="brokers.example", listing_links=suggested)
        result, source, _, _ = await _read(probe, again, pinned)
        assert [l.url for l in result.listings] == [f"{SITE}/us/biz-{n}.aspx" for n in range(1, 5)]
        assert "group" not in again.kinds()
        assert source.decisions[0]["listing_links"] == {
            "by": "override", "patterns": ["brokers.example/us/{*}"], "instead_of": contacts,
            "left_out": 4}
        assert source.suggested_override()["listing_links"] == [contacts]

    @pytest.mark.asyncio
    async def test_a_tile_with_only_an_action_link_is_still_read(self):
        watch = _group(WATCH, _watch_cards(range(1, 5), same_tiles=False))
        detail = _group(PATTERN, [_card(n, card_id=f"g1c{n}") for n in range(1, 4)])
        override = SiteOverride(match="brokers.example", listing_links=[PATTERN, WATCH])
        result, _, _, _ = await _read(_probe([detail, watch]), FakeJev(fields={}), override)
        assert [l.url for l in result.listings][-1] == f"{SITE}/watch_item?id=4&type=listing"
        assert len(result.listings) == 4

    @pytest.mark.parametrize("pattern, texts, acting", [
        ("flippa.com/watch_item?disabled_title&id", ["Watch"] * 3, True),
        ("app.empireflippers.com/unlock/{*}", ["Unlock Listing"] * 3, True),
        ("brokers.example/us/{*}/contact", ["Business 1", "Business 2", "Business 3"], True),
        ("brokers.example/{*}", ["Contact Seller", "Contact Seller", "Business 3"], True),
        ("brokers.example/{*}", ["Sign up", "Sign up", "Sign up"], True),
        ("empireflippers.com/listing/{*}", ["View Listing"] * 3, False),
        ("flippa.com/{id}", ["Ecommerce | Home", "SaaS | Business", "Content | Blog"], False),
        # Titles that merely start with an action word are titles.
        ("brokers.example/listing/{*}", ["Save-A-Lot Grocery", "Follow-Up Clinic",
                                         "Watchmaker Shop"], False),
        ("brokers.example/saved-searches/{*}", ["A", "B", "C"], False),
    ])
    def test_looks_like_action(self, pattern, texts, acting):
        group = {"pattern": pattern, "cards": [{"link_text": t} for t in texts]}
        assert generic._looks_like_action(group) is acting


class TestDescribedTitle:
    """A tile with no name, heading or titled link (Empire Flippers)."""

    FIELDS = {"#97637": ("listing_id", 0.99), "Apparel & Accessories, Home": ("category", 0.97),
              "This listing is for an Amazon FBA business": ("description", 0.95)}

    @staticmethod
    def _tiles(**slots) -> dict:
        cards = [_card(n, labeled={}, link_text="View Listing", heading="",
                       slots={k: v.format(n=n) for k, v in slots.items()}) for n in range(1, 4)]
        return _probe([_group(cards=cards)])

    @pytest.mark.asyncio
    async def test_its_category_and_number_are_its_title(self):
        probe = self._tiles(**{"div.num#0": "#9763{n}",
                               "div.niche#0": "Apparel & Accessories, Home"})
        fields = {**self.FIELDS, "#97631": ("listing_id", 0.99)}
        result, _, _, _ = await _read(probe, FakeJev(fields=fields))
        assert result.listings[0].title == "Apparel & Accessories, Home · #97631"

    @pytest.mark.asyncio
    async def test_without_a_category_the_start_of_its_description(self):
        summary = ("This listing is for an Amazon FBA business established in 2021, operating in "
                   "the home niche with steady sales and a loyal customer base")
        probe = self._tiles(**{"div.num#0": "#97637", "div.summary#0": summary})
        fields = {**self.FIELDS, summary[:70]: ("description", 0.95)}  # as the state quotes it
        result, _, _, _ = await _read(probe, FakeJev(fields=fields))
        title = result.listings[0].title
        assert title.startswith("This listing is for an Amazon FBA business")
        assert title.endswith("… · #97637")
        assert len(title.split(" · ")[0]) <= generic._DESCRIBED_CHARS + 1

    @pytest.mark.asyncio
    async def test_a_number_alone_is_not_a_title(self):
        result, _, _, _ = await _read(self._tiles(**{"div.num#0": "#97637"}),
                                      FakeJev(fields=self.FIELDS))
        assert result.listings[0].title == ""


# ── scrolling before reading ─────────────────────────────────────────────────


class ScrollPage(FakePage):
    """A page `heights[0]` tall that grows to the next height each time it is scrolled."""

    def __init__(self, *probes: dict, heights, view: int = 900, broken: bool = False):
        super().__init__(*probes)
        self.heights = list(heights)
        self.view = view
        self.y = 0
        self.broken = broken
        self.steps: list[str] = []
        self.waits: list[int] = []
        self.mouse = self

    async def move(self, x, y):
        pass

    async def wheel(self, dx, dy):
        if self.broken:
            raise RuntimeError("Target page, context or browser has been closed")
        self.steps.append(f"wheel {dy}")
        self.y = min(self.y + dy, max(0, self.heights[0] - self.view))
        if len(self.heights) > 1:
            self.heights.pop(0)  # what the step revealed has loaded

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)

    async def evaluate(self, script, arg=None):
        if script == generic._JS_SCROLL_STATE:
            return [self.y, self.view, self.heights[0]]
        if script == generic._JS_SCROLL_TOP:
            self.steps.append("top")
            self.y = 0
            return None
        if script == JS_PROBE:
            self.steps.append("probe")
        return await super().evaluate(script, arg)


class TestScrolling:
    @pytest.mark.asyncio
    async def test_the_page_is_scrolled_to_a_bottom_that_stops_growing_then_back_up(self):
        page = ScrollPage(_probe(), heights=[3000, 5000, 7000, 7000])
        result = await GenericSource(LIST_URL, FakeJev()).cards(page)
        assert len(result.listings) == 4
        assert page.steps == ["wheel 2500"] * 3 + ["top", "probe"]
        assert page.waits == [generic.SCROLL_PAUSE_MS] * 3 + [generic.SCROLL_BOTTOM_SETTLE_MS]

    @pytest.mark.asyncio
    async def test_a_short_page_costs_one_step(self):
        page = ScrollPage(_probe(), heights=[800])
        await GenericSource(LIST_URL, FakeJev()).cards(page)
        assert page.steps == ["wheel 2500", "top", "probe"]

    @pytest.mark.asyncio
    async def test_a_feed_that_grows_forever_stops_at_the_step_bound(self):
        page = ScrollPage(_probe(), heights=[3000 + 2500 * i for i in range(40)])
        assert await generic._scroll_through(page) == generic.SCROLL_MAX_STEPS == 10
        assert page.steps[-1] == "top"

    @pytest.mark.asyncio
    async def test_slow_steps_stop_at_the_time_bound(self, monkeypatch):
        clock = [0.0]

        class Slow(ScrollPage):
            async def wait_for_timeout(self, ms):
                clock[0] += 4.0  # a page that takes four seconds a step

        monkeypatch.setattr(generic, "time", type("T", (), {"monotonic": staticmethod(
            lambda: clock[0])}))
        page = Slow(_probe(), heights=[3000 + 2500 * i for i in range(40)])
        assert await generic._scroll_through(page) == 3
        assert clock[0] == 12.0 and generic.SCROLL_BUDGET_S == 10.0
        assert page.steps[-1] == "top"

    @pytest.mark.asyncio
    async def test_a_bottom_is_believed_only_after_one_longer_wait(self):
        """A lazy list that loads its next batch slower than a step's pause:
        the settle wait at the bottom sees it grow, so the scroll goes on."""

        class LateBatch(ScrollPage):
            async def wait_for_timeout(self, ms):
                await super().wait_for_timeout(ms)
                if ms == generic.SCROLL_BOTTOM_SETTLE_MS and self.late:
                    self.heights = self.late
                    self.late = None

        page = LateBatch(_probe(), heights=[3000])
        page.late = [6000, 6000]
        await generic._scroll_through(page)
        assert page.waits.count(generic.SCROLL_BOTTOM_SETTLE_MS) == 2, page.waits
        assert page.steps.count("wheel 2500") >= 2, "the scroll went on after the late batch"

    @pytest.mark.asyncio
    async def test_a_page_that_cannot_be_scrolled_is_read_as_it_is(self):
        page = ScrollPage(_probe(), heights=[9000], broken=True)
        result = await GenericSource(LIST_URL, FakeJev()).cards(page)
        assert len(result.listings) == 4 and page.steps == ["probe"]


# ── fields ───────────────────────────────────────────────────────────────────


class TestFields:
    @pytest.mark.asyncio
    async def test_every_field_is_named_in_one_request(self):
        result, source, jev, _ = await _read(_probe())
        [(state, questions)] = jev.of("fields")
        assert set(state) == {"site", "example_listing_card", "fields"}
        assert state["site"] == "Businesses for sale | Brokers"
        assert state["example_listing_card"].startswith("Business 1 Austin, TX")
        assert state["fields"]["field_1"] == {
            "text_just_before_this_field": "Asking Price",
            "values_on_some_cards": ["$1,100,000", "$1,200,000", "$1,300,000", "$1,400,000"],
        }
        slot = next(f for f in state["fields"].values() if f["values_on_some_cards"][0] == "Austin, TX")
        assert slot["text_just_before_this_field"] is None
        assert slot["values_on_some_cards"] == ["Austin, TX"]
        assert set(questions) == set(state["fields"]) and len(questions) == 4
        assert questions["field_3"]["instructions"] == (
            "Each listing card on this page has the fields in the state. What does field_3 hold?"
        )
        listing = result.listings[0]
        assert (listing.title, listing.location) == ("Business 1", "Austin, TX")
        assert (listing.asking_price, listing.cashflow) == ("$1,100,000", "$150,000")

    @pytest.mark.asyncio
    async def test_a_card_s_excerpt_is_capped(self):
        """The excerpt is the card's markdown, which for a card that is most of a
        page would be stored (and sent to triage) whole."""
        cards = [_card(n) for n in range(1, 5)]
        cards[0]["excerpt"] = "**Business 1** " + "lorem ipsum " * 1000
        result, _, _, _ = await _read(_probe([_group(cards=cards), _nav()]))
        long, short = result.listings[0].excerpt, result.listings[1].excerpt
        assert len(long) <= generic.EXCERPT_CHARS == 2000
        assert long.startswith("**Business 1** lorem ipsum") and long.endswith("…")
        assert short == "**Business 2**\n\nAsking Price: $1,200,000", "a short one is untouched"

    @pytest.mark.asyncio
    async def test_money_looking_fields_only_choose_among_money_roles(self):
        _, _, jev, _ = await _read(_probe())
        [(state, questions)] = jev.of("fields")
        for name, field in state["fields"].items():
            criteria = questions[name]["criteria"]
            if field["values_on_some_cards"][0].startswith("$"):
                assert set(criteria) == {*MONEY_ROLES, "other"}
            else:
                assert criteria == ROLES
            assert all(criteria[k] == ROLES[k] for k in criteria)

    @pytest.mark.asyncio
    async def test_a_field_on_two_cards_is_asked_about_and_on_one_it_is_not(self):
        """Two of twenty cards with an EBITDA is the list's EBITDA field (it
        was below the old 30% bar); one card's own text is not a field."""
        cards = [_card(n) for n in range(1, 21)]
        cards[4]["labeled"]["EBITDA"] = "$90,000"
        cards[15]["labeled"]["EBITDA"] = "$95,000"
        jev = FakeJev(fields={**FakeJev().fields, "EBITDA": ("ebitda", 0.93)})
        result, _, jev, _ = await _read(_probe([_group(cards=cards)]), jev)
        asked = [f["text_just_before_this_field"] for f in jev.of("fields")[0][0]["fields"].values()]
        assert "EBITDA" in asked
        assert [l.ebitda for l in result.listings if l.ebitda] == ["$90,000", "$95,000"]
        cards[15]["labeled"].pop("EBITDA")
        _, _, jev, _ = await _read(_probe([_group(cards=cards)]))
        asked = [f["text_just_before_this_field"] for f in jev.of("fields")[0][0]["fields"].values()]
        assert "EBITDA" not in asked

    @pytest.mark.asyncio
    async def test_the_sample_values_are_spread_across_the_page(self):
        """The first, the last and evenly between — not the first three, which
        are often three of a kind (a page sorted by price, three "Featured")."""
        cards = [_card(n) for n in range(1, 13)]
        _, _, jev, _ = await _read(_probe([_group(cards=cards)]))
        [(state, _)] = jev.of("fields")
        asking = next(f for f in state["fields"].values()
                      if f["text_just_before_this_field"] == "Asking Price")
        assert asking["values_on_some_cards"] == [
            "$1,100,000", "$1,400,000", "$1,700,000", "$1,900,000", "$1,1200,000"]
        assert generic.FIELD_SAMPLES == 5 and generic.FIELD_MIN_CARDS == 2

    def test_spread_keeps_order_and_distinct_values(self):
        assert generic._spread(["a", "a", "b"], 5, None) == ["a", "b"]
        assert generic._spread([str(i) for i in range(9)], 5, None) == ["0", "2", "4", "6", "8"]
        assert generic._spread(["x" * 90], 5, 70) == ["x" * 70]

    @pytest.mark.asyncio
    async def test_an_unsure_answer_leaves_the_field_empty(self):
        jev = FakeJev(fields={"Asking Price": ("asking_price", 0.79),
                              "Cash Flow": ("cash_flow_sde", 0.8)})
        result, source, _, _ = await _read(_probe(), jev)
        listing = result.listings[0]
        assert listing.asking_price == ""   # 0.79: empty beats confidently wrong
        assert listing.cashflow == "$150,000"  # 0.8 is enough
        assert "Asking Price: $1,100,000" in listing.excerpt  # the text is still kept
        report = {f["key"]: f for f in source.decisions[0]["fields"]}
        assert report["Asking Price"] == {"key": "Asking Price", "labeled": True,
                                          "role": "asking_price", "by": "jev",
                                          "confidence": 0.79, "used": False}

    @pytest.mark.asyncio
    async def test_an_override_wins_and_ignore_is_neither_asked_nor_filled(self):
        override = SiteOverride(match="brokers.example",
                                fields={"asking price": "revenue", "Cash Flow:": "ignore"})
        result, source, jev, _ = await _read(_probe(), override=override)
        asked = [f["text_just_before_this_field"] for f in jev.of("fields")[0][0]["fields"].values()]
        assert "Asking Price" not in asked and "Cash Flow" not in asked
        listing = result.listings[0]
        assert listing.revenue == "$1,100,000" and listing.asking_price == ""
        assert listing.cashflow == ""
        by = {f["key"]: f["by"] for f in source.decisions[0]["fields"]}
        assert by["Asking Price"] == by["Cash Flow"] == "override"

    @pytest.mark.asyncio
    async def test_when_every_field_is_pinned_nothing_is_asked(self):
        override = SiteOverride(match="brokers.example", fields={
            "Asking Price": "asking_price", "Cash Flow": "cash_flow_sde",
            "div.card>h3#0": "title", "div.card>p.loc#0": "location",
        })
        result, _, jev, _ = await _read(_probe(), override=override)
        assert "fields" not in jev.kinds()
        assert result.listings[0].title == "Business 1"

    @pytest.mark.asyncio
    async def test_money_is_verbatim_with_only_its_own_label_taken_off(self):
        cards = [_card(n, labeled={}, slots={"div.card>h3#0": f"Business {n}",
                                             "div.card>p.price#0": f"Asking Price $1,{n}00,000 + Inventory",
                                             "div.card>p.rev#0": "Not Disclosed"})
                 for n in range(1, 5)]
        jev = FakeJev(fields={"Business 1": ("title", 0.96),
                              "Asking Price $1,100,000 + Inventory": ("asking_price", 0.97),
                              "Not Disclosed": ("revenue", 0.9)})
        result, _, _, _ = await _read(_probe([_group(cards=cards)]), jev)
        assert result.listings[0].asking_price == "$1,100,000 + Inventory"
        assert result.listings[0].revenue == "Not Disclosed"

    @pytest.mark.asyncio
    async def test_the_most_confident_of_two_title_fields_wins_per_card(self):
        cards = [_card(n, slots={"div.card>h3#0": f"Business {n}",
                                 "div.card>p.sub#0": f"Subtitle {n}"}) for n in range(1, 5)]
        cards[1]["slots"].pop("div.card>h3#0")
        jev = FakeJev(fields={"Business 1": ("title", 0.96), "Subtitle 1": ("title", 0.85)})
        result, _, _, _ = await _read(_probe([_group(cards=cards)]), jev)
        assert [l.title for l in result.listings[:2]] == ["Business 1", "Subtitle 2"]

    @pytest.mark.asyncio
    async def test_title_falls_back_to_the_link_text_then_the_heading(self):
        cards = [
            _card(1, slots={}, link_text="Busy Bakery", heading="Ignored"),
            _card(2, slots={}, link_text="View details", heading="Corner Deli"),
            _card(3, slots={}, link_text="Car Wash In Reno, great cash flow, $1.2M",
                  heading="Car Wash In Reno"),
            _card(4, slots={}, link_text="", heading="Pet Groomer"),
        ]
        result, _, _, _ = await _read(_probe([_group(cards=cards)]))
        assert [l.title for l in result.listings] == [
            "Busy Bakery", "Corner Deli", "Car Wash In Reno", "Pet Groomer",
        ]


# ── status: read, never asked about ──────────────────────────────────────────


def _with_status(*statuses: str) -> dict:
    cards = [_card(n) for n in range(1, len(statuses) + 1)]
    for card, status in zip(cards, statuses):
        card["slots"]["div.card>span.badge#0"] = status
    return _probe([_group(cards=cards)])


STATUS_FIELDS = {"Asking Price": ("asking_price", 0.97), "Business 1": ("title", 0.96),
                 "Active": ("status", 0.95)}


class TestStatus:
    """No request asks which statuses mean gone: whether a card is a business
    for sale now is part of the sweep's one request per card, which drops the
    sold, pending and under-contract ones (tests/test_scrape.py). A person's
    `drop_status` still drops by the status field, asking nothing."""

    @pytest.mark.asyncio
    async def test_sold_cards_are_read_like_any_other_and_nothing_is_asked_about_them(self):
        result, source, jev, _ = await _read(
            _with_status("Active", "Sold", "Active", "Under Contract"), FakeJev(fields=STATUS_FIELDS))
        assert set(jev.kinds()) == {"group", "fields"}, "no page, so no next-page request"
        assert len(result.listings) == 4
        record = source.decisions[0]
        assert (record["cards"], record["kept"], record["dropped_unavailable"]) == (4, 4, 0)
        assert "status" not in record
        roles = {f["key"]: f["role"] for f in record["fields"]}
        assert roles["div.card>span.badge#0"] == "status", "the field is still read"

    @pytest.mark.asyncio
    async def test_override_drop_status_matches_substrings_without_asking(self):
        override = SiteOverride(match="brokers.example", drop_status=["sold", "contract"])
        jev = FakeJev(fields=STATUS_FIELDS)
        result, source, _, _ = await _read(
            _with_status("Active", "SOLD!", "Under Contract", "Pending"), jev, override)
        assert [l.url.rsplit("-", 1)[1] for l in result.listings] == ["1", "4"]
        assert source.decisions[0]["status"] == {
            "by": "override", "unavailable": ["SOLD!", "Under Contract"],
            "values": ["Active", "SOLD!", "Under Contract", "Pending"]}

    @pytest.mark.asyncio
    async def test_dropped_cards_are_still_seen(self):
        """A page of dropped cards is not the end of the feed."""
        override = SiteOverride(match="brokers.example", drop_status=["sold"])
        result, source, _, _ = await _read(
            _with_status("Active", "Sold", "Sold", "Sold"), FakeJev(fields=STATUS_FIELDS), override)
        assert [l.url for l in result.listings] == [f"{SITE}/listing/biz-1"]
        assert result.seen_urls == [f"{SITE}/listing/biz-{n}" for n in range(1, 5)]
        assert source.decisions[0]["dropped_unavailable"] == 3

    @pytest.mark.asyncio
    async def test_an_empty_drop_status_drops_nothing(self):
        override = SiteOverride(match="brokers.example", drop_status=[])
        result, _, _, _ = await _read(_with_status("Active", "Sold", "Active"),
                                      FakeJev(fields=STATUS_FIELDS), override)
        assert len(result.listings) == 3


# ── next page ────────────────────────────────────────────────────────────────


class TestClickFallbacks:
    """A next-page control that is there but fails the humanized click."""

    @pytest.mark.asyncio
    async def test_a_control_on_a_page_that_never_stops_moving_gets_a_forced_click(self):
        page = FakePage(_probe())
        page.moving.add("button.facetwp-load-more")
        assert await generic._try_click(page, "button.facetwp-load-more", LIST_URL)
        assert page.clicks == ["button.facetwp-load-more"]

    @pytest.mark.asyncio
    async def test_a_control_no_pointer_can_reach_is_clicked_by_its_own_click(self):
        page = FakePage(_probe())
        page.no_pointer.add("button.more")
        assert await generic._try_click(page, "button.more", LIST_URL)
        assert page.clicks == ["script:button.more"]

    @pytest.mark.asyncio
    async def test_a_control_that_is_gone_is_reported_as_not_clicked(self):
        page = FakePage(_probe())
        page.lost.add('[data-cbs-next="n1"]')
        assert not await generic._try_click(page, '[data-cbs-next="n1"]', LIST_URL)
        assert page.clicks == []


class TestNextPage:
    @pytest.mark.asyncio
    async def test_a_url_candidate_is_followed_by_navigating(self):
        pager = [_pager(1, NEXT_URL, "rel=next", "text '2' (in a pagination block)"),
                 _pager(2, f"{SITE}/about", "text 'Next'")]
        jev = FakeJev()
        source = GenericSource(LIST_URL, jev)
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        [(state, questions)] = jev.of("next")
        assert state == {
            "current_page_url": LIST_URL,
            "page_title": "Businesses for sale | Brokers",
            "links": {
                "link_1": {"url": NEXT_URL,
                           "appears_as": ["rel=next", "text '2' (in a pagination block)"]},
                "link_2": {"url": f"{SITE}/about", "appears_as": ["text 'Next'"]},
            },
        }
        # A load-more control is a next page too: read literally, "goes to page
        # 2" left Empire Flippers' "Load More Listings" just under one half.
        assert questions["link_1"] == {
            "type": "noul",
            "instructions": ("link_1 in the state goes to the next page (page 2) of the same "
                             "list of businesses, or loads more businesses into this list (for "
                             "example a 'Load more' or 'Show more listings' button)."),
        }
        assert source.decisions[0]["next_page"] == {"by": "jev", "rule": NEXT_URL,
                                                    "probability": 0.97, "candidates": 2}
        assert await source.advance(page, 2) is True
        assert page.gotos == [NEXT_URL] and page.clicks == []
        # Page 2 asks for page 3.
        await source.cards(page)
        assert page.probe_args[1]["next_number"] == 3
        assert "next page (page 3) of the same list" in jev.of("next")[1][1]["link_1"][
            "instructions"]

    @pytest.mark.asyncio
    async def test_a_script_only_control_is_clicked_by_its_mark(self):
        pager = [_pager(1, None, "text '2' (in a pagination block)", selector="a.paginate",
                        numbered=True),
                 _pager(2, None, "text '»' (in a pagination block)", selector="a.next")]
        jev = FakeJev(next={"text '»'": 0.9, "text '2'": 0.6})
        source = GenericSource(LIST_URL, jev)
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        links = jev.of("next")[0][0]["links"]
        assert links["link_1"]["url"] == generic._SCRIPT_LINK
        assert source.decisions[0]["next_page"] == {
            "by": "jev", "rule": "click", "probability": 0.9, "candidates": 2,
            "appears_as": ["text '»' (in a pagination block)"], "selector": "a.next",
        }
        assert await source.advance(page, 2) is True
        assert page.clicks == ['[data-cbs-next="n2"]'] and page.gotos == []

    @pytest.mark.asyncio
    async def test_a_page_number_control_is_clicked_but_never_offered_as_a_pin(self):
        pager = [_pager(1, None, "text '2' (in a pagination block)", selector="a.paginate",
                        numbered=True)]
        source = GenericSource(LIST_URL, FakeJev(next={"text '2'": 0.82}))
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        assert "selector" not in source.decisions[0]["next_page"]
        assert "next_page" not in source.suggested_override()
        assert await source.advance(page, 2) is True
        assert page.clicks == ['[data-cbs-next="n1"]']

    @pytest.mark.asyncio
    async def test_a_vanished_control_stops_paging_and_says_so(self):
        """The control was on the page a moment ago: the pages after it exist."""
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Next'": 0.9}))
        page = FakePage(_probe(pager=[_pager(1, None, "text 'Next'")]))
        await source.cards(page)
        page.missing.add('[data-cbs-next="n1"]')
        with pytest.raises(PageNotReached) as raised:
            await source.advance(page, 2)
        assert str(raised.value) == (
            "the next-page control on page 1 (text 'Next') could not be clicked")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url, why", [
        ("https://elsewhere.example/businesses-for-sale/page/2/",
         "on another site (elsewhere.example), not brokers.example"),
        ("https://brokers.example.evil.test/page/2/", "on another site"),
        ("javascript:alert(1)", "not a web address"),
        ("file:///etc/passwd", "not a web address"),
    ])
    async def test_a_next_page_off_the_site_is_not_followed(self, url, why):
        """The address comes from the page: a page can offer anything as its
        "next" link, and the browser goes only where the swept site is."""
        source = GenericSource(LIST_URL, FakeJev(next={url: 0.97}))
        page = FakePage(_probe(pager=[_pager(1, url, "rel=next")]))
        await source.cards(page)
        decided = source.decisions[0]["next_page"]
        assert decided["rule"] == "none"
        assert decided["refused"]["url"] == url and why in decided["refused"]["why"]
        assert await source.advance(page, 2) is False
        assert page.gotos == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site, url", [
        ("https://brokers.example/list/", "https://www.brokers.example/list/?page=2"),
        ("https://www.fcbb.com/listings/", "https://sfbay.fcbb.com/listings/page/2/"),
        ("https://a.example.co.uk/list/", "https://b.example.co.uk/list/2"),
    ])
    async def test_a_next_page_on_the_same_site_is_followed(self, site, url):
        source = GenericSource(site, FakeJev(next={url: 0.97}))
        page = FakePage(_probe(pager=[_pager(1, url, "rel=next")], url=site), url=site)
        await source.cards(page)
        assert "refused" not in source.decisions[0]["next_page"]
        assert await source.advance(page, 2) is True
        assert page.gotos == [url]

    def test_a_country_domain_is_not_one_site(self):
        assert generic._off_site("https://other.co.uk/p/2", "example.co.uk")
        assert generic._off_site("https://10.0.0.2/p/2", "10.0.0.1")
        assert not generic._off_site("https://10.0.0.1/p/2", "10.0.0.1")

    @pytest.mark.asyncio
    async def test_a_re_rendered_control_is_clicked_by_its_selector(self):
        """Synergy's FacetWP "Load more": re-drawn as it scrolls into view, so the
        probe's mark is on nothing by the time it is clicked. The stable
        selector the probe recorded still finds it."""
        pager = [_pager(1, None, "text 'Load more'", selector="button.facetwp-load-more")]
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.55}))
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        page.lost.add('[data-cbs-next="n1"]')

        assert await source.advance(page, 2) is True
        assert page.clicks == ["button.facetwp-load-more"]
        assert source.decisions[0]["next_page"]["clicked_by"] == "selector"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unusable", ["two", "hidden", "none"])
    async def test_otherwise_the_pager_is_probed_again_and_the_control_found_by_its_text(
        self, unusable,
    ):
        """A selector that is not exactly one visible element could be anything;
        a fresh probe of the pager finds the control that reads the same."""
        selector = None if unusable == "none" else "button.more"
        first = _probe(pager=[_pager(1, None, "text 'Load more'", selector=selector)])
        again = _probe(pager=[_pager(3, None, "text 'Newsletter'"),
                              _pager(4, None, "text 'Load more'", selector=selector)])
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.8}))
        page = FakePage(first, again)
        await source.cards(page)
        page.lost.add('[data-cbs-next="n1"]')
        if unusable == "two":
            page.counts["button.more"] = 2
        elif unusable == "hidden":
            page.hidden.add("button.more")

        assert await source.advance(page, 2) is True
        assert page.clicks == ['[data-cbs-next="n4"]']
        assert page.probe_args[-1]["next_number"] == 2, "the pager of the page it is on"
        assert source.decisions[0]["next_page"]["clicked_by"] == "re-probe"

    @pytest.mark.asyncio
    async def test_when_nothing_finds_the_control_again_paging_stops(self):
        pager = [_pager(1, None, "text 'Load more'", selector="button.more")]
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.8}))
        page = FakePage(_probe(pager=pager), _probe(pager=[]))
        await source.cards(page)
        page.lost.update({'[data-cbs-next="n1"]', "button.more"})

        with pytest.raises(PageNotReached):
            await source.advance(page, 2)
        assert page.clicks == []
        assert source.decisions[0]["next_page"]["clicked_by"] == "nothing"

    @pytest.mark.asyncio
    async def test_a_page_number_s_selector_is_never_a_fallback(self):
        """"a.page-numbers" is every page number; finding one would be luck."""
        pager = [_pager(1, None, "text '2' (in a pagination block)", selector="a.page-numbers",
                        numbered=True)]
        source = GenericSource(LIST_URL, FakeJev(next={"text '2'": 0.9}))
        page = FakePage(_probe(pager=pager), _probe(pager=[]))
        await source.cards(page)
        page.lost.add('[data-cbs-next="n1"]')
        with pytest.raises(PageNotReached):
            await source.advance(page, 2)
        assert page.clicks == []

    @pytest.mark.asyncio
    async def test_a_click_waits_for_new_cards_before_the_page_is_read(self, monkeypatch):
        """A "Load more" fetches its cards after the click returns; read too soon,
        the page shows only cards already seen, and paging ends."""
        class LoadingPage(FakePage):
            def __init__(self, *probes):
                super().__init__(*probes)
                self.polls = 0

            async def evaluate(self, script, arg=None):
                if script == generic._JS_PAGE_SIZE:
                    self.polls += 1
                    return [40, 3000, self.url] if self.polls >= 4 else [20, 1500, self.url]
                return await super().evaluate(script, arg)

            async def wait_for_timeout(self, ms):
                self.waited = getattr(self, "waited", 0) + ms

        monkeypatch.setattr(generic, "CLICK_SETTLE_S", 5.0)
        pager = [_pager(1, None, "text 'Load more'")]
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.8}))
        page = LoadingPage(_probe(pager=pager))
        await source.cards(page)

        assert await source.advance(page, 2) is True
        assert page.polls == 4, "measured before the click, then until the page grew"
        assert page.waited == 3 * generic.CLICK_POLL_MS

    @pytest.mark.asyncio
    async def test_the_wait_after_a_click_is_bounded(self, monkeypatch):
        class StillPage(FakePage):
            async def evaluate(self, script, arg=None):
                if script == generic._JS_PAGE_SIZE:
                    return [20, 1500, self.url]
                return await super().evaluate(script, arg)

            async def wait_for_timeout(self, ms):
                await asyncio.sleep(0.001)

        monkeypatch.setattr(generic, "CLICK_SETTLE_S", 0.05)
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.8}))
        page = StillPage(_probe(pager=[_pager(1, None, "text 'Load more'")]))
        await source.cards(page)
        assert await asyncio.wait_for(source.advance(page, 2), 2) is True, (
            "nothing new is not a failure: the page is read and the sweep decides")

    @pytest.mark.asyncio
    async def test_a_click_the_page_ignored_is_made_once_more_by_its_mark(self, monkeypatch):
        """FCBB's "4" once did nothing for the whole wait: page 3 was read again,
        looked like the end of the list, and pages 4-6 were never read."""
        monkeypatch.setattr(generic, "CLICK_SETTLE_S", 0.05)
        source = GenericSource(LIST_URL, FakeJev(next={"text '2'": 0.9}))
        page = _StillPage(_probe(pager=[_pager(1, None, "text '2' (in a pagination block)")]))
        await source.cards(page)

        assert await source.advance(page, 2) is True
        assert page.clicks == ['[data-cbs-next="n1"]', '[data-cbs-next="n1"]']
        decided = source.decisions[0]["next_page"]
        assert decided["clicked_by"] == "mark" and decided["clicked_again_by"] == "mark"

    @pytest.mark.asyncio
    async def test_a_page_that_moved_after_the_wait_is_not_clicked_again(self, monkeypatch):
        """A slow page that did go on: a second "Next" would skip a page."""
        async def waited_in_vain(page, before, url):
            return False

        monkeypatch.setattr(generic, "_settle", waited_in_vain)
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Next'": 0.9}))
        page = _StillPage(_probe(pager=[_pager(1, None, "text 'Next'")]),
                          sizes=[[20, 1500, LIST_URL, 1], [20, 1500, LIST_URL, 2]])
        await source.cards(page)

        assert await source.advance(page, 2) is True
        assert page.clicks == ['[data-cbs-next="n1"]']
        assert source.decisions[0]["next_page"]["clicked_again_by"] == "nothing"

    @pytest.mark.asyncio
    async def test_a_second_click_is_only_ever_by_the_mark(self, monkeypatch):
        """Found again by its selector, the "Next" may be the next page's own."""
        monkeypatch.setattr(generic, "CLICK_SETTLE_S", 0.05)
        pager = [_pager(1, None, "text 'Next'", selector="a.next")]
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Next'": 0.9}))
        page = _StillPage(_probe(pager=pager))
        await source.cards(page)
        page.lost.add('[data-cbs-next="n1"]')

        assert await source.advance(page, 2) is True
        assert page.clicks == ["a.next"], "clicked once, by its selector, and not again"
        assert source.decisions[0]["next_page"]["clicked_again_by"] == "nothing"

    @pytest.mark.asyncio
    async def test_a_click_that_left_the_page_as_it_was_is_an_error_not_the_end(self):
        """Read as the next page, the same listings again end paging with
        nothing said; the pages after it would go unread."""
        pager = [_pager(1, None, "text '2' (in a pagination block)")]
        source = GenericSource(LIST_URL, FakeJev(next={"text '2'": 0.9}))
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        assert await source.advance(page, 2) is True

        second = await source.cards(page)
        assert second.listings == [] and second.retry is False
        assert second.error == (
            "Page 2 showed the same 4 listings as page 1: clicking the next-page control "
            "(text '2' (in a pagination block)) did not change the page.")
        assert source.decisions[1]["error"] == second.error

    @pytest.mark.asyncio
    async def test_a_load_more_that_added_listings_is_read(self):
        pager = [_pager(1, None, "text 'Load more'")]
        more = _group(cards=[_card(n) for n in range(1, 9)])
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Load more'": 0.9}))
        page = FakePage(_probe(pager=pager), _probe([more, _nav()], pager=pager))
        await source.cards(page)
        assert await source.advance(page, 2) is True

        second = await source.cards(page)
        assert not second.error and len(second.listings) == 8

    @pytest.mark.asyncio
    async def test_the_same_listings_at_a_new_address_are_left_to_the_sweep(self):
        """Reached by its address, a repeat is a site sending page 1 again past
        its last page — the end of the list, for the sweep's stop rule."""
        source = GenericSource(LIST_URL, FakeJev(next={NEXT_URL: 0.9}))
        page = FakePage(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]))
        await source.cards(page)
        assert await source.advance(page, 2) is True

        second = await source.cards(page)
        assert not second.error and len(second.listings) == 4

    @pytest.mark.asyncio
    async def test_has_next_page_says_whether_the_page_read_showed_one(self):
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Next'": 0.9}))
        await source.cards(FakePage(_probe(pager=[_pager(1, None, "text 'Next'")])))
        assert source.has_next_page() is True
        await source.cards(FakePage(_probe(pager=[])))
        assert source.has_next_page() is False

    @pytest.mark.asyncio
    async def test_listing_links_are_never_candidates(self):
        pager = [_pager(1, f"{SITE}/listing/biz-1", "text '>' (in a pagination block)"),
                 _pager(2, f"{SITE}/listing/biz-2", "text '>'")]
        _, source, jev, page = await _read(_probe(pager=pager))
        assert "next" not in jev.kinds()
        assert source.decisions[0]["next_page"] == {"by": "probe", "rule": "none",
                                                    "candidates": 0}
        assert await source.advance(page, 2) is False

    @pytest.mark.asyncio
    async def test_below_one_half_there_is_no_next_page(self):
        pager = [_pager(1, f"{SITE}/listing/featured/9", "text 'Next'")]
        source = GenericSource(LIST_URL, FakeJev(next={f"{SITE}/listing/featured/9": 0.49}))
        page = FakePage(_probe(pager=pager))
        await source.cards(page)
        assert source.decisions[0]["next_page"]["rule"] == "none"
        assert await source.advance(page, 2) is False

    @pytest.mark.asyncio
    async def test_override_template_click_and_none(self):
        template = f"{SITE}/businesses-for-sale/?page={{page}}"
        for rule, gotos, clicks, advanced in (
            (template, [f"{SITE}/businesses-for-sale/?page=2"], [], True),
            ("click:a.load-more", [], ["a.load-more"], True),
            ("none", [], [], False),
        ):
            override = SiteOverride(match="brokers.example", next_page=rule)
            jev = FakeJev()
            source = GenericSource(LIST_URL, jev, override)
            page = FakePage(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]))
            await source.cards(page)
            assert "next" not in jev.kinds()
            assert source.decisions[0]["next_page"] == {"by": "override", "rule": rule}
            assert await source.advance(page, 2) is advanced
            assert (page.gotos, page.clicks) == (gotos, clicks)


# ── one request per decision ─────────────────────────────────────────────────


class TestRequestsPerPage:
    @pytest.mark.asyncio
    async def test_each_decision_is_one_request_on_page_one_and_only_next_after(self):
        """Page 1 asks each question once; a later page with nothing new on it
        asks only for its own next page (see TestLaterPagesReuse)."""
        jev = FakeJev(fields=STATUS_FIELDS)
        probe = _with_status("Active", "Sold", "Active", "Active")
        probe["pager"] = [_pager(1, NEXT_URL, "rel=next"), _pager(2, None, "text 'Load more'")]
        source = GenericSource(LIST_URL, jev)
        page = FakePage(probe)
        await source.cards(page)
        assert jev.kinds() == {"group": 1, "fields": 1, "next": 1}
        await source.advance(page, 2)
        await source.cards(page)
        assert jev.kinds() == {"group": 1, "fields": 1, "next": 2}
        await source.advance(page, 3)
        await source.cards(page)
        assert jev.kinds() == {"group": 1, "fields": 1, "next": 3}


# ── the later pages of one sweep ─────────────────────────────────────────────


def _page_url(n: int) -> str:
    return LIST_URL if n == 1 else f"{LIST_URL}page/{n}/"


class _Sweep:
    """Canned probes read as pages 1, 2, … of one sweep, each linking to the next."""

    def __init__(self, *probes: dict, jev: FakeJev | None = None,
                 override: SiteOverride | None = None):
        self.jev = jev or FakeJev()
        self.jev.next = {_page_url(n): 0.97 for n in range(2, len(probes) + 2)}
        self.source = GenericSource(LIST_URL, self.jev, override)
        self.source.begin()
        self.page = FakePage(*[{**p, "url": _page_url(n),
                                "pager": [_pager(1, _page_url(n + 1), "rel=next")]}
                               for n, p in enumerate(probes, 1)])
        self.count = len(probes)
        self.n = 0

    async def read(self):
        self.n += 1
        if self.n > 1:
            assert await self.source.advance(self.page, self.n) is True
        return await self.source.cards(self.page)

    async def read_all(self) -> list:
        return [await self.read() for _ in range(self.count - self.n)]


def _cards(ns, **card) -> list[dict]:
    return [_card(n, **card) for n in ns]


def _redesigned(ns) -> dict:
    """The same site in another layout: other links, other tiles, other fields."""
    cards = [_card(n, path="/biz/b-{n}", link_text=f"Biz {n}", heading="",
                   labeled={"Price": f"${n}00,000"}, slots={"li.t#0": f"Biz {n}"})
             for n in ns]
    return _probe([_group("brokers.example/biz/{*}", cards), _nav()])


def _contact_seller(ns) -> dict:
    """BusinessesForSale's shape: "Contact seller" on every tile, and the
    listings' own /us/{*} links shared with the site's menu."""
    contact = []
    for n in ns:
        card = _card(n, path="/us/biz-{n}/contact", link_text="Contact seller", heading="",
                     card_id=f"g1c{n}")
        card["hrefs"] = [f"{SITE}/us/biz-{n}.aspx", card["href"]]
        contact.append(card)
    menu = [_card(i, path=f"/us/{slug}", link_text=slug, heading="", labeled={}, slots={},
                  card_id=f"g0m{i}") for i, slug in enumerate(("sell", "login", "faq"))]
    tiles = [_card(n, path="/us/biz-{n}.aspx", card_id=f"g1c{n}") for n in ns]
    return _probe([_group("brokers.example/us/{*}", [*menu, *tiles]),
                   _group(CONTACTS, contact)])


CONTACTS = "brokers.example/us/{*}/contact"


class TestLaterPagesReuse:
    """Within one sweep the list and the fields are decided on page 1 and
    reused on the pages after it; the next page is decided on every page;
    nothing outlives the attempt (`begin()`)."""

    @pytest.mark.asyncio
    async def test_page_two_asks_neither_for_the_list_nor_for_fields_page_one_named(self):
        sweep = _Sweep(_probe(), _probe([_group(cards=_cards(range(5, 9))), _nav()]))
        first, second = await sweep.read_all()
        assert sweep.jev.kinds() == {"group": 1, "fields": 1, "next": 2}
        assert [l.url for l in second.listings] == [f"{SITE}/listing/biz-{n}" for n in range(5, 9)]
        listing = second.listings[0]
        assert (listing.title, listing.location, listing.asking_price, listing.cashflow) == (
            "Business 5", "Austin, TX", "$1,500,000", "$550,000")
        page2 = sweep.source.decisions[1]
        assert page2["listing_links"] == {"by": "page 1", "patterns": [PATTERN],
                                          "cards_by_shape": 0}
        assert {f["by"] for f in page2["fields"]} == {"page 1"}
        assert ({f["key"]: (f["role"], f["used"]) for f in page2["fields"]}
                == {f["key"]: (f["role"], f["used"]) for f in sweep.source.decisions[0]["fields"]})
        assert page2["next_page"]["by"] == "jev"

    @pytest.mark.asyncio
    async def test_only_keys_no_earlier_page_had_are_asked_about(self):
        with_ebitda = _cards(range(5, 9))
        for card in with_ebitda:
            card["labeled"]["EBITDA"] = f"${card['href'][-1]}0,000"
        jev = FakeJev(fields={**FakeJev().fields, "EBITDA": ("ebitda", 0.9)})
        sweep = _Sweep(_probe(), _probe([_group(cards=with_ebitda)]),
                       _probe([_group(cards=with_ebitda)]), jev=jev)
        _, second, third = await sweep.read_all()
        assert jev.kinds()["fields"] == 2
        state, questions = jev.of("fields")[1]
        assert list(questions) == ["field_1"]
        assert state["fields"]["field_1"]["text_just_before_this_field"] == "EBITDA"
        assert second.listings[0].ebitda == third.listings[0].ebitda == "$50,000"
        by = {f["key"]: f["by"] for f in sweep.source.decisions[1]["fields"]}
        assert by == {"Asking Price": "page 1", "Cash Flow": "page 1", "EBITDA": "jev",
                      "div.card>h3#0": "page 1", "div.card>p.loc#0": "page 1"}
        # Page 3 has nothing new: page 2's answer is reused too.
        by = {f["key"]: f["by"] for f in sweep.source.decisions[2]["fields"]}
        assert by["EBITDA"] == "page 2" and by["Asking Price"] == "page 1"

    @pytest.mark.asyncio
    async def test_an_unsure_answer_is_reused_as_unsure(self):
        jev = FakeJev(fields={**FakeJev().fields, "Asking Price": ("asking_price", 0.6)})
        sweep = _Sweep(_probe(), _probe())
        sweep.jev.fields = jev.fields
        _, second = await sweep.read_all()
        assert sweep.jev.kinds()["fields"] == 1
        assert second.listings[0].asking_price == ""
        [asking] = [f for f in sweep.source.decisions[1]["fields"] if f["key"] == "Asking Price"]
        assert asking == {"key": "Asking Price", "labeled": True, "role": "asking_price",
                          "by": "page 1", "confidence": 0.6, "used": False}

    @pytest.mark.asyncio
    async def test_a_page_without_page_one_s_list_is_decided_afresh_and_that_is_reused(self):
        jev = FakeJev(fields={**FakeJev().fields, "Price": ("asking_price", 0.95)})
        sweep = _Sweep(_probe(), _redesigned(range(1, 4)), _redesigned(range(4, 7)), jev=jev)
        await sweep.read()
        jev.group = "/biz/"
        second = await sweep.read()
        assert [(l.title, l.asking_price) for l in second.listings] == [
            ("Biz 1", "$100,000"), ("Biz 2", "$200,000"), ("Biz 3", "$300,000")]
        links = sweep.source.decisions[1]["listing_links"]
        assert links["by"] == "jev" and links["patterns"] == ["brokers.example/biz/{*}"]
        assert links["missing"] == {"decided_on_page": 1, "patterns": [PATTERN]}
        # Its fields were asked afresh too: another layout's keys.
        assert {f["by"] for f in sweep.source.decisions[1]["fields"]} == {"jev"}
        third = await sweep.read()
        assert [l.title for l in third.listings] == ["Biz 4", "Biz 5", "Biz 6"]
        assert sweep.jev.kinds() == {"group": 2, "fields": 2, "next": 3}
        assert sweep.source.decisions[2]["listing_links"]["by"] == "page 2"
        assert {f["by"] for f in sweep.source.decisions[2]["fields"]} == {"page 2"}
        # The suggestion is still page 1's.
        assert sweep.source.suggested_override()["listing_links"] == [PATTERN]

    @pytest.mark.asyncio
    async def test_the_same_links_around_other_tiles_are_decided_afresh(self):
        """Page 1's cards had a shape; page 2's links of that pattern sit in
        none of that shape, so they are not assumed to be the same list."""
        tiles = [{**c, "shape": "ARTICLE.tile", "container": "DIV.grid"}
                 for c in _cards(range(1, 5))]
        elsewhere = [{**c, "shape": "LI.menu-item", "container": "UL.menu"}
                     for c in _cards(range(5, 9))]
        sweep = _Sweep(_probe([_group(cards=tiles), _nav()]),
                       _probe([_group(cards=elsewhere), _nav()]))
        await sweep.read_all()
        assert sweep.jev.kinds()["group"] == 2
        assert sweep.source.decisions[1]["listing_links"]["missing"]["patterns"] == [PATTERN]

    @pytest.mark.asyncio
    async def test_page_two_s_probe_carries_page_one_s_card_shape_and_field_keys(self):
        tiles = [{**c, "shape": "ARTICLE.tile", "container": "DIV.grid"}
                 for c in _cards(range(1, 5))]
        tiles[3]["shape"] = "ARTICLE.featured.tile"  # a minority does not change it
        last = [{**_card(5), "shape": "ARTICLE.tile", "container": "DIV.grid", "shaped": True}]
        sweep = _Sweep(_probe([_group(cards=tiles), _nav()]),
                       _probe([_group(cards=last, links=1), _nav()]))
        _, second = await sweep.read_all()
        assert sweep.page.probe_args[0] == {"next_number": 2, "patterns": []}
        args = sweep.page.probe_args[1]
        assert args["next_number"] == 3 and args["patterns"] == [PATTERN]
        # The commonest shape, and every shape page 1's cards had: the one
        # listing left may be the odd tile ("new", "featured").
        assert args["shapes"] == {PATTERN: {"card": "ARTICLE.tile",
                                            "cards": ["ARTICLE.tile", "ARTICLE.featured.tile"],
                                            "container": "DIV.grid"}}
        assert set(args["known_keys"]) == {"Asking Price", "Cash Flow", "div.card>h3#0",
                                           "div.card>p.loc#0"}
        assert [l.url for l in second.listings] == [f"{SITE}/listing/biz-5"]
        assert sweep.source.decisions[1]["listing_links"]["cards_by_shape"] == 1

    @pytest.mark.asyncio
    async def test_a_query_string_identity_survives_a_page_with_one_listing(self):
        """Business Team: listing.aspx?LID=… for every listing. With one link
        on the last page nothing varies, so without page 1's say-so the LID
        was dropped and every such listing would share one address."""
        def listing(n: int) -> dict:
            return _card(n, path="/listing.aspx?LID=SF{n}&From=Search")
        page1 = _group("brokers.example/listing.aspx?From&LID", [listing(n) for n in range(1, 5)],
                       varying_keys=["LID"], paths_unique=False)
        last = _group("brokers.example/listing.aspx?From&LID", [listing(9)])
        sweep = _Sweep(_probe([page1, _nav()]), _probe([last, _nav()]),
                       jev=FakeJev(group="listing.aspx"))
        first, second = await sweep.read_all()
        assert first.listings[0].normalized_url == "brokers.example/listing.aspx?LID=SF1"
        assert [l.normalized_url for l in second.listings] == [
            "brokers.example/listing.aspx?LID=SF9"]

    @pytest.mark.asyncio
    async def test_both_link_shapes_of_one_list_are_reused(self):
        """Sunbelt: page 1 read two patterns as one list; a last page with
        only the second shape on it is still read, without asking."""
        listing, office = _one_list_two_shapes()
        last = _group(OFFICE, [_card(8, path="/reno/details/biz-{n}", card_id="g1c8")])
        sweep = _Sweep(_probe([listing, office, _nav()]), _probe([last, _nav()]))
        _, second = await sweep.read_all()
        assert [l.url for l in second.listings] == [f"{SITE}/reno/details/biz-8"]
        assert sweep.jev.kinds()["group"] == 1
        assert sweep.page.probe_args[1]["patterns"] == [PATTERN, OFFICE]
        assert sweep.source.decisions[1]["listing_links"]["patterns"] == [OFFICE]

    @pytest.mark.asyncio
    async def test_an_action_group_is_read_through_its_detail_links_again(self):
        """BusinessesForSale: page 1's "Contact seller" pick, read through the
        /us/{*} links inside its tiles — and not the menu's — on a last page
        with one tile."""
        sweep = _Sweep(_contact_seller(range(1, 5)), _contact_seller([5]),
                       jev=FakeJev(group="us/{*}/contact;", fields={}))
        first, second = await sweep.read_all()
        assert len(first.listings) == 4
        assert [(l.url, l.title) for l in second.listings] == [(f"{SITE}/us/biz-5.aspx",
                                                                "Business 5")]
        assert sweep.jev.kinds()["group"] == 1
        assert sweep.page.probe_args[1]["patterns"] == ["brokers.example/us/{*}", CONTACTS]
        assert sweep.source.decisions[1]["listing_links"] == {
            "by": "page 1", "patterns": ["brokers.example/us/{*}"], "cards_by_shape": 0,
            "instead_of": CONTACTS, "left_out": 3}
        assert sweep.source.suggested_override()["listing_links"] == [CONTACTS]

    @pytest.mark.asyncio
    async def test_a_known_field_on_one_card_of_a_last_page_is_still_read(self):
        """A field needs two cards to be asked about; one page 1 already named
        is read wherever it appears — the one listing of a last page included."""
        last = _card(9, labeled={"Asking Price": "$1,900,000", "Cash Flow": "$950,000"})
        sweep = _Sweep(_probe(), _probe([_group(cards=[last], links=1), _nav()]))
        _, second = await sweep.read_all()
        assert (second.listings[0].asking_price, second.listings[0].cashflow) == (
            "$1,900,000", "$950,000")
        assert sweep.jev.kinds()["fields"] == 1

    @pytest.mark.asyncio
    async def test_a_drop_status_override_applies_on_every_page(self):
        override = SiteOverride(match="brokers.example", drop_status=["sold"])
        sweep = _Sweep(_with_status("Active", "Sold", "Active"),
                       _with_status("Sold", "Active", "Active"),
                       jev=FakeJev(fields=STATUS_FIELDS), override=override)
        first, second = await sweep.read_all()
        assert [l.url for l in first.listings] == [f"{SITE}/listing/biz-1", f"{SITE}/listing/biz-3"]
        assert [l.url for l in second.listings] == [f"{SITE}/listing/biz-2", f"{SITE}/listing/biz-3"]
        assert sweep.source.suggested_override()["drop_status"] == ["Sold"]

    @pytest.mark.asyncio
    async def test_overrides_still_win_on_every_page(self):
        override = SiteOverride(match="brokers.example", listing_links=[PATTERN],
                                fields={"Cash Flow": "ignore"})
        sweep = _Sweep(_probe(), _probe([_group(cards=_cards(range(5, 9)))]),
                       override=override)
        _, second = await sweep.read_all()
        assert "group" not in sweep.jev.kinds() and sweep.jev.kinds()["fields"] == 1
        page2 = sweep.source.decisions[1]
        assert page2["listing_links"] == {"by": "override", "patterns": [PATTERN]}
        by = {f["key"]: f["by"] for f in page2["fields"]}
        assert by["Cash Flow"] == "override" and by["Asking Price"] == "page 1"
        assert second.listings[0].cashflow == ""

    @pytest.mark.asyncio
    async def test_a_retry_decides_page_one_afresh(self):
        sweep = _Sweep(_probe(), _probe())
        await sweep.read_all()
        assert sweep.jev.kinds() == {"group": 1, "fields": 1, "next": 2}
        sweep.source.begin()
        result = await sweep.source.cards(sweep.page)
        assert len(result.listings) == 4
        assert sweep.jev.kinds() == {"group": 2, "fields": 2, "next": 3}
        assert sweep.source.decisions[0]["listing_links"]["by"] == "jev"
        assert {f["by"] for f in sweep.source.decisions[0]["fields"]} == {"jev"}
        assert sweep.page.probe_args[-1] == {"next_number": 2, "patterns": []}


# ── errors and blocks ────────────────────────────────────────────────────────


class TestErrorsAndBlocks:
    @pytest.mark.parametrize("kind", ["group", "fields", "next"])
    @pytest.mark.asyncio
    async def test_a_classifier_error_fails_the_page_without_retry(self, kind):
        error = TypeSafeAuthError("OpenRouter rejected the key (HTTP 401).")
        jev = FakeJev(error=error, error_on=kind)
        result, source, _, _ = await _read(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]), jev)
        assert result.listings == [] and result.retry is False
        assert result.error == (f"Could not read the listings on {LIST_URL}: "
                                f"OpenRouter rejected the key (HTTP 401).")
        assert source.decisions[0]["error"] == "OpenRouter rejected the key (HTTP 401)."

    @pytest.mark.asyncio
    async def test_a_probe_that_never_returns_is_a_page_error_not_a_stall(self, monkeypatch):
        class Endless(FakePage):
            async def evaluate(self, script, arg=None):
                if script == JS_PROBE:
                    await asyncio.sleep(3600)
                return None

        monkeypatch.setattr(generic, "PROBE_TIMEOUT_S", 0.05)
        jev = FakeJev()
        source = GenericSource(LIST_URL, jev)
        result = await asyncio.wait_for(source.cards(Endless(_probe())), 2)
        assert result.listings == [] and result.retry is False
        assert result.error == (f"Reading the listings on {LIST_URL} took longer than 0 s, so "
                                f"the page was not used.")
        assert jev.requests == []
        assert "took longer" in source.decisions[0]["error"]

    @pytest.mark.asyncio
    async def test_no_classifier_is_a_plain_error(self):
        source = GenericSource(LIST_URL, None)
        result = await source.cards(FakePage(_probe()))
        assert result.retry is False
        assert "needs the Decision API" in result.error

    @pytest.mark.asyncio
    async def test_a_challenge_page_is_a_block_and_nothing_is_asked(self):
        result, _, jev, _ = await _read(_probe(title="Access Denied", groups=[]))
        assert result.blocked is True and result.error == ""
        assert jev.requests == []

    @pytest.mark.asyncio
    async def test_a_blocker_phrase_in_a_short_body_is_a_block(self):
        result, _, jev, _ = await _read(_probe(body="Just a moment... checking your browser"))
        assert result.blocked is True and jev.requests == []

    @pytest.mark.asyncio
    async def test_a_blocker_phrase_inside_a_real_list_is_not(self):
        body = "A cafe just a moment from the beach. " + "Businesses for sale. " * 400
        result, _, _, _ = await _read(_probe(body=body))
        assert result.blocked is False and len(result.listings) == 4


# ── URL identity ─────────────────────────────────────────────────────────────


class TestUrlIdentity:
    @pytest.mark.asyncio
    async def test_when_paths_repeat_the_varying_query_is_the_identity(self):
        cards = [_card(n, path="/buy/business-for-sale.aspx?LID=SF{n}&From=Search&utm_source=mail")
                 for n in range(1, 4)]
        group = _group("brokers.example/buy/business-for-sale.aspx?From&LID", cards,
                       varying_keys=["LID"], paths_unique=False)
        result, _, _, _ = await _read(_probe([group]), FakeJev(group="business-for-sale.aspx"))
        listing = result.listings[0]
        assert listing.url == f"{SITE}/buy/business-for-sale.aspx?LID=SF1&From=Search"
        assert listing.normalized_url == "brokers.example/buy/business-for-sale.aspx?LID=SF1"

    @pytest.mark.asyncio
    async def test_when_paths_are_unique_the_query_is_not(self):
        cards = [_card(n, path="/listing/biz-{n}?pos={n}") for n in range(1, 4)]
        group = _group(cards=cards, varying_keys=["pos"], paths_unique=True)
        result, _, _, _ = await _read(_probe([group]))
        assert result.listings[0].normalized_url == "brokers.example/listing/biz-1"
        assert result.listings[0].url == f"{SITE}/listing/biz-1?pos=1"

    @pytest.mark.asyncio
    async def test_a_contact_link_is_stored_as_the_listing_it_belongs_to(self):
        cards = []
        for n in range(1, 4):
            card = _card(n, path="/us/biz-{n}/contact")
            card["hrefs"] = [card["href"], f"{SITE}/us/biz-{n}", f"{SITE}/us"]
            cards.append(card)
        result, _, _, _ = await _read(_probe([_group("brokers.example/us/{*}/contact", cards)]),
                                      FakeJev(group="/us/"))
        # /us is on every card: an index, not this listing's page.
        assert [l.url for l in result.listings] == [f"{SITE}/us/biz-{n}" for n in range(1, 4)]
        assert result.listings[0].normalized_url == "brokers.example/us/biz-1"

    @pytest.mark.asyncio
    async def test_listing_shape(self):
        result, _, _, _ = await _read(_probe(), url="https://www.brokers.example/list")
        listing = result.listings[0]
        assert listing.listing_id == ""  # stores dedupe on it first, across sources
        assert listing.source == "brokers.example"
        assert listing.excerpt == "**Business 1**\n\nAsking Price: $1,100,000"


# ── the source object ────────────────────────────────────────────────────────


class TestSourceObject:
    def test_identity(self):
        source = GenericSource("https://www.brokers.example/list?x=1", FakeJev())
        assert source.name == "generic" and source.label == "Any site"
        assert source.warmup_url == "https://www.brokers.example/"
        assert source.page_url(source.url, 1) == source.url
        assert source.matches("http://brokers.example/other")
        assert not source.matches("https://other.example/list")
        assert not source.matches("ftp://brokers.example/list")

    @pytest.mark.asyncio
    async def test_begin_forgets_the_last_attempt(self):
        source = GenericSource(LIST_URL, FakeJev())
        page = FakePage(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]))
        await source.cards(page)
        await source.advance(page, 2)
        await source.cards(page)
        assert [d["page"] for d in source.decisions] == [1, 2]
        source.begin()
        assert source.decisions == []
        assert await source.advance(page, 2) is False  # nothing decided yet
        await source.cards(page)
        assert source.decisions[0]["page"] == 1
        assert page.probe_args[-1]["next_number"] == 2

    @pytest.mark.asyncio
    async def test_the_suggested_override_is_valid_and_pins_what_was_decided(self):
        jev = FakeJev(fields={**STATUS_FIELDS, "Cash Flow": ("cash_flow_sde", 0.5)})
        probe = _with_status("Active", "Sold", "Active")
        probe["pager"] = [_pager(1, NEXT_URL, "rel=next")]
        _, source, _, _ = await _read(probe, jev)
        suggested = source.suggested_override()
        # No drop_status: nothing decided which statuses mean gone (the
        # sweep's per-card request does, card by card).
        assert suggested == {
            "match": "brokers.example",
            "listing_links": [PATTERN],
            "fields": {"Asking Price": "asking_price", "Cash Flow": "ignore",
                       "div.card>h3#0": "title", "div.card>p.loc#0": "other",
                       "div.card>span.badge#0": "status"},
            "next_page": f"{LIST_URL}page/{{page}}/",
        }
        pinned = SiteOverride(**suggested)
        # Pasted back, it answers every question itself.
        again = FakeJev()
        await _read(probe, again, pinned)
        assert again.requests == []


# ── helpers ──────────────────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize("values, money", [
        (["$1,250,000", "$ 540,166", "Not Disclosed"], True),
        (["$1.2M", "N/A", "N/A", "N/A"], True),
        (["289-25867", "289-25764"], False),
        (["Sign In to View", "View Profit"], False),
        (["A cafe with $1.2M in sales and a loyal following near the beach"], False),
        (["$120K", "2 locations", "12 years"], False),
    ])
    def test_looks_like_money(self, values, money):
        assert generic._looks_like_money(values) is money

    @pytest.mark.parametrize("url, page, template", [
        ("https://x.example/list/page/2/", 2, "https://x.example/list/page/{page}/"),
        ("https://x.example/s?page=2", 2, "https://x.example/s?page={page}"),
        ("https://x.example/2026/list-page-3", 3, "https://x.example/2026/list-page-{page}"),
        ("https://x.example/list?a=2&page=2", 2, None),
        ("https://x.example/list?cursor=abc", 2, None),
    ])
    def test_page_template(self, url, page, template):
        assert generic._page_template(url, page) == template

    @pytest.mark.parametrize("raw, stripped", [
        ("Asking Price $1,250,000", "$1,250,000"),
        ("Cash Flow: $300,000", "$300,000"),
        ("$81,000 + Inventory", "$81,000 + Inventory"),
        ("Starting at $50,000", "Starting at $50,000"),
        ("Not Disclosed", "Not Disclosed"),
    ])
    def test_strip_money_label(self, raw, stripped):
        assert generic._strip_money_label(raw) == stripped

    def test_option_names_carry_no_order_beyond_z(self):
        assert [generic._letter(i) for i in (0, 25, 26, 27)] == ["A", "Z", "AA", "AB"]


# ── overrides ────────────────────────────────────────────────────────────────


class TestSiteOverride:
    def test_roles_are_the_literal(self):
        from typing import get_args
        assert tuple(ROLES) == get_args(Role)

    def test_every_part_is_optional(self):
        o = SiteOverride(match="bizquest.com")
        assert (o.listing_links, o.fields, o.next_page, o.drop_status) == ([], {}, None, None)

    @pytest.mark.parametrize("rule", [
        "none", "click:a.next", "click: button.load-more",
        "https://www.bizquest.com/businesses-for-sale-in-california-ca/page-{page}/",
    ])
    def test_next_page_forms(self, rule):
        assert SiteOverride(match="bizquest.com", next_page=rule).next_page == rule.strip()

    @pytest.mark.parametrize("bad", [
        {"match": ""},
        {"match": "   "},
        {"match": "ftp://bizquest.com/"},
        {"match": "bizquest.com", "next_page": "https://bizquest.com/page/2/"},  # no {page}
        {"match": "bizquest.com", "next_page": "page-{page}"},                  # not a URL
        {"match": "bizquest.com", "next_page": "click:"},
        {"match": "bizquest.com", "next_page": "None"},
        {"match": "bizquest.com", "fields": {"Price": "price"}},                # not a role
        {"match": "bizquest.com", "fields": {"": "title"}},
        {"match": "bizquest.com", "listing_links": [""]},
        {"match": "bizquest.com", "drop_status": ["sold", " "]},
        {"match": "bizquest.com", "selectors": {}},                              # unknown key
    ])
    def test_invalid_overrides_are_refused(self, bad):
        with pytest.raises(ValidationError):
            SiteOverride(**bad)

    def test_ignore_is_a_field_rule(self):
        assert SiteOverride(match="x.com", fields={"Save": "ignore"}).fields == {"Save": "ignore"}


class TestOverrideFor:
    OVERRIDES = [
        SiteOverride(match="bizquest.com"),
        SiteOverride(match="https://www.bizquest.com/businesses-for-sale-in-california-ca/"),
        SiteOverride(match="http://bizquest.com/businesses-for-sale-in-"),
        SiteOverride(match="sfbay.fcbb.com"),
    ]

    def test_longest_prefix_wins(self):
        o = override_for("https://www.bizquest.com/businesses-for-sale-in-california-ca/page-2/",
                         self.OVERRIDES)
        assert o is self.OVERRIDES[1]
        o = override_for("https://bizquest.com/businesses-for-sale-in-nevada-nv/", self.OVERRIDES)
        assert o is self.OVERRIDES[2]

    def test_a_bare_host_covers_every_page_with_or_without_www(self):
        assert override_for("https://www.bizquest.com/franchise-for-sale/",
                            self.OVERRIDES) is self.OVERRIDES[0]
        assert override_for("http://bizquest.com/", self.OVERRIDES) is self.OVERRIDES[0]

    def test_hosts_must_be_equal(self):
        assert override_for("https://fcbb.com/", self.OVERRIDES) is None
        assert override_for("https://la.fcbb.com/", self.OVERRIDES) is None
        assert override_for("https://bizquest.com.evil.example/", self.OVERRIDES) is None
        assert override_for("https://sfbay.fcbb.com/x", self.OVERRIDES) is self.OVERRIDES[3]

    def test_a_trailing_slash_does_not_matter(self):
        o = override_for("https://www.bizquest.com/businesses-for-sale-in-california-ca",
                         self.OVERRIDES)
        assert o is self.OVERRIDES[1]

    def test_nothing_matches_nothing(self):
        assert override_for("https://example.com/", self.OVERRIDES) is None
        assert override_for("https://example.com/", []) is None
        assert override_for("not a url", None) is None

    def test_a_tie_goes_to_the_first(self):
        a, b = SiteOverride(match="x.com"), SiteOverride(match="www.x.com")
        assert override_for("https://x.com/list", [a, b]) is a


class TestSuggestedOverride:
    """What a run suggests is the shape a site is pinned with in code."""

    def test_a_suggested_override_is_a_valid_site_override(self):
        source = GenericSource(LIST_URL, FakeJev())
        source.decisions = [{"page": 1, "listing_links": {"by": "jev", "patterns": [PATTERN]},
                             "fields": [{"key": "Asking Price", "role": "asking_price",
                                         "by": "jev", "used": True}],
                             "next_page": {"by": "jev", "rule": "none"}}]
        pinned = SiteOverride.model_validate(source.suggested_override())
        assert pinned.listing_links == [PATTERN]

    def test_the_code_s_overrides_are_valid_and_one_per_match(self):
        from app.sources.overrides import SITE_OVERRIDES

        matches = [o.match for o in SITE_OVERRIDES]
        assert len(matches) == len(set(matches))


# ── the probe, in a real browser ─────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures" / "generic"


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
    not _chromium_available(), reason="the real JS probe needs a Playwright chromium"
)


def _captured_url(name: str) -> str:
    head = (FIXTURES / f"{name}.html").read_text().split("\n", 1)[0]
    return head.split("Captured ", 1)[1].split(" on ", 1)[0]


async def _with_fixture(name: str, run, html: str | None = None):
    """Load a saved page offline (every request aborted) and run `run(page)`."""
    from playwright.async_api import async_playwright

    html = html if html is not None else (FIXTURES / f"{name}.html").read_text()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            return await run(page)
        finally:
            await browser.close()


async def _probe_of(name: str, next_number: int = 2, html: str | None = None) -> dict:
    async def run(page):
        await extract.inject(page)
        return json.loads(await page.evaluate(JS_PROBE, {"next_number": next_number,
                                                          "patterns": []}))
    return await _with_fixture(name, run, html)


def _group_of(probe: dict, pattern: str) -> dict:
    return next(g for g in probe["groups"] if g["pattern"] == pattern)


@needs_chromium
class TestProbeOnSavedPages:
    """What JS_PROBE finds on real listing pages it was never tuned to."""

    @pytest.mark.parametrize("name, pattern, links", [
        ("websiteclosers_list", "www.websiteclosers.com/businesses/{*}/{*}", 12),
        ("dealonomy_list", "www.dealonomy.com/s/{*}", 35),
        # One list whose slugs sometimes contain digits: 37 + 13 in the
        # prototype, one group here.
        ("bizquest_list", "www.bizquest.com/business-for-sale/{*}/{*}", 50),
        ("fcbb_list", "sfbay.fcbb.com/listing-property/{*}", 10),
        # Slugs and bare ids under /listing/: 9 + 3 in the prototype.
        ("libertygroup_list", "www.thelibertygroupofnevada.com/listing/{*}", 12),
        ("businessteam_list",
         "www.business-team.com/buy-a-business/business-for-sale.aspx?From&LID", 88),
        # Saved scrolled to the bottom: all 53, where the live sweep saw 23.
        ("businessbrokernet_list", "www.businessbroker.net/business-for-sale/{*}/{*}", 53),
    ])
    @pytest.mark.asyncio
    async def test_the_listing_list_is_the_biggest_group_and_every_card_is_read(
            self, name, pattern, links):
        probe = await _probe_of(name)
        top = generic._candidates(probe["groups"])[0]
        assert (top["pattern"], top["links"]) == (pattern, links)
        assert top["chrome"] == 0
        assert len(top["cards"]) == links
        assert len({c["href"] for c in top["cards"]}) == links
        assert all(c["excerpt"] and c["text"] for c in top["cards"])

    @pytest.mark.asyncio
    async def test_a_link_with_and_without_its_trailing_slash_is_one_link(self):
        """Empire Flippers prints /listing/97637/ and /listing/97637 on every tile.
        Counted as two links, each id looked repeated — a literal — and the
        group fell apart into groups of two."""
        probe = await _probe_of("empireflippers_list")
        listing = _group_of(probe, "empireflippers.com/listing/{*}")
        unlock = _group_of(probe, "app.empireflippers.com/unlock/{*}")
        assert listing["links"] == unlock["links"] == 13
        assert len({c["href"].rstrip("/") for c in listing["cards"]}) == 13
        # Each tile is one card, the same one for both of its links.
        assert [c["card"] for c in listing["cards"]] == [c["card"] for c in unlock["cards"]]
        assert all(len(c["hrefs"]) == 1 for c in listing["cards"])

    @pytest.mark.asyncio
    async def test_ids_at_the_site_root_are_not_merged_with_its_menu(self):
        """Flippa's listings are flippa.com/<id>-<slug>, beside flippa.com/websites."""
        probe = await _probe_of("flippa_list")
        ids = _group_of(probe, "flippa.com/{id}")
        assert ids["links"] == 5 and ids["chrome"] == 0
        assert all(re.match(r"https://flippa\.com/\d{8}-", c["href"]) for c in ids["cards"])
        menu = _group_of(probe, "flippa.com/{*}")
        assert menu["links"] >= 30
        assert "flippa.com/{id}" in [g["pattern"] for g in generic._candidates(probe["groups"])]

    @pytest.mark.parametrize("name", [
        "websiteclosers_list", "dealonomy_list", "bizquest_list", "fcbb_list",
        "libertygroup_list", "businessteam_list", "businessbrokernet_list",
    ])
    @pytest.mark.asyncio
    async def test_a_list_takes_in_no_other_group(self, name):
        candidates = generic._candidates((await _probe_of(name))["groups"])
        top = candidates[0]
        assert generic._whole_list(top, candidates) == ([top], [], None, 0)

    @pytest.mark.parametrize("name, ads", [
        ("bizquest_list", "www.bizquest.com/{*}?q"),
        ("businessbrokernet_list", "www.businessbroker.net/franchises/franchise/{*}"),
    ])
    @pytest.mark.asyncio
    async def test_franchise_ads_slotted_into_the_list_are_not_the_list(self, name, ads):
        """Same tile, same list element — told apart only by their fields."""
        probe = await _probe_of(name)
        top, ad = generic._candidates(probe["groups"])[0], _group_of(probe, ads)
        assert ad["card_shape"] == top["card_shape"]
        assert set(ad["containers"]) & set(top["containers"])
        assert not generic._one_list(top, ad)

    @pytest.mark.asyncio
    async def test_a_carousel_is_not_a_pager(self):
        """Empire Flippers' testimonial slider has a Next arrow and numbered dots."""
        probe = await _probe_of("empireflippers_list")
        seen = [a for c in probe["pager"] for a in c["appears_as"]]
        assert any("Load More Listings" in a for a in seen)
        assert not any("'Next'" in a or "'2'" in a for a in seen)

    @pytest.mark.asyncio
    async def test_a_photo_lightbox_is_not_a_pager(self):
        """FCBB's PhotoSwipe "Next (arrow right)" scored 0.80 against its pager's 0.82."""
        probe = await _probe_of("fcbb_list")
        assert [c["appears_as"] for c in probe["pager"]] == [
            ["text '2' (in a pagination block)"], ["text '»' (in a pagination block)"]]

    @pytest.mark.parametrize("name", ["quietlight_list", "quietlight_detail",
                                      "businessesforsale_list"])
    @pytest.mark.asyncio
    async def test_a_cookie_banner_is_not_a_pager(self, name):
        """CookieYes' "Show more" (button.cky-show-desc-btn) was clicked as
        QuietLight's next page in the second live gate."""
        probe = await _probe_of(name)
        seen = [a for c in probe["pager"] for a in c["appears_as"]]
        assert not any("Show more" in a for a in seen)
        assert not any("cky" in (c["selector"] or "") for c in probe["pager"])

    @pytest.mark.asyncio
    async def test_a_wordpress_body_is_not_a_pagination_block(self):
        """<body class="page-template …"> matched a "pag" substring, so every
        control on QuietLight was reported as inside a pager. It has none: all
        its listings are on one page, and its one candidate is an FAQ's."""
        probe = await _probe_of("quietlight_list")
        assert [c["appears_as"] for c in probe["pager"]] == [
            ["text 'See more questions & answers'"]]

    @pytest.mark.asyncio
    async def test_consent_tools_are_passed_over_and_pagers_are_named_by_words(self):
        tiles = "".join(f'<div class="tile"><h3><a href="/listing/biz-{n}">Business {n}</a>'
                        f'</h3></div>' for n in range(1, 4))
        html = (
            '<html><head><base href="https://brokers.example/list/"></head>'
            '<body class="page page-template-default"><div id="page"><main>' + tiles
            # Adobe's components are all "cmp-…", and "trusted" is not TrustArc.
            + '<div class="trusted-sellers cmp-container">'
              '<a class="more" href="/list/?page=2">Load more</a></div>'
            # A "2" in no pager is not a page number, #page and body.page or not
            # — unless it says so itself.
            + '<p><a href="/list/?page=3">2</a></p>'
              '<p><a aria-label="Go to page 2" href="/list/?p=2">2</a></p></main></div>'
            '<div id="onetrust-banner-sdk"><button>Show more</button></div>'
            '<div class="cky-consent-container"><button class="cky-show-desc-btn">Show more'
            '</button></div>'
            '<div class="cc-window"><a href="/cookie-policy/2">Next</a></div>'
            '<div class="qc-cmp2-ui"><button>View more</button></div>'
            '<div role="dialog" aria-label="Cookie consent"><button>Load more</button></div>'
            '<div data-testid="cookie-banner"><a href="/privacy?p=2">Next page</a></div>'
            '</body></html>')
        probe = await _probe_of("inline", html=html)
        assert [(c["url"], c["appears_as"]) for c in probe["pager"]] == [
            ("https://brokers.example/list/?page=2", ["text 'Load more'"]),
            ("https://brokers.example/list/?p=2", ["text '2' (in a pagination block)"])]

    @pytest.mark.asyncio
    async def test_a_query_string_identity_is_reported_as_varying(self):
        group = _group_of(await _probe_of("businessteam_list"),
                          "www.business-team.com/buy-a-business/business-for-sale.aspx?From&LID")
        assert group["varying_keys"] == ["LID"]
        assert group["paths_unique"] is False

    @pytest.mark.asyncio
    async def test_a_pathological_page_gets_a_bounded_answer(self):
        """250 tiles, one of them a wall of text in 150 lines, and 70 other link
        shapes: the probe's answer stays small."""
        lines = "".join(f"<p>Line {i}: {'word ' * 60}</p>" for i in range(150))
        tiles = "".join(
            f'<div class="tile"><h3><a href="/listing/biz-{n}">Business {n}</a></h3>'
            + (lines if n == 1 else f"<p>Asking Price: $1,{n % 10}00,000</p>") + "</div>"
            for n in range(1, 251))
        others = "".join(f'<p><a href="/other-{g}/a">x</a><a href="/other-{g}/b">y</a>'
                         f'<a href="/other-{g}/c">z</a></p>' for g in range(70))
        html = ('<html><head><base href="https://brokers.example/list/"></head><body><main>'
                + tiles + others + "</main></body></html>")
        probe = await _probe_of("inline", html=html)
        assert len(probe["groups"]) <= 60
        group = _group_of(probe, "brokers.example/listing/{*}")
        assert group["links"] == 250 and len(group["cards"]) == 200
        wall = group["cards"][0]
        assert len(wall["excerpt"]) <= 4000
        assert len(wall["labeled"]) + len(wall["slots"]) <= 80
        assert len(json.dumps(probe)) < 400_000

    @pytest.mark.asyncio
    async def test_a_ref_query_is_a_listing_id_not_tracking(self):
        """?ref=<id> names the listing on some sites; read as tracking, the
        whole page collapsed to one listing."""
        tile = ('<div class="tile"><h3><a href="/listing.php?ref={n}">Business {n}</a></h3>'
                '<p>Asking Price: $1,{n}00,000</p></div>')
        html = ('<html><head><base href="https://brokers.example/list/"></head><body><main>'
                + "".join(tile.format(n=n) for n in range(101, 106)) + "</main></body></html>")
        probe = await _probe_of("inline", html=html)
        group = _group_of(probe, "brokers.example/listing.php?ref")
        assert group["varying_keys"] == ["ref"] and group["paths_unique"] is False
        cards = generic._cards_of([group])
        assert len({c.normalized_url for c in cards}) == 5
        assert cards[0].url == "https://brokers.example/listing.php?ref=101"

    @pytest.mark.asyncio
    async def test_fcbb_fields_are_keyed_by_their_labels(self):
        group = _group_of(await _probe_of("fcbb_list"), "sfbay.fcbb.com/listing-property/{*}")
        for card in group["cards"]:
            labeled = card["labeled"]
            assert set(labeled) >= {"Total Income", "Revenue", "Listing Number", "Category"}
            # The listing number printed on the card is the one in its link:
            # values are paired with their own labels, not a neighbour's.
            assert card["href"].endswith(labeled["Listing Number"])
            assert labeled["Revenue"].startswith("$")
            assert not labeled["Category"].startswith("$")
            # A value that is merely the same on every card is not a label.
            assert "California" not in labeled

    @pytest.mark.asyncio
    async def test_labels_survive_a_missing_field(self):
        """Positions shift when a card has no value for a field; labels do not."""
        tile = ('<div class="tile"><h3><a href="/listing/{slug}">{title}</a></h3>'
                '{fields}<a class="btn" href="/listing/{slug}">View</a></div>')
        field = '<p class="fin"><span class="label">{label}:</span> {value}</p>'
        rows = [
            ("alpha-bakery", "Alpha Bakery", [("Asking Price", "$100,000"), ("Cash Flow", "$40,000"),
                                              ("Revenue", "$300,000")]),
            ("beta-deli", "Beta Deli", [("Asking Price", "$200,000"), ("Revenue", "$500,000")]),
            ("gamma-cafe", "Gamma Cafe", [("Asking Price", "$300,000"), ("Cash Flow", "$90,000"),
                                          ("Revenue", "$700,000")]),
        ]
        html = ('<html><head><base href="https://brokers.example/list/"></head><body><main>'
                + "".join(tile.format(slug=s, title=t, fields="".join(
                    field.format(label=k, value=v) for k, v in fs)) for s, t, fs in rows)
                + "</main></body></html>")
        probe = await _probe_of("inline", html=html)
        cards = _group_of(probe, "brokers.example/listing/{*}")["cards"]
        assert [c["labeled"] for c in cards] == [
            {"Asking Price": "$100,000", "Cash Flow": "$40,000", "Revenue": "$300,000"},
            {"Asking Price": "$200,000", "Revenue": "$500,000"},
            {"Asking Price": "$300,000", "Cash Flow": "$90,000", "Revenue": "$700,000"},
        ]
        assert all(c["href"].startswith("https://brokers.example/listing/") for c in cards)

    @staticmethod
    def _tiles(*paths: str) -> str:
        tile = ('<div class="tile"><h3><a href="{path}">Business {n}</a></h3>'
                '<p class="price">Asking Price: $1,{n}00,000</p></div>')
        return ('<html><head><base href="https://brokers.example/list/"></head><body><main>'
                + "".join(tile.format(path=p, n=n) for n, p in enumerate(paths, 1))
                + '<nav><a href="/about">About</a><a href="/contact">Contact</a></nav>'
                  "</main></body></html>")

    async def _pinned(self, html: str, patterns: list[str]) -> dict:
        async def run(page):
            await extract.inject(page)
            return json.loads(await page.evaluate(JS_PROBE, {"next_number": 2,
                                                              "patterns": patterns}))
        return await _with_fixture("inline", run, html)

    @pytest.mark.asyncio
    async def test_a_pinned_pattern_reads_a_small_last_page(self):
        html = self._tiles("/listing/alpha-bakery-for-sale", "/listing/beta-deli-for-sale")
        unpinned = await self._pinned(html, [])
        assert not generic._candidates(unpinned["groups"])
        probe = await self._pinned(html, ["brokers.example/listing/{*}"])
        cards = _group_of(probe, "brokers.example/listing/{*}")["cards"]
        assert [c["labeled"]["Asking Price"] for c in cards] == ["$1,100,000", "$1,200,000"]

    @pytest.mark.asyncio
    async def test_a_pinned_pattern_is_a_wildcard_when_no_group_has_it_exactly(self):
        # Split by category on this page: two groups of three.
        html = self._tiles(*(f"/{cat}/business-number-{i}-for-sale"
                             for cat in ("restaurants", "retail") for i in range(3)))
        probe = await self._pinned(html, [])
        assert {g["pattern"] for g in generic._candidates(probe["groups"])} == {
            "brokers.example/restaurants/{*}", "brokers.example/retail/{*}"}
        probe = await self._pinned(html, ["brokers.example/{*}/{*}"])
        assert _group_of(probe, "brokers.example/{*}/{*}")["links"] == 6

    @pytest.mark.asyncio
    async def test_a_pinned_id_pattern_matches_only_ids(self):
        # Two ids beside three words are too few to be split off on their own.
        html = self._tiles("/12345678-alpha-bakery", "/87654321-beta-deli", "/pricing")
        unpinned = await self._pinned(html, [])
        assert "brokers.example/{id}" not in [g["pattern"] for g in unpinned["groups"]]
        probe = await self._pinned(html, ["brokers.example/{id}"])
        assert [c["href"] for c in _group_of(probe, "brokers.example/{id}")["cards"]] == [
            "https://brokers.example/12345678-alpha-bakery",
            "https://brokers.example/87654321-beta-deli",
        ]

    @pytest.mark.asyncio
    async def test_a_status_badge_whose_class_names_its_value_is_one_slot(self):
        group = _group_of(await _probe_of("libertygroup_list"),
                          "www.thelibertygroupofnevada.com/listing/{*}")
        badge = "div.col-sm-4>div.listingBox>div.listingImage>div.topLeft>div#0"
        statuses = [c["slots"].get(badge) for c in group["cards"]]
        assert statuses.count("UNDER CONTRACT") == 2
        assert all(statuses)
        # The unlinked sold tiles next to a linked one do not join its card.
        assert all("Industry (2)" not in c["labeled"] for c in group["cards"])

    @pytest.mark.parametrize("name, url, appears", [
        ("websiteclosers_list", "https://www.websiteclosers.com/businesses-for-sale/page/2/",
         "rel=next"),
        ("dealonomy_list", "https://www.dealonomy.com/s?page=2",
         "text 'Next' (in a pagination block)"),
        ("bizquest_list", "https://www.bizquest.com/businesses-for-sale-in-california-ca/page-2/",
         "text '2' (in a pagination block)"),
    ])
    @pytest.mark.asyncio
    async def test_next_page_links(self, name, url, appears):
        probe = await _probe_of(name)
        cand = next(c for c in probe["pager"] if c["url"] == url)
        assert cand["script_only"] is False
        assert appears in cand["appears_as"]

    @pytest.mark.asyncio
    async def test_script_only_pager_controls_are_marked_for_clicking(self):
        async def run(page):
            await extract.inject(page)
            probe = json.loads(await page.evaluate(JS_PROBE, {"next_number": 2, "patterns": []}))
            marks = {c["id"]: await page.locator(f'[data-cbs-next="{c["id"]}"]').inner_text()
                     for c in probe["pager"] if c["script_only"]}
            return probe, marks

        probe, marks = await _with_fixture("fcbb_list", run)
        script = {c["id"]: c for c in probe["pager"] if c["script_only"]}
        by_text = {marks[i].strip(): c for i, c in script.items()}
        assert by_text["2"]["numbered"] is True
        assert by_text["2"]["appears_as"] == ["text '2' (in a pagination block)"]
        assert by_text["»"]["numbered"] is False and by_text["»"]["selector"] == "a.next"
        assert all(c["url"] is None for c in script.values())

    @pytest.mark.asyncio
    async def test_the_next_page_number_follows_the_page(self):
        probe = await _probe_of("fcbb_list", next_number=3)
        assert any("text '3' (in a pagination block)" in c["appears_as"] for c in probe["pager"])
        assert not any("text '2'" in a for c in probe["pager"] for a in c["appears_as"])


@needs_chromium
class TestGenericSourceOnSavedPages:
    """GenericSource.cards end to end on saved pages, with the classifier faked."""

    @staticmethod
    async def _cards(name: str, jev: FakeJev):
        source = GenericSource(_captured_url(name), jev)
        result = await _with_fixture(name, source.cards)
        assert result.error == "" and not result.blocked
        return result, source

    @pytest.mark.asyncio
    async def test_empire_flippers_unlock_links_are_read_through_the_listing_links(self):
        jev = FakeJev(group="unlock/", fields={
            "#97447": ("listing_id", 0.99), "Personal Care, Bed & Bath, Home": ("category", 0.98),
        })
        result, source = await self._cards("empireflippers_list", jev)
        assert len(result.listings) == 13
        assert all(re.fullmatch(r"https://empireflippers\.com/listing/\d+/?", l.url)
                   for l in result.listings)
        # No name, heading or titled link on these tiles: niche and number.
        assert result.listings[0].title == "Personal Care, Bed & Bath, Home · #97447"
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == ["empireflippers.com/listing/{*}"]
        assert links["instead_of"] == "app.empireflippers.com/unlock/{*}"

    @pytest.mark.asyncio
    async def test_flippa_watch_buttons_are_read_through_the_listing_links(self):
        result, source = await self._cards("flippa_list", FakeJev(group="watch_item", fields={}))
        assert len(result.listings) == 5
        assert all(re.match(r"https://flippa\.com/\d{8}-", l.url) for l in result.listings)
        assert all(l.title and l.title != "Watch" for l in result.listings)
        assert source.decisions[0]["listing_links"]["patterns"] == ["flippa.com/{id}"]

    @pytest.mark.asyncio
    async def test_businessesforsale_reads_its_listings_not_its_menu(self):
        """The classifier picked the cards' "Contact seller" links (/us/{*}/contact);
        the listings' own links, /us/{*}, are also the site's menu and footer."""
        jev = FakeJev(group="com/us/{*}/contact;", fields={})
        result, source = await self._cards("businessesforsale_list", jev)
        titles = [l.title for l in result.listings]
        assert 16 <= len(titles) <= 19
        assert "Established San Diego Property Management Book Of Business" in titles
        assert "Popular Korean Soft Tofu Restaurant in Rancho Cucamonga" in titles
        assert not set(titles) & {"Sell Your Business", "Login", "FAQs", "Register as a Buyer",
                                  "Email Alerts", "Contact Us", ""}
        assert all(re.fullmatch(r"https://us\.businessesforsale\.com/us/[a-z0-9-]+\.aspx", l.url)
                   for l in result.listings)
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == ["us.businessesforsale.com/us/{*}"]
        assert links["instead_of"] == "us.businessesforsale.com/us/{*}/contact"
        assert links["left_out"] == 12
        # Its only next-page candidate is its pager: the cookie banner's
        # "Show more" is not offered.
        [(state, _)] = jev.of("next")
        assert [link["url"] for link in state["links"].values()] == [
            "https://us.businessesforsale.com/us/search/businesses-for-sale-in-california-2"]
        # The suggestion pins the contact links, read the same way when pasted.
        suggested = source.suggested_override()["listing_links"]
        assert suggested == ["us.businessesforsale.com/us/{*}/contact"]
        pinned = GenericSource(_captured_url("businessesforsale_list"), FakeJev(fields={}),
                               SiteOverride(match="us.businessesforsale.com",
                                            listing_links=suggested))
        again = await _with_fixture("businessesforsale_list", pinned.cards)
        assert [l.title for l in again.listings] == titles

    @pytest.mark.asyncio
    async def test_quietlight_is_one_page_of_listings(self):
        jev = FakeJev(group="quietlight.com/listings/", fields={})
        result, source = await self._cards("quietlight_list", jev)
        assert len(result.listings) == 85
        [(state, _)] = jev.of("next")
        assert [link["appears_as"] for link in state["links"].values()] == [
            ["text 'See more questions & answers'"]]
        assert source.decisions[0]["next_page"]["rule"] == "none"

    @pytest.mark.asyncio
    async def test_sunbelt_is_one_list_with_two_link_shapes(self):
        details = "www.sunbeltnetwork.com/business-search/business-details/{*}"
        offices = "www.sunbeltnetwork.com/{*}/buy-a-business/listings/listing-details/{*}"
        source = GenericSource(_captured_url("sunbelt_list"),
                               FakeJev(group="business-search/business-details", fields={}))

        async def run(page):
            result = await source.cards(page)
            order = await page.evaluate(
                "() => [...document.querySelectorAll('article h4')]"
                ".map((h) => h.closest('a').href)")
            return result, order

        result, order = await _with_fixture("sunbelt_list", run)
        assert [l.url for l in result.listings] == order and len(order) == 10
        assert all(l.title for l in result.listings)
        links = source.decisions[0]["listing_links"]
        assert links["patterns"] == [details, offices] and links["same_list"] == [offices]
        assert source.suggested_override()["listing_links"] == [details, offices]

    @pytest.mark.asyncio
    async def test_fcbb(self):
        jev = FakeJev(group="sfbay.fcbb.com/listing-property/", fields={
            "Total Income": ("cash_flow_sde", 0.94), "Revenue": ("revenue", 1.0),
            "Listing Number": ("listing_id", 1.0), "Category": ("category", 1.0),
            "Pet Grooming & Specialty Retail Business": ("title", 0.99),
            "California": ("location", 0.96), "$550,000": ("asking_price", 0.97),
        }, next={"text '2'": 0.82})
        source = GenericSource(_captured_url("fcbb_list"), jev)

        async def run(page):
            result = await source.cards(page)
            advanced = await source.advance(page, 2)
            return result, advanced

        result, advanced = await _with_fixture("fcbb_list", run)
        assert result.error == "" and not result.blocked
        assert len(result.listings) == 10
        first = result.listings[0]
        assert first.url == ("https://sfbay.fcbb.com/listing-property/"
                             "pet-services-peninsula-pet-grooming-specialty-retail-business-289-25867")
        assert (first.title, first.location) == ("Pet Grooming & Specialty Retail Business",
                                                 "California")
        assert (first.asking_price, first.cashflow, first.revenue) == ("$550,000", "$-140,435",
                                                                       "$176,038")
        assert first.source == "sfbay.fcbb.com" and first.listing_id == ""
        assert "Pet Grooming" in first.excerpt
        assert jev.kinds() == {"group": 1, "fields": 1, "next": 1}
        assert source.decisions[0]["next_page"]["rule"] == "click"
        assert advanced is True


# Every card of the chosen list except those named, removed from the page:
# what a last page with one or two listings on it leaves. Each card is the
# element page 1 marked (data-cbs-card) around the listing's link.
_KEEP_ONLY = r"""(drop) => {
  const gone = new Set(drop.map((u) => u.replace(/\/+$/, '')));
  let removed = 0;
  for (const a of [...document.querySelectorAll('a[href]')]) {
    let u;
    try { u = new URL(a.getAttribute('href'), document.baseURI); } catch (_) { continue; }
    u.hash = '';
    if (!gone.has(u.href.replace(/\/+$/, ''))) continue;
    const card = a.closest('[data-cbs-card]');
    if (card && card.isConnected) { card.remove(); removed++; }
  }
  return removed;
}"""

# What each field's label holds, for every saved page below: enough for the
# money fields and locations to be filled, so page 2 has something to match.
_LABELS = {label: (role, 0.97) for label, role in [
    ("Asking Price", "asking_price"), ("Price", "asking_price"),
    ("Cash Flow", "cash_flow_sde"), ("SDE", "cash_flow_sde"), ("Total Income", "cash_flow_sde"),
    ("Adjusted Earnings", "cash_flow_sde"), ("Income", "cash_flow_sde"),
    ("Net Profit", "cash_flow_sde"), ("Revenue", "revenue"), ("Gross Sales", "revenue"),
    ("Location", "location"),
]}


@needs_chromium
class TestLastPageOnSavedPages:
    """A last page with one or two listings, on real markup.

    One link has no neighbour to stop its card's climb, and fewer than three
    links make no candidate list at all — so a page like that, read afresh,
    has "no list of businesses". Read as page 2 of a sweep, it is page 1's
    list: the same patterns, found by page 1's card shapes, with page 1's
    field roles. Page 1 is the saved page; page 2 is the same page with all
    but the first listing or two taken off it.
    """

    @pytest.fixture(autouse=True)
    def _no_scroll_waits(self, monkeypatch):
        # A saved page loads nothing more when scrolled; three reads per case
        # would otherwise wait out every scroll pause.
        monkeypatch.setattr(generic, "SCROLL_PAUSE_MS", 0)
        monkeypatch.setattr(generic, "SCROLL_BOTTOM_SETTLE_MS", 0)

    @pytest.mark.parametrize("name, group, keep", [
        ("websiteclosers_list", "websiteclosers.com/businesses/", 1),
        ("dealonomy_list", "dealonomy.com/s/", 2),
        ("bizquest_list", "bizquest.com/business-for-sale/", 1),
        ("fcbb_list", "sfbay.fcbb.com/listing-property/", 1),
        ("libertygroup_list", "thelibertygroupofnevada.com/listing/", 1),
        # The listing left is the second link shape of Sunbelt's one list.
        ("sunbelt_list", "business-search/business-details", 1),
        # Read through the detail links inside page 1's "Contact seller" and
        # "Unlock" picks; Empire Flippers' last tile is its one "new" tile.
        ("businessesforsale_list", "com/us/{*}/contact;", 1),
        ("empireflippers_list", "unlock/", 1),
        # The listing is named by its LID query key, which one link cannot vary.
        ("businessteam_list", "business-for-sale.aspx", 1),
    ])
    @pytest.mark.asyncio
    async def test_a_last_page_with_one_or_two_listings_is_read_as_page_one_read_it(
            self, name, group, keep):
        url = _captured_url(name)
        jev = FakeJev(group=group, fields=_LABELS)
        source = GenericSource(url, jev)
        source.begin()

        async def run(page):
            first = await source.cards(page)
            assert first.error == "" and len(first.listings) > keep
            removed = await page.evaluate(
                _KEEP_ONLY, [l.url for l in first.listings[keep:]])
            assert removed >= len(first.listings) - keep

            async def to_the_last_page(target, **_kw):
                pass  # the page is already trimmed in place
            page.goto = to_the_last_page
            # However page 1 said to reach page 2, here it is an address.
            source._next = generic._Next("goto", f"{url}#page-2")
            assert await source.advance(page, 2) is True
            second = await source.cards(page)
            fresh = await GenericSource(url, FakeJev(group=group, fields=_LABELS)).cards(page)
            return first, second, fresh

        first, second, fresh = await _with_fixture(name, run)
        assert second.error == "" and not second.blocked
        # The same listings, read the same way: address, title, money, excerpt.
        assert second.listings == first.listings[:keep]
        assert jev.kinds()["group"] == 1
        page2 = source.decisions[1]
        assert page2["listing_links"]["by"] == "page 1"
        assert {f["by"] for f in page2["fields"]} >= {"page 1"}
        # Read afresh, the same page has no list on it.
        assert fresh.error.startswith("Found no list of businesses for sale")
