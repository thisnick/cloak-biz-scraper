"""Triage: REVIEW or REJECT for one listing, decided by the TypeSafe Classifier (e.g. Jev).

**One question, two answers.** The caller's criteria text — a person's own
words, read from wherever they keep them — becomes the question's
instructions, behind a fixed lead-in. The options are fixed too: REVIEW (does
not clearly fail any criterion, or the evidence is missing or ambiguous) and
REJECT (clearly fails at least one, with specific evidence). The criteria are
not decomposed into one question per rule: the person wrote them as one
judgement, and asked as one they matched the decisions an LLM had been making
on 96% of the rows made under the current criteria.

**REJECT needs P(REJECT) ≥ 0.5.** The asymmetry is in the option wording, not
the threshold: "the evidence is missing" already sends a doubtful listing to
REVIEW, so the threshold is the plain majority.

**The classifier cannot divide.** A rule like "reject when price / SDE > 6.0"
went from 56% to 83% agreement once the multiple was computed here and handed
over as a field (`price_to_earnings_multiple`), so it is — but only when both
figures parse exactly (`stores/money.parse_money`); "$81,000 + Inventory" has
no multiple, rather than a wrong one.

**It cannot write either,** so the reason recorded next to a decision is a
template — the decision, P(review), and what it was decided on — not prose.

**`criteria_version`** is the first 8 hex characters of the criteria text's
sha256: a row triaged under different words says so, and changing one comma
changes it, which is the point — the row records exactly which text judged it.

**At the card stage the question rides in the listing's one request**
(`legibility.ask`): the sweep asks every question about one listing element
together — is it a business for sale now, and REVIEW or REJECT — in one
request whose state is that card. One listing per request, never several:
bundling 5–10 listings (the criteria sent once, in the state) cut input
tokens to ~30%, but agreement with Bot Triage fell from 96% to 93% on
current-criteria rows — about three times as many false REVIEWs. Adding the
eligibility question to the same request changed none of 219 answers
(96.3% agreement either way) for 4% more input tokens. The detail stage
asks this question alone, on the card and the detail page.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..models import Listing
from ..stores.money import parse_money

LEAD_IN = (
    "Triage this business-for-sale listing for an acquisition search, using these "
    "criteria.\n\n"
)

# The option wording is what the 2026-09-28 evaluation used; it carries the
# "keep it when in doubt" posture, so the threshold below can stay a plain 0.5.
CRITERIA: dict[str, str] = {
    "REVIEW": "Keep for review: the listing does not clearly fail any criterion, or the "
              "evidence is missing, incomplete or ambiguous.",
    "REJECT": "Reject: the listing clearly fails at least one criterion, with specific "
              "evidence in the listing.",
}
REVIEW, REJECT = "REVIEW", "REJECT"
REJECT_THRESHOLD = 0.5

# The detail page's text, at most. The classifier reads ~32k tokens per question
# and the criteria and card take a few thousand of them; 60k characters of
# page markdown stays under that with room to spare.
DETAIL_CHARS = 60_000
# The card's excerpt, at most. A card is a few lines; an "excerpt" longer than
# this is a whole page read as one card, and sent in full it would crowd the
# detail page out of the stage-2 state (which holds the card too).
EXCERPT_CHARS = 2_000

# What a decision was made on — the last part of the recorded reason.
STAGE_CARD = "card"
STAGE_DETAIL = "card + detail page"
STAGE_CARD_ONLY = "card only — detail page not readable"


def criteria_version(prompt: str) -> str:
    """The first 8 hex characters of sha256 of the criteria text (stripped)."""
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:8]


def instructions(prompt: str) -> str:
    """The question's instructions: the fixed lead-in, then the caller's text."""
    return LEAD_IN + prompt.strip()


def price_to_earnings(listing: Listing) -> str | None:
    """Asking price ÷ cash flow (or EBITDA when there is no cash flow), as "4.25x".

    None unless the price and the earnings figure each parse as one exact amount
    and the earnings are positive: a multiple built from "$81,000 + Inventory"
    or a range would be a confident wrong number, and an absent field is what
    the criteria already treat as "not disclosed".
    """
    price = parse_money(listing.asking_price)
    earnings = parse_money(listing.cashflow) or parse_money(listing.ebitda)
    if not price or not earnings or earnings <= 0:
        return None
    return f"{price / earnings:.2f}x"


def card_state(listing: Listing) -> dict[str, str]:
    """The listing as its card showed it, for the classifier. Blank fields are
    left out rather than sent empty — an empty "ebitda" reads as a claim — and
    the excerpt is cut to `EXCERPT_CHARS`."""
    fields = {
        "title": listing.title,
        "location": listing.location,
        "asking_price": listing.asking_price,
        "cash_flow_sde": listing.cashflow,
        "ebitda": listing.ebitda,
        "revenue": listing.revenue,
        "listing_excerpt": (listing.excerpt or "").strip()[:EXCERPT_CHARS],
    }
    state = {k: v.strip() for k, v in fields.items() if v and v.strip()}
    multiple = price_to_earnings(listing)
    if multiple:
        state["price_to_earnings_multiple"] = multiple
    return state


def detail_state(listing: Listing, markdown: str) -> dict[str, str]:
    """The card state plus the detail page's text, cut to `DETAIL_CHARS`."""
    state = card_state(listing)
    text = (markdown or "").strip()
    if text:
        state["detail_page_text"] = text[:DETAIL_CHARS]
    return state


