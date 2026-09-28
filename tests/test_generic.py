"""The generic listing reader: its decisions, its overrides, and its probe.

Most of this file runs everywhere, CI included: `GenericSource` is driven with
canned probe output (the JSON `JS_PROBE` returns) and a fake classifier that
records every request. That is where the contract lives — which group is read,
what fills a Listing and what is left empty, which cards are dropped, how the
next page is reached, what an error looks like — and, because the classifier
reads its state once and answers every question in it, that each decision is
ONE request per page, not one per field or per link.

The last classes need a Playwright chromium: they run the real `JS_PROBE` on six
listing pages saved on 2026-09-28 (tests/fixtures/generic), with the network
blocked, and pin what the probe finds on markup it has never been tuned to —
the whole point of the source.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import extract
from app.services.typesafe import Choice, Noul, TypeSafeAuthError
from app.sources import generic
from app.sources.generic import JS_PROBE, GenericSource
from app.sources.overrides import MONEY_ROLES, ROLES, Role, SiteOverride, override_for

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
    else is "other" at 0.99. `status` maps a status value to P(gone); `next`
    maps a link's URL, or text in its `appears_as`, to P(next page).
    """

    def __init__(self, *, group: str = "/listing/", group_confidence: float = 0.97,
                 fields=None, status=None, next=None, error: Exception | None = None,
                 error_on: str | None = None):
        self.group = group
        self.group_confidence = group_confidence
        self.fields = {
            "Asking Price": ("asking_price", 0.97),
            "Cash Flow": ("cash_flow_sde", 0.95),
            "Business 1": ("title", 0.96),
            "Austin, TX": ("location", 0.92),
        } if fields is None else fields
        self.status = status or {}
        self.next = {NEXT_URL: 0.97} if next is None else next
        self.error = error
        self.error_on = error_on
        self.requests: list[tuple[str, dict, dict]] = []

    @staticmethod
    def kind(questions: dict) -> str:
        first = next(iter(questions))
        if first == "listing_group":
            return "group"
        return {"field": "fields", "status": "status", "link": "next"}[first.split("_")[0]]

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
            first = field["values_on_three_cards"][0]
            role, conf = self.fields.get(label) or self.fields.get(first) or ("other", 0.99)
            out[name] = Choice(role, {role: conf}, conf, "typesafe/jev-test")
        return out

    def _status(self, state, questions):
        return {name: Noul(self.status.get(state["statuses"][name], 0.02), "typesafe/jev-test")
                for name in questions}

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
        return 0 if self.selector in self.page.missing else 1

    @property
    def first(self) -> "FakeLocator":
        return self

    async def click(self, **_kw) -> None:
        self.page.clicks.append(self.selector)


