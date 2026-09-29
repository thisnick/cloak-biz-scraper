"""Does this page of cards read as business listings? Asked before anything is kept.

A source adapter can be wrong without failing. A site redesign moves the title
out of the element the adapter reads; a generic reader picks the wrong list of
links; a consent screen happens to contain a few links that look like cards.
Each of those still returns *something*, and filed into a database it is worse
than nothing: garbage rows look like listings until someone reads them, and
dedupe then treats them as already seen. So every page that returned cards is
asked, cheaply, whether they look like listings — and a page that does not is
a loud, per-source failure with evidence, not a quiet contribution to the
results.

Two layers, both conservative:

* **Code checks**, always. A card with no title or no link cannot be filed, so
  it is dropped; but when most cards on a page lack one, the adapter is reading
  the wrong thing, and the page fails. And when the asking prices that contain
  a digit mostly do not read as an amount, the "price" is some other text.
  Only the asking price is judged: revenue and cash flow are legitimately
  ranges on some sites ("$250K - $500K"), which is not a sign of anything.
* **The TypeSafe Classifier (e.g. Jev)**, when a key is saved: a yes/no on
  every card that passed the code checks. It catches what the code cannot see —
  well-formed cards that are blog posts, franchise ads, or a site's own
  navigation. A page where fewer than half pass fails. The cards go in ONE
  request — every card in one state, one question per card — because the
  classifier reads the state once and answers every question in it; a request
  per card would pay for that reading again and again.

What the classifier's per-card answers are allowed to do depends on who chose
the cards (`drop_cards`):

* **The generic reader chose them itself** (it picked a group of links on a
  page it had never seen), so a card the classifier judges not to be a listing
  is dropped: two menu links at the ends of a list are two cards to leave out,
  not a reason to throw away the seventeen listings between them
  (BusinessesForSale, second live gate, where a sample of three failed the
  page).
* **A site adapter's cards** (BizBuySell) are read by code written for that
  page, so the classifier's answer is a verdict on the PAGE only: it fails
  when fewer than half pass — the adapter is reading the wrong thing — and
  otherwise every card is kept. One misjudged listing is not the classifier's
  to silently remove from a page the adapter read correctly.

The classifier half is best-effort here. BizBuySell pages do not depend on it,
so a classifier outage must not fail them: the error is logged and recorded on
the verdict, and the code checks still decide. (A source that does depend on
the classifier fails on its own calls long before this one.)

A page with zero cards is never judged — that is an empty last page, and what
it means is the sweep's business, exactly as before.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from ..models import Listing
from ..stores.money import parse_money
from .typesafe import Noul, TypeSafeError

logger = logging.getLogger("cloakbiz.legibility")

# The statement put to the classifier about each card, by its key in the state
# ("card_1", "card_2", …).
QUESTION = "{card} in the state is a listing of a business for sale."

# At least this share of cards must have both a title and a link.
MIN_COMPLETE = 0.7
# At least this share of digit-bearing asking prices must read as an amount.
MIN_PRICES_READ = 0.5
# A card the classifier gives less than this is not a listing (and is dropped,
# when the check may drop cards — see `drop_cards`).
MIN_CARD = 0.5
# The page fails when fewer than this share of the cards asked about pass.
MIN_PASSING = 0.5
# Cards asked about per page, all in one request. A longer page has its first
# and last MAX_CARDS / 2 asked about — cards that are not listings come from
# the menu above the list and the footer below it — and the ones between kept
# on the strength of the code checks.
MAX_CARDS = 40
# Enough of a card's excerpt to recognise it: forty of them share one request.
_EXCERPT_CHARS = 300
# How much of an unreadable price to quote in the failure message.
_QUOTE_CHARS = 40
# Dropped cards named in the record, for someone checking the classifier's call.
_REJECTS_SHOWN = 10
_REJECT_TITLE_CHARS = 80

_DIGIT = re.compile(r"\d")
# A currency before an amount: a code ("USD", "CAD"), a sign ("$", "€", "£"),
# or both ("US$", "C$", "USD $").
_CURRENCY = re.compile(
    r"^\s*(?:(?:USD|US|CAD|AUD|NZD|EUR|GBP)\s*)?(?:US|CA|C|AU|A|NZ)?[$€£]?\s*(?=\d)"
)


@dataclass(frozen=True)
class Verdict:
    """What the check decided about one page.

    `listings` are the cards that survive — those with a title and a link, less
    (when the check was allowed to drop cards) any the classifier judged not to
    be listings — and the only ones the sweep keeps. `reason` is set when `ok` is False and is written for the person
    reading the job's error: what was wrong, on which page, and that nothing
    from it was kept.
    """

    listings: list[Listing]
    ok: bool = True
    reason: str = ""
    # Cards the code checks dropped (no title or no link).
    dropped: int = 0
    # Cards the classifier was asked about, how many of those it judged not to
    # be listings, and how many of those were dropped for it (all of them when
    # the check may drop cards or the page failed; none on an adapter's page
    # that passed). 0 when it was not asked (no key) or could not answer.
    classifier_asked: int = 0
    classifier_low: int = 0
    classifier_dropped: int = 0
    # The classifier's mean over the cards it was asked about; None when it
    # was not asked or could not answer.
    classifier_mean: float | None = None
    # The cards it judged not to be listings, as (title, probability), page order.
    classifier_rejected: tuple[tuple[str, float], ...] = ()
    # Why the classifier half was skipped, when it was asked and failed.
    classifier_error: str = ""

    def record(self, page: int) -> dict[str, Any]:
        """A small, JSON-safe summary for the run's evidence."""
        out: dict[str, Any] = {"page": page, "ok": self.ok, "kept": len(self.listings),
                               "dropped": self.dropped}
        if self.reason:
            out["reason"] = self.reason
        if self.classifier_asked:
            out["classifier_asked"] = self.classifier_asked
            out["classifier_low"] = self.classifier_low
            out["classifier_dropped"] = self.classifier_dropped
            if self.classifier_mean is not None:
                out["classifier_mean"] = round(self.classifier_mean, 3)
            if self.classifier_rejected:
                out["classifier_rejected"] = [
                    {"title": title[:_REJECT_TITLE_CHARS], "p": round(p, 3)}
                    for title, p in self.classifier_rejected[:_REJECTS_SHOWN]
                ]
        if self.classifier_error:
            out["classifier_error"] = self.classifier_error
        return out


