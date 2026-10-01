"""Is each card a business for sale now, and does the page read as listings at all?

A source adapter can be wrong without failing. A site redesign moves the title
out of the element the adapter reads; a generic reader picks the wrong list of
links; a consent screen happens to contain a few links that look like cards.
Each of those still returns *something*, and filed into a database it is worse
than nothing: garbage rows look like listings until someone reads them, and
dedupe then treats them as already seen. So every page that returned cards is
checked before anything from it is kept, in two layers.

* **Code checks** (`check`), always. A card with no title or no link cannot be
  filed, so it is dropped; but when most cards on a page lack one, the adapter
  is reading the wrong thing, and the page fails. And when the asking prices
  that contain a digit mostly do not read as an amount, the "price" is some
  other text. Only the asking price is judged: revenue and cash flow are
  legitimately ranges on some sites ("$250K - $500K").
* **One request per listing** (`ListingCheck`), when the TypeSafe Classifier
  (e.g. Jev) has a key. Once a page is down to its array of listing elements,
  every question about one element is asked together, in ONE request whose
  state is that card (`triage.card_state`): is it one listing of a business
  that is currently for sale (not sold, pending or under contract, and not a
  menu link, an ad or other page furniture) — and, when the sweep triages,
  REVIEW or REJECT with the caller's criteria. One card per request because
  that is what the triage answers were measured on (bundling several cards
  lost agreement, see `triage.py`); adding the eligibility question to it
  changed none of 219 triage answers. Requests go out the sweep's own
  `classifier_parallel` at a time (`TYPESAFE_PARALLEL`, five, unless the call
  names another), each sweep behind its own gate, and all of them under the
  client's one ceiling for the process (`TYPESAFE_MAX_PARALLEL`).

Only elements the store does not have are asked about — plus, in a triaging
sweep, stored rows whose Bot Triage is still blank (the backlog triage heals).
A stored row that already has a decision costs nothing, and with sync=false
every element is new. Without a key nothing is asked at all: a BizBuySell sweep
then makes no classifier call, exactly as before the classifier existed.

What the answers are allowed to do depends on who chose the cards (`drop`):

* **The generic reader chose them itself** (it picked a group of links on a
  page it had never seen), so a card judged not eligible is dropped: two menu
  links at the ends of a list, or the "– Sold" tiles an infinite scroll runs
  into, are cards to leave out, not a reason to throw away the listings
  between them. There is no separate "sold" handling: this is it.
* **A site adapter's cards** (BizBuySell) are read by code written for that
  page, so the answers are a verdict on the PAGE only, and every card is kept.
  One misjudged listing is not the classifier's to silently remove from a page
  the adapter read correctly.

The page check comes from the same answers: a page where fewer than half of
its cards pass fails — the source is reading the wrong thing. A card the store
already has counts as passing (it was filed as a listing before, and asking
about it again would cost a request per known row per sweep), so a page whose
cards are all known asks nothing and passes. A page with fewer than
`MIN_JUDGED` cards to go on is never failed by it: one sold listing left on a
broker's profile is not a page read wrong. Nor is a later page of a list the
generic reader chose on its first page: it is read with that page's card
shape, so its cards are that list's, and a run of sold or under-contract ones
(a site that lists its closed deals after its open ones) is left out card by
card without taking the cards for sale between them along.

The classifier half is best-effort. A classifier that cannot answer at all (no
key, a rejected key, no credits, no answer after the client's retries) stops
this sweep's requests — every later one would fail the same way — and the
cards not yet asked about are kept as the code checks left them; triage leaves
their rows blank for a later sweep. Any other classifier error is that one
card's: it is kept, and its row fails triage. Neither ever fails a page, so a
BizBuySell sweep keeps working while the classifier is down.

A page with zero cards is never judged — that is an empty last page, and what
it means is the sweep's business, exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Listing
from ..stores.money import parse_money
from .triage import STAGE_CARD, TriageDecision, Triager, card_state
from .typesafe import (
    TYPESAFE_PARALLEL,
    Choice,
    Noul,
    TypeSafeAuthError,
    TypeSafeCreditError,
    TypeSafeError,
    TypeSafeNotConfigured,
    TypeSafeUnavailable,
)

logger = logging.getLogger("cloakbiz.legibility")

# The statement put to the classifier about each listing element, with the
# card as the state. This exact wording was validated live on 2026-09-28: 20 of
# 20 Synergy "– Sold" titles scored ≤ 0.08, 218 of 219 real listings ≥ 0.5
# (median 0.80, one at 0.48), menu links ("Sell Your Business", "Login") ≤ 0.48.
ELIGIBLE_QUESTION = (
    "The state is one listing of a business that is currently for sale — not a sold, pending "
    "or under-contract listing, and not a menu link, an advertisement or some other page "
    "element."
)

# At least this share of cards must have both a title and a link.
MIN_COMPLETE = 0.7
# At least this share of digit-bearing asking prices must read as an amount.
MIN_PRICES_READ = 0.5
# A card the classifier gives less than this is not a listing for sale now.
MIN_ELIGIBLE = 0.5
# The page fails when fewer than this share of its judged cards pass...
MIN_PASSING = 0.5
# ...and it has at least this many judged cards to go on.
MIN_JUDGED = 3
# How much of an unreadable price to quote in the failure message.
_QUOTE_CHARS = 40
# Cards judged not eligible, named in a page's record for someone checking the call.
_SHOWN = 10
_TITLE_CHARS = 80

# Classifier failures that stop a sweep's requests: every later one would fail
# the same way. Any other TypeSafeError is that one card's.
STOPS = (TypeSafeAuthError, TypeSafeCreditError, TypeSafeUnavailable, TypeSafeNotConfigured)

_DIGIT = re.compile(r"\d")
# A currency before an amount: a code ("USD", "CAD"), a sign ("$", "€", "£"),
# or both ("US$", "C$", "USD $").
_CURRENCY = re.compile(
    r"^\s*(?:(?:USD|US|CAD|AUD|NZD|EUR|GBP)\s*)?(?:US|CA|C|AU|A|NZ)?[$€£]?\s*(?=\d)"
)
# What a price can carry after the amount that says nothing against it being
# one: a currency code ("$650,000 USD") and a note in brackets ("(Firm)",
# "(negotiable)").
_TRAILING_CODE = re.compile(r"\s*(?:USD|US\$|CAD|AUD|NZD|EUR|GBP)\.?\s*$", re.IGNORECASE)
_TRAILING_NOTE = re.compile(r"\s*\([^()]*\)\s*$")
# A multiplier spelled out ("$1.2 Million", "$850 Thousand", "$3.4 Mil"), as
# the letter `parse_money` reads.
_WORD_MULTIPLIERS = (
    (re.compile(r"\s*\b(?:billions?|bn)\b\.?", re.IGNORECASE), "B"),
    (re.compile(r"\s*\b(?:millions?|mill?)\b\.?", re.IGNORECASE), "M"),
    (re.compile(r"\s*\b(?:thousands?)\b", re.IGNORECASE), "K"),
)


# ── the code checks ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """What the code checks decided about one page.

    `listings` are the cards with a title and a link — the only ones the sweep
    goes on with. `reason` is set when `ok` is False and is written for the
    person reading the job's error: what was wrong, on which page, and that
    nothing from it was kept.
    """

    listings: list[Listing]
    ok: bool = True
    reason: str = ""
    # Cards dropped for having no title or no link.
    dropped: int = 0

    def record(self, page: int) -> dict[str, Any]:
        """A small, JSON-safe summary for the run's evidence."""
        out: dict[str, Any] = {"page": page, "ok": self.ok, "kept": len(self.listings),
                               "dropped": self.dropped}
        if self.reason:
            out["reason"] = self.reason
        return out