class FakePage:
    """Answers JS_PROBE with the next canned probe (the last one repeats)."""

    def __init__(self, *probes: dict, url: str = LIST_URL):
        self.probes = list(probes)
        self.url = url
        self.probe_args: list[dict] = []
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.missing: set[str] = set()

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
    async def test_none_on_a_later_page_is_the_end_of_the_list(self):
        jev = FakeJev()
        source = GenericSource(LIST_URL, jev)
        page = FakePage(_probe(pager=[_pager(1, NEXT_URL, "rel=next")]), _probe())
        first = await source.cards(page)
        assert len(first.listings) == 4
        assert await source.advance(page, 2) is True
        jev.group = "none"
        second = await source.cards(page)
        assert second.listings == [] and second.error == "" and not second.blocked
        assert source.decisions[1]["page"] == 2
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
            "values_on_three_cards": ["$1,100,000", "$1,200,000", "$1,300,000"],
        }
        slot = next(f for f in state["fields"].values() if f["values_on_three_cards"][0] == "Austin, TX")
        assert slot["text_just_before_this_field"] is None
        assert slot["values_on_three_cards"] == ["Austin, TX"]
        assert set(questions) == set(state["fields"]) and len(questions) == 4
        assert questions["field_3"]["instructions"] == (
            "Each listing card on this page has the fields in the state. What does field_3 hold?"
        )
        listing = result.listings[0]
        assert (listing.title, listing.location) == ("Business 1", "Austin, TX")
        assert (listing.asking_price, listing.cashflow) == ("$1,100,000", "$150,000")

    @pytest.mark.asyncio
    async def test_money_looking_fields_only_choose_among_money_roles(self):
        _, _, jev, _ = await _read(_probe())
        [(state, questions)] = jev.of("fields")
        for name, field in state["fields"].items():
            criteria = questions[name]["criteria"]
            if field["values_on_three_cards"][0].startswith("$"):
                assert set(criteria) == {*MONEY_ROLES, "other"}
            else:
                assert criteria == ROLES
            assert all(criteria[k] == ROLES[k] for k in criteria)

    @pytest.mark.asyncio
    async def test_a_field_on_fewer_than_30_percent_of_cards_is_not_asked_about(self):
        cards = [_card(n) for n in range(1, 11)]
        cards[0]["labeled"]["EBITDA"] = "$90,000"
        cards[1]["labeled"]["EBITDA"] = "$95,000"
        cards[2]["labeled"]["EBITDA"] = "$99,000"
        _, _, jev, _ = await _read(_probe([_group(cards=cards)]))
        asked = [f["text_just_before_this_field"] for f in jev.of("fields")[0][0]["fields"].values()]
        assert "EBITDA" in asked
        cards[2]["labeled"].pop("EBITDA")
        _, _, jev, _ = await _read(_probe([_group(cards=cards)]))
        asked = [f["text_just_before_this_field"] for f in jev.of("fields")[0][0]["fields"].values()]
        assert "EBITDA" not in asked

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


# ── unavailable listings ─────────────────────────────────────────────────────


def _with_status(*statuses: str) -> dict:
    cards = [_card(n) for n in range(1, len(statuses) + 1)]
    for card, status in zip(cards, statuses):
        card["slots"]["div.card>span.badge#0"] = status
    return _probe([_group(cards=cards)])


STATUS_FIELDS = {"Asking Price": ("asking_price", 0.97), "Business 1": ("title", 0.96),
                 "Active": ("status", 0.95)}


class TestUnavailable:
    @pytest.mark.asyncio
    async def test_gone_listings_are_dropped_but_still_seen(self):
        jev = FakeJev(fields=STATUS_FIELDS, status={"Sold": 0.94, "Under Contract": 0.9})
        result, source, _, _ = await _read(_with_status("Active", "Sold", "Active",
                                                        "Under Contract"), jev)
        assert [l.url for l in result.listings] == [f"{SITE}/listing/biz-1",
                                                    f"{SITE}/listing/biz-3"]
        assert result.seen_urls == [f"{SITE}/listing/biz-{n}" for n in range(1, 5)]
        record = source.decisions[0]
        assert (record["cards"], record["kept"], record["dropped_unavailable"]) == (4, 2, 2)
        assert record["status"] == {
            "by": "jev", "unavailable": ["Sold", "Under Contract"],
            "values": {"Active": 0.02, "Sold": 0.94, "Under Contract": 0.9},
        }

    @pytest.mark.asyncio
    async def test_every_distinct_status_is_one_yes_no_in_one_request(self):
        jev = FakeJev(fields=STATUS_FIELDS)
        await _read(_with_status("Active", "Sold", "Active", "Pending"), jev)
        [(state, questions)] = jev.of("status")
        assert state == {"statuses": {"status_1": "Active", "status_2": "Sold",
                                      "status_3": "Pending"}}
        assert questions["status_2"] == {
            "type": "noul",
            "instructions": ("status_2 in the state means the business is no longer available: "
                             "sold, pending, or under contract"),
        }

    @pytest.mark.asyncio
    async def test_no_status_field_means_no_status_request(self):
        _, source, jev, _ = await _read(_probe())
        assert "status" not in jev.kinds()
        assert "status" not in source.decisions[0]

    @pytest.mark.asyncio
    async def test_override_drop_status_matches_substrings_without_asking(self):
        override = SiteOverride(match="brokers.example", drop_status=["sold", "contract"])
        jev = FakeJev(fields=STATUS_FIELDS)
        result, source, _, _ = await _read(
            _with_status("Active", "SOLD!", "Under Contract", "Pending"), jev, override)
        assert "status" not in jev.kinds()
        assert [l.url.rsplit("-", 1)[1] for l in result.listings] == ["1", "4"]
        assert source.decisions[0]["status"]["by"] == "override"

    @pytest.mark.asyncio
    async def test_an_empty_drop_status_drops_nothing(self):
        override = SiteOverride(match="brokers.example", drop_status=[])
        jev = FakeJev(fields=STATUS_FIELDS, status={"Sold": 0.99})
        result, _, _, _ = await _read(_with_status("Active", "Sold", "Active"), jev, override)
        assert len(result.listings) == 3
        assert "status" not in jev.kinds()


