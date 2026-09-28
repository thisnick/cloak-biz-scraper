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
* **The TypeSafe Classifier (e.g. Jev)**, when a key is saved: a yes/no on a
  few sampled cards. It catches what the code cannot see — well-formed cards
  that are blog posts, franchise ads, or a site's own navigation.

The classifier half is best-effort here. BizBuySell pages do not depend on it,
so a classifier outage must not fail them: the error is logged and recorded on
the verdict, and the code checks still decide. (A source that does depend on
the classifier fails on its own calls long before this one.)

A page with zero cards is never judged — that is an empty last page, and what
it means is the sweep's business, exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..models import Listing
from ..stores.money import parse_money
from .typesafe import TypeSafeError

logger = logging.getLogger("cloakbiz.legibility")

# The statement put to the classifier about each sampled card.
QUESTION = "This is a legible listing of a business for sale."

# At least this share of cards must have both a title and a link.
MIN_COMPLETE = 0.7
# At least this share of digit-bearing asking prices must read as an amount.
MIN_PRICES_READ = 0.5
# The page fails when the classifier's mean over the sample is below this.
MIN_CLASSIFIER_MEAN = 0.5
# Cards asked about per page. Three spread across the page are enough to tell a
# list of listings from a list of something else; the rest would only add cost.
SAMPLE_SIZE = 3
# Enough of a card's excerpt to recognise it; a card is not a detail page.
_EXCERPT_CHARS = 1500
# How much of an unreadable price to quote in the failure message.
_QUOTE_CHARS = 40

_DIGIT = re.compile(r"\d")


@dataclass(frozen=True)
class Verdict:
    """What the check decided about one page.

    `listings` are the cards that survive (those with a title and a link) — the
    only ones the sweep keeps. `reason` is set when `ok` is False and is written
    for the person reading the job's error: what was wrong, on which page, and
    that nothing from it was kept.
    """

    listings: list[Listing]
    ok: bool = True
    reason: str = ""
    dropped: int = 0
    # The classifier's mean over the sample; None when it was not asked (no key)
    # or could not answer.
    classifier_mean: float | None = None
    classifier_samples: int = 0
    # Why the classifier half was skipped, when it was asked and failed.
    classifier_error: str = ""

    def record(self, page: int) -> dict[str, Any]:
        """A small, JSON-safe summary for the run's evidence."""
        out: dict[str, Any] = {"page": page, "ok": self.ok, "kept": len(self.listings),
                               "dropped": self.dropped}
        if self.reason:
            out["reason"] = self.reason
        if self.classifier_mean is not None:
            out["classifier_mean"] = round(self.classifier_mean, 3)
            out["classifier_samples"] = self.classifier_samples
        if self.classifier_error:
            out["classifier_error"] = self.classifier_error
        return out


async def check(listings: list[Listing], *, page: int, classifier=None) -> Verdict:
    """Judge one page's cards. Never raises for a classifier failure.

    `classifier` is a TypeSafe client (anything with `noul(state, instructions)`),
    passed only when a key is saved; None skips that half.
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

    sample = _sample(complete)
    answers = await asyncio.gather(
        *(classifier.noul(_state(card), QUESTION) for card in sample),
        return_exceptions=True,
    )
    failed = next((a for a in answers if isinstance(a, BaseException)), None)
    if failed is not None:
        if not isinstance(failed, TypeSafeError):
            raise failed
        # Best-effort: the code checks above have already passed this page.
        logger.warning("legibility: classifier skipped on page %d: %s", page, failed)
        return Verdict(listings=complete, dropped=dropped, classifier_error=str(failed))

    mean = sum(float(a) for a in answers) / len(answers)
    if mean < MIN_CLASSIFIER_MEAN:
        return Verdict(
            listings=[], ok=False, dropped=dropped,
            classifier_mean=mean, classifier_samples=len(answers),
            reason=(
                f"The cards on page {page} don't read as business listings (mean "
                f"{mean:.2f} on {len(answers)} sample{'s' if len(answers) != 1 else ''}) — "
                f"nothing from this page was kept."
            ),
        )
    return Verdict(listings=complete, dropped=dropped,
                   classifier_mean=mean, classifier_samples=len(answers))


def _reads_as_amount(value: str) -> bool:
    """Whether an asking price is an amount, possibly with a trailing qualifier.

    `parse_money` refuses "$81,000 + Inventory", and rightly — as a number to
    *store* it would understate the price. But the question here is only
    whether the field holds a price at all, and a qualified price is one: a
    one-listing broker page quoting "+ Inventory" is a perfectly good page.
    """
    return parse_money(value) is not None or parse_money(value.split("+", 1)[0]) is not None


def _sample(cards: list[Listing]) -> list[Listing]:
    """Up to SAMPLE_SIZE cards, spread from the first to the last.

    Spread rather than the first few, because a page's top cards are the ones
    most likely to be something else (a featured ad, a promoted broker); and
    fixed rather than random, so the same page gets the same verdict twice.
    """
    n = len(cards)
    if n <= SAMPLE_SIZE:
        return list(cards)
    picks = sorted({round(i * (n - 1) / (SAMPLE_SIZE - 1)) for i in range(SAMPLE_SIZE)})
    return [cards[i] for i in picks]


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