def check(listings: list[Listing], *, page: int) -> Verdict:
    """The code checks on one page's cards: no title or link, prices that aren't."""
    if not listings:
        return Verdict(listings=[])

    total = len(listings)
    complete = [c for c in listings if c.title.strip() and c.url.strip()]
    dropped = total - len(complete)
    if len(complete) / total < MIN_COMPLETE:
        return Verdict(
            listings=[], ok=False, dropped=dropped,
            reason=(
                f"Only {len(complete)} of {total} cards on page {page} have both a title "
                f"and a link (at least {MIN_COMPLETE:.0%} should), so they don't read as "
                f"business listings — nothing from this page was kept."
            ),
        )

    prices = [c.asking_price.strip() for c in complete if _DIGIT.search(c.asking_price)]
    unread = [p for p in prices if not _reads_as_amount(p)]
    if prices and (len(prices) - len(unread)) / len(prices) < MIN_PRICES_READ:
        example = unread[0]
        if len(example) > _QUOTE_CHARS:
            example = example[: _QUOTE_CHARS - 1].rstrip() + "…"
        return Verdict(
            listings=[], ok=False, dropped=dropped,
            reason=(
                f"Only {len(prices) - len(unread)} of {len(prices)} asking prices on page "
                f"{page} read as an amount (e.g. {example!r}), so the cards don't read as "
                f"business listings — nothing from this page was kept."
            ),
        )
    return Verdict(listings=complete, dropped=dropped)