# ── next page ────────────────────────────────────────────────────────────────


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
        assert questions["link_1"] == {
            "type": "noul",
            "instructions": ("link_1 in the state goes to the next page (page 2) of the same "
                             "list as the current page."),
        }
        assert source.decisions[0]["next_page"] == {"by": "jev", "rule": NEXT_URL,
                                                    "probability": 0.97, "candidates": 2}
        assert await source.advance(page, 2) is True
        assert page.gotos == [NEXT_URL] and page.clicks == []
        # Page 2 asks for page 3.
        await source.cards(page)
        assert page.probe_args[1]["next_number"] == 3
        assert jev.of("next")[1][1]["link_1"]["instructions"].endswith(
            "next page (page 3) of the same list as the current page.")

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
    async def test_a_vanished_control_ends_paging(self):
        source = GenericSource(LIST_URL, FakeJev(next={"text 'Next'": 0.9}))
        page = FakePage(_probe(pager=[_pager(1, None, "text 'Next'")]))
        await source.cards(page)
        page.missing.add('[data-cbs-next="n1"]')
        assert await source.advance(page, 2) is False

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
    async def test_each_decision_is_one_request_per_page(self):
        jev = FakeJev(fields=STATUS_FIELDS, status={"Sold": 0.9})
        probe = _with_status("Active", "Sold", "Active", "Active")
        probe["pager"] = [_pager(1, NEXT_URL, "rel=next"), _pager(2, None, "text 'Load more'")]
        source = GenericSource(LIST_URL, jev)
        page = FakePage(probe)
        await source.cards(page)
        assert jev.kinds() == {"group": 1, "fields": 1, "status": 1, "next": 1}
        await source.advance(page, 2)
        await source.cards(page)
        assert jev.kinds() == {"group": 2, "fields": 2, "status": 2, "next": 2}


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
    async def test_no_classifier_is_a_plain_error(self):
        source = GenericSource(LIST_URL, None)
        result = await source.cards(FakePage(_probe()))
        assert result.retry is False
        assert "needs the TypeSafe Classifier (e.g. Jev)" in result.error

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
        jev = FakeJev(fields={**STATUS_FIELDS, "Cash Flow": ("cash_flow_sde", 0.5)},
                      status={"Sold": 0.9})
        probe = _with_status("Active", "Sold", "Active")
        probe["pager"] = [_pager(1, NEXT_URL, "rel=next")]
        _, source, _, _ = await _read(probe, jev)
        suggested = source.suggested_override()
        assert suggested == {
            "match": "brokers.example",
            "listing_links": [PATTERN],
            "fields": {"Asking Price": "asking_price", "Cash Flow": "ignore",
                       "div.card>h3#0": "title", "div.card>p.loc#0": "other",
                       "div.card>span.badge#0": "status"},
            "next_page": f"{LIST_URL}page/{{page}}/",
            "drop_status": ["Sold"],
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
    """What JS_PROBE finds on six real listing pages it was never tuned to."""

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
    async def test_a_query_string_identity_is_reported_as_varying(self):
        group = _group_of(await _probe_of("businessteam_list"),
                          "www.business-team.com/buy-a-business/business-for-sale.aspx?From&LID")
        assert group["varying_keys"] == ["LID"]
        assert group["paths_unique"] is False

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
class TestGenericSourceOnASavedPage:
    """GenericSource.cards end to end on FCBB, with the classifier faked."""

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