async def check(listings: list[Listing], *, page: int, classifier=None,
                drop_cards: bool = False) -> Verdict:
    """Judge one page's cards. Never raises for a classifier failure.

    `classifier` is a TypeSafe client (anything with `ask(state, questions)`),
    passed only when a key is saved; None skips that half. `drop_cards` lets
    the classifier's answers remove single cards from a page that passes — only
    for a source that chose the cards itself (the generic reader). Left False,
    the answers judge the page and nothing else (see the module docstring).
    """
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

    if classifier is None:
        return Verdict(listings=complete, dropped=dropped)

    asked = _asked(len(complete))
    names = [f"card_{i}" for i in range(1, len(asked) + 1)]
    state = {"cards": {name: _state(complete[i]) for name, i in zip(names, asked)}}
    questions = {name: {"type": "noul", "instructions": QUESTION.format(card=name)}
                 for name in names}
    try:
        replies = await classifier.ask(state, questions)
        answers = []
        for name in names:
            reply = replies.get(name)
            if not isinstance(reply, Noul):
                raise TypeSafeError(
                    f"The TypeSafe Classifier did not answer the yes/no question about {name}."
                )
            answers.append(reply.probability)
    except TypeSafeError as exc:
        # Best-effort: the code checks above have already passed this page.
        logger.warning("legibility: classifier skipped on page %d: %s", page, exc)
        return Verdict(listings=complete, dropped=dropped, classifier_error=str(exc))

    low = {i: p for i, p in zip(asked, answers) if p < MIN_CARD}
    passing = len(asked) - len(low)
    judged = {
        "classifier_asked": len(asked),
        "classifier_low": len(low),
        "classifier_mean": sum(answers) / len(answers),
        "classifier_rejected": tuple((complete[i].title.strip(), p) for i, p in low.items()),
    }
    if passing < MIN_PASSING * len(asked):
        return Verdict(
            listings=[], ok=False, dropped=dropped, classifier_dropped=len(low), **judged,
            reason=(
                f"Only {passing} of {len(asked)} cards on page {page} read as business "
                f"listings (at least half should) — nothing from this page was kept."
            ),
        )
    if not drop_cards:
        # An adapter's page that passed: every card it read is kept.
        if low:
            logger.info("legibility: page %d: %d of %d cards judged not listings, all kept "
                        "(the adapter chose them)", page, len(low), len(asked))
        return Verdict(listings=complete, dropped=dropped, **judged)
    if low:
        logger.info("legibility: page %d: %d of %d cards dropped as not listings",
                    page, len(low), len(asked))
    kept = [c for i, c in enumerate(complete) if i not in low]
    return Verdict(listings=kept, dropped=dropped, classifier_dropped=len(low), **judged)


def _reads_as_amount(value: str) -> bool:
    """Whether an asking price is an amount, possibly with a trailing qualifier.

    `parse_money` refuses "$81,000 + Inventory", and rightly — as a number to
    *store* it would understate the price. But the question here is only
    whether the field holds a price at all, and a qualified price is one: a
    one-listing broker page quoting "+ Inventory" is a perfectly good page. So
    is a price in another currency, or with its currency spelled out before it
    ("USD $650,000" on Flippa, "CAD $450,000", "€300,000"): the currency is
    read past here, where `parse_money`, which stores the number, keeps to US
    dollars.
    """
    amount = _CURRENCY.sub("", value, count=1)
    return parse_money(amount) is not None or parse_money(amount.split("+", 1)[0]) is not None


def _asked(n: int) -> list[int]:
    """The positions of the cards asked about: all of them, up to MAX_CARDS.

    On a longer page, the first and the last MAX_CARDS / 2 — the ends are where
    a menu or a footer read as cards would be — and in page order, so the same
    page gets the same verdict twice.
    """
    if n <= MAX_CARDS:
        return list(range(n))
    head = MAX_CARDS // 2
    return list(range(head)) + list(range(n - (MAX_CARDS - head), n))


def _state(card: Listing) -> dict[str, str]:
    """A card as the classifier sees it: its readable fields, blanks left out."""
    fields = {
        "title": card.title,
        "location": card.location,
        "asking_price": card.asking_price,
        "cash_flow": card.cashflow,
        "ebitda": card.ebitda,
        "revenue": card.revenue,
        "excerpt": card.excerpt[:_EXCERPT_CHARS],
    }
    return {k: v.strip() for k, v in fields.items() if v and v.strip()}