def _reads_as_amount(value: str) -> bool:
    """Whether an asking price is an amount, possibly with a trailing qualifier.

    `parse_money` refuses "$81,000 + Inventory", and rightly — as a number to
    *store* it would understate the price. But the question here is only
    whether the field holds a price at all, and a qualified price is one: a
    one-listing broker page quoting "+ Inventory" is a perfectly good page. So
    is a price in another currency, or with its currency spelled out before or
    after it ("USD $650,000" on Flippa, "CAD $450,000", "€300,000", "$650,000
    USD"), with its multiplier spelled out ("$1.2 Million", "$850 Thousand"),
    or with a note in brackets ("$1,250,000 (Firm)"): each is read past here,
    where `parse_money`, which stores the number, stays strict.
    """
    amount = _CURRENCY.sub("", value, count=1)
    return any(parse_money(_bare(text)) is not None
               for text in (amount, amount.split("+", 1)[0]))


def _bare(text: str) -> str:
    """An amount without what `_reads_as_amount` reads past after it."""
    text = text.strip()
    while True:
        shorter = _TRAILING_CODE.sub("", _TRAILING_NOTE.sub("", text)).strip()
        if shorter == text:
            break
        text = shorter
    for pattern, letter in _WORD_MULTIPLIERS:
        text = pattern.sub(letter, text)
    return text.strip()


# ── one request per listing ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ListingAnswer:
    """Everything one request said about one listing element.

    `eligible` is P(a business currently for sale); `triage` the card-stage
    decision when the sweep triages. `error` is set, and the rest empty, when
    this one request failed without the classifier being down (a request it
    refused, an answer that can't be read).
    """

    eligible: float | None = None
    triage: TriageDecision | None = None
    error: str = ""


async def ask(classifier, listing: Listing, triager: Triager | None = None) -> ListingAnswer:
    """ONE request about one listing: eligible, and triage when `triager` is given.

    The state is the card (`triage.card_state`: title, location, money, the
    excerpt and the computed price/earnings multiple). Raises the client's
    TypeSafeError; an answer of the wrong kind is one too.
    """
    questions: dict[str, dict[str, Any]] = {
        "eligible": {"type": "noul", "instructions": ELIGIBLE_QUESTION},
    }
    if triager is not None:
        questions["triage"] = triager.question()
    replies = await classifier.ask(card_state(listing), questions)
    eligible = replies.get("eligible") if isinstance(replies, dict) else None
    if not isinstance(eligible, Noul):
        raise TypeSafeError("The TypeSafe Classifier did not answer whether this is a listing "
                            "of a business for sale.")
    decision = None
    if triager is not None:
        answer = replies.get("triage")
        if not isinstance(answer, Choice):
            raise TypeSafeError("The TypeSafe Classifier did not answer the triage question.")
        decision = triager.decision(answer, STAGE_CARD)
    return ListingAnswer(eligible=eligible.probability, triage=decision)


def listing_key(listing: Listing) -> str:
    """The identity a sweep dedupes on (listing id, else normalized URL, else URL)."""
    return listing.listing_id or listing.normalized_url or listing.url


@dataclass(frozen=True)
class PageCheck:
    """What one page's answers decided: the cards it keeps, or why it fails."""

    listings: list[Listing]
    ok: bool = True
    reason: str = ""
    record: dict[str, Any] = field(default_factory=dict)