@dataclass(frozen=True)
class TriageDecision:
    """One listing's verdict, and everything the recorded reason says about it.

    `p_review` is the classifier's probability for REVIEW; `decision` follows
    from P(REJECT) ≥ 0.5. `guard` is set only on a decision made on the card
    because the detail page turned out not to be the listing's content (the
    guard's probability that it was).
    """

    decision: str
    p_review: float
    confidence: float
    stage: str
    criteria_version: str
    model: str = ""
    guard: float | None = None

    @property
    def reason(self) -> str:
        """The Triage Reason text, e.g. `REVIEW · P(review)=0.91 · card + detail page`."""
        text = f"{self.decision} · P(review)={self.p_review:.2f} · {self.stage}"
        if self.guard is not None:
            text += f" (P={self.guard:.2f})"
        return text

    def on_card_only(self, guard: float) -> "TriageDecision":
        """This card decision, re-stated as final because the detail page was a
        wall, an error or a removed listing (P(real content) = `guard`)."""
        return TriageDecision(
            decision=self.decision, p_review=self.p_review, confidence=self.confidence,
            stage=STAGE_CARD_ONLY, criteria_version=self.criteria_version, model=self.model,
            guard=guard,
        )

    def record(self) -> dict[str, Any]:
        """For the run's evidence."""
        return {"decision": self.decision, "p_review": round(self.p_review, 4),
                "confidence": round(self.confidence, 4), "stage": self.stage,
                "criteria_version": self.criteria_version, "model": self.model,
                "guard": None if self.guard is None else round(self.guard, 4),
                "reason": self.reason}


class Triager:
    """The one triage question with one criteria text.

    Built per sweep (or per evaluation run) over the shared TypeSafe client, so
    the client's concurrency ceiling is the only one. At the card stage the
    question goes out inside the listing's one request (`question`, read back
    with `decision`); at the detail stage `detail` asks it on its own. Raises
    the client's TypeSafeError when it cannot answer; what that means is the
    caller's call.
    """

    def __init__(self, typesafe, prompt: str) -> None:
        if not (prompt or "").strip():
            raise ValueError("a triage prompt needs some criteria text")
        self._typesafe = typesafe
        self.prompt = prompt.strip()
        self.version = criteria_version(self.prompt)
        self._instructions = instructions(self.prompt)

    def question(self) -> dict[str, Any]:
        """The triage question, for a request that asks it alongside others."""
        return {"type": "choice", "instructions": self._instructions,
                "criteria": dict(CRITERIA)}

    async def detail(self, listing: Listing, markdown: str) -> TriageDecision:
        """The decision on the card and the detail page's text."""
        answer = await self._typesafe.choice(detail_state(listing, markdown),
                                             self._instructions, CRITERIA)
        return self.decision(answer, STAGE_DETAIL)

    def decision(self, answer, stage: str) -> TriageDecision:
        """The decision a choice answer (`Choice`) to `question` makes."""
        probs = answer.probabilities or {}
        if REJECT in probs:
            p_reject = float(probs[REJECT])
        elif REVIEW in probs:
            p_reject = 1.0 - float(probs[REVIEW])
        else:
            p_reject = 1.0 if answer.choice == REJECT else 0.0
        p_review = float(probs.get(REVIEW, 1.0 - p_reject))
        return TriageDecision(
            decision=REJECT if p_reject >= REJECT_THRESHOLD else REVIEW,
            p_review=p_review, confidence=float(answer.confidence), stage=stage,
            criteria_version=self.version, model=answer.model,
        )