class ListingCheck:
    """One sweep's per-listing requests, shared by every URL and page of it.

    `known` is the store's index read at the start of a synced sweep (None
    with sync=false, or when it could not be read: then every element is new),
    or the task reading it — started with the sweep, so the read overlaps the
    browser's start-up, and waited for by the first page that needs it.
    `triager` adds the triage question to each request. Answers are kept by
    listing identity for the whole sweep, so a listing seen on two pages or
    under two URLs — or a page retried from a new exit IP — is asked once, and
    the triage phase reads each row's card decision from here (`answer`).
    `parallel` is how many requests it has in flight at once — the sweep's
    `classifier_parallel`. The gate is this check's own, so two sweeps running
    together each get their full limit; the client's ceiling bounds the sum.
    """

    def __init__(self, classifier, *, triager: Triager | None = None, known=None,
                 parallel: int = TYPESAFE_PARALLEL) -> None:
        self._classifier = classifier
        self.triager = triager
        self._known = known
        self.parallel = max(1, int(parallel))
        self._gate = asyncio.Semaphore(self.parallel)
        self._answers: dict[str, ListingAnswer] = {}
        # One lock per listing, so two pages showing it at once (two URLs of
        # one sweep) wait for one request rather than making two.
        self._asking: dict[str, asyncio.Lock] = {}
        # Why the classifier stopped being asked, once it could not answer at all.
        self.stopped: str | None = None
        self.requests = 0

    def close(self) -> None:
        """Stop reading the index, if the sweep ends before any page needed it."""
        if isinstance(self._known, asyncio.Future) and not self._known.done():
            self._known.cancel()

    def to_ask(self, listing: Listing) -> bool:
        """New to the store, or (when triaging) stored with a blank Bot Triage."""
        if self._known is None or not self._known.contains(listing):
            return True
        return self.triager is not None and self._known.decision(listing) == ""

    def answer(self, listing: Listing) -> ListingAnswer | None:
        """What this sweep's request said about `listing`, if it was asked."""
        return self._answers.get(listing_key(listing))

    async def ask_one(self, listing: Listing) -> ListingAnswer | None:
        """`listing`'s answer, asked now unless it already was; None once stopped.

        Gated `parallel` at a time, and the stop is looked at inside
        the gate: every card of a page is started at once, and one that only
        looked before waiting would still ask after an outage it had queued
        behind.
        """
        key = listing_key(listing)
        if key in self._answers:
            return self._answers[key]
        async with self._asking.setdefault(key, asyncio.Lock()), self._gate:
            if key in self._answers:
                return self._answers[key]
            if self.stopped:
                return None
            self.requests += 1
            try:
                answer = await ask(self._classifier, listing, self.triager)
            except STOPS as exc:
                if self.stopped is None:
                    logger.warning("listing check: the classifier stopped answering: %s", exc)
                    self.stopped = str(exc)
                return None
            except TypeSafeError as exc:
                logger.warning("listing check: no answer for %s: %s", listing.url, exc)
                answer = ListingAnswer(error=str(exc))
            self._answers[key] = answer
        return answer

    async def page(self, listings: list[Listing], *, page: int, drop: bool) -> PageCheck:
        """Ask about this page's new cards, then judge the page from the answers.

        `drop` lets a card judged not eligible be left out — only for a source
        that chose the cards itself (see the module docstring).
        """
        if isinstance(self._known, asyncio.Future):
            self._known = await self._known
        asked = [c for c in listings if self.to_ask(c)]
        known = len(listings) - len(asked)
        answers = await asyncio.gather(*(self.ask_one(c) for c in asked))
        judged = [(c, a.eligible) for c, a in zip(asked, answers)
                  if a is not None and a.eligible is not None]
        low = [(c, p) for c, p in judged if p < MIN_ELIGIBLE]
        passing = len(judged) - len(low) + known
        total = len(judged) + known
        record: dict[str, Any] = {"asked": len(asked), "answered": len(judged), "known": known,
                                  "eligible": len(judged) - len(low), "not_eligible": len(low),
                                  "dropped": 0}
        if low:
            record["not_eligible_listings"] = [
                {"title": c.title.strip()[:_TITLE_CHARS], "p": round(p, 3)} for c, p in low[:_SHOWN]]
        errors = sum(1 for a in answers if a is not None and a.error)
        if errors:
            record["errors"] = errors
        if any(a is None for a in answers):
            record["unanswered"] = sum(1 for a in answers if a is None)
            record["stopped"] = self.stopped
        # A later page of a list the reader chose is read with the first
        # page's card shape, so its cards are that list's, and one where most
        # are sold or under contract is a page of listings no longer for sale,
        # not a wrong list (Dealonomy's page 2: 31 Sold, 3 Under Contract). Its
        # cards for sale are kept; failing it would drop them with the rest.
        judges_page = not (drop and page > 1)
        if judges_page and total >= MIN_JUDGED and passing < MIN_PASSING * total:
            return PageCheck(listings=[], ok=False, record=record, reason=(
                f"Only {passing} of {total} cards on page {page} read as business listings "
                f"currently for sale (at least half should) — nothing from this page was kept."))
        if not drop or not low:
            if low:
                logger.info("listing check: page %d: %d of %d cards judged not for sale, all "
                            "kept (the adapter chose them)", page, len(low), len(judged))
            return PageCheck(listings=list(listings), record=record)
        gone = {id(c) for c, _ in low}
        record["dropped"] = len(low)
        logger.info("listing check: page %d: %d of %d cards left out as not currently for sale",
                    page, len(low), len(judged))
        return PageCheck(listings=[c for c in listings if id(c) not in gone], record=record)
