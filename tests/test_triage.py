"""The triage question: what the classifier is shown, what it is asked, and how
its answer becomes a decision and a reason.

No network: the classifier is a fake that records every question. What is
pinned is the contract the 2026-09-28 evaluation measured — the lead-in, the
fixed option wording, the state's field names (with blanks left out and the
price/earnings multiple computed, because the classifier cannot divide), the
0.5 threshold on P(REJECT) — so a later edit that quietly changes one of them
shows up here rather than as a drop in agreement nobody measured.
"""
from __future__ import annotations

import hashlib

import pytest

from app.models import Listing
from app.services import legibility, triage
from app.services.triage import (
    CRITERIA,
    DETAIL_CHARS,
    LEAD_IN,
    STAGE_CARD,
    STAGE_CARD_ONLY,
    STAGE_DETAIL,
    TriageDecision,
    Triager,
    card_state,
    criteria_version,
    detail_state,
    price_to_earnings,
)
from app.services.typesafe import Choice, Noul, TypeSafeUnavailable

PROMPT = "Reject restaurants.\nReject if asking price / SDE > 6.0."


def _listing(**fields) -> Listing:
    base = dict(
        url="https://example.com/listing/1", title="HVAC Contractor", location="Oakland, CA",
        asking_price="$2,400,000", cashflow="$600,000", ebitda="", revenue="$3,100,000",
        excerpt="Commercial service contracts, 20 years.",
    )
    base.update(fields)
    return Listing(**base)


class FakeClassifier:
    """Answers every choice question with fixed probabilities, and records it —
    asked alone (`choice`, the detail stage) or inside the listing's one
    request (`ask`, the card stage, where it rides with the eligibility
    question)."""

    def __init__(self, p_review: float = 0.9, *, probabilities=None, choice=None,
                 error: Exception | None = None, model: str = "typesafe/jev-test") -> None:
        self.probabilities = (probabilities if probabilities is not None
                              else {"REVIEW": p_review, "REJECT": round(1 - p_review, 6)})
        self._choice = choice
        self.error = error
        self.model = model
        self.asked: list[tuple[dict, str, dict]] = []

    async def choice(self, state, instructions, criteria):
        self.asked.append((state, instructions, criteria))
        if self.error is not None:
            raise self.error
        probs = self.probabilities
        picked = self._choice or (max(probs, key=probs.get) if probs else "REVIEW")
        return Choice(choice=picked, probabilities=dict(probs),
                      confidence=probs.get(picked, 0.0), model=self.model)

    async def ask(self, state, questions):
        triage_q = questions["triage"]
        answer = await self.choice(state, triage_q["instructions"], triage_q["criteria"])
        return {"eligible": Noul(0.9, self.model), "triage": answer}


async def _card(triager: Triager, listing: Listing) -> TriageDecision:
    """The card-stage decision, as a sweep gets it: from the listing's one request."""
    return (await legibility.ask(triager._typesafe, listing, triager)).triage


class TestTheState:
    def test_the_card_fields_under_the_names_the_evaluation_used(self):
        state = card_state(_listing())
        assert state == {
            "title": "HVAC Contractor",
            "location": "Oakland, CA",
            "asking_price": "$2,400,000",
            "cash_flow_sde": "$600,000",
            "revenue": "$3,100,000",
            "listing_excerpt": "Commercial service contracts, 20 years.",
            "price_to_earnings_multiple": "4.00x",
        }

    def test_blank_fields_are_left_out_not_sent_empty(self):
        state = card_state(_listing(location="  ", revenue="", excerpt=""))
        assert "location" not in state and "revenue" not in state
        assert "listing_excerpt" not in state and "ebitda" not in state
        assert all(v for v in state.values())

    def test_the_multiple_uses_ebitda_when_there_is_no_cash_flow(self):
        assert price_to_earnings(_listing(cashflow="", ebitda="$800K")) == "3.00x"
        assert card_state(_listing(cashflow="Not Disclosed", ebitda="$1.2M"))[
            "price_to_earnings_multiple"] == "2.00x"

    @pytest.mark.parametrize("fields", [
        {"asking_price": "Not Disclosed"},
        {"asking_price": "$81,000 + Inventory"},
        {"cashflow": "", "ebitda": ""},
        {"cashflow": "$0", "ebitda": ""},
        {"cashflow": "$100k - $200k", "ebitda": ""},
    ])
    def test_no_multiple_unless_both_figures_are_exact(self, fields):
        """A multiple from "$81,000 + Inventory" would be a confident wrong
        number; an absent one is what the criteria call "not disclosed"."""
        listing = _listing(**fields)
        assert price_to_earnings(listing) is None
        assert "price_to_earnings_multiple" not in card_state(listing)

    def test_a_long_excerpt_is_cut_to_2000_characters(self):
        """A generic card can be most of a page; sent whole it would crowd the
        detail page out of the stage-2 state, which carries the card too."""
        assert triage.EXCERPT_CHARS == 2000
        state = card_state(_listing(excerpt="  " + "word " * 1000))
        assert 1990 <= len(state["listing_excerpt"]) <= 2000
        assert state["listing_excerpt"].startswith("word word")
        page = detail_state(_listing(excerpt="y" * 9000), "the page")
        assert len(page["listing_excerpt"]) == 2000 and page["detail_page_text"] == "the page"

    def test_the_detail_state_is_the_card_plus_the_page_cut_to_size(self):
        page = "x" * (DETAIL_CHARS + 500)
        state = detail_state(_listing(), page)
        assert {k: v for k, v in state.items() if k != "detail_page_text"} == card_state(_listing())
        assert len(state["detail_page_text"]) == DETAIL_CHARS
        assert "detail_page_text" not in detail_state(_listing(), "   ")


class TestTheQuestion:
    @pytest.mark.asyncio
    async def test_one_choice_question_with_the_fixed_lead_in_and_options(self):
        classifier = FakeClassifier()
        await _card(Triager(classifier, PROMPT + "\n\n"), _listing())

        ((state, instructions, criteria),) = classifier.asked
        assert instructions == (
            "Triage this business-for-sale listing for an acquisition search, using these "
            "criteria.\n\n" + PROMPT
        )
        assert instructions.startswith(LEAD_IN)
        assert criteria == CRITERIA
        assert set(criteria) == {"REVIEW", "REJECT"}
        assert criteria["REVIEW"].startswith("Keep for review: the listing does not clearly fail")
        assert criteria["REJECT"].startswith("Reject: the listing clearly fails at least one")
        assert state == card_state(_listing())

    @pytest.mark.asyncio
    async def test_the_detail_stage_asks_the_same_question_about_more_text(self):
        classifier = FakeClassifier()
        triager = Triager(classifier, PROMPT)
        await _card(triager, _listing())
        await triager.detail(_listing(), "# HVAC\n\nRecurring maintenance contracts.")
        (card_q, detail_q) = classifier.asked
        assert card_q[1:] == detail_q[1:], "same instructions and options"
        assert detail_q[0]["detail_page_text"].startswith("# HVAC")

    def test_the_question_is_the_one_asked_alone_and_inside_a_listing_s_request(self):
        """At the card stage it rides in the listing's one request, next to the
        eligibility question — the same instructions and options either way."""
        triager = Triager(FakeClassifier(), PROMPT)
        assert triager.question() == {"type": "choice", "instructions": LEAD_IN + PROMPT,
                                      "criteria": CRITERIA}

    def test_a_blank_prompt_is_refused(self):
        with pytest.raises(ValueError):
            Triager(FakeClassifier(), "  \n ")

    @pytest.mark.asyncio
    async def test_a_classifier_error_is_raised_for_the_caller_to_handle(self):
        triager = Triager(FakeClassifier(error=TypeSafeUnavailable("down")), PROMPT)
        with pytest.raises(TypeSafeUnavailable):
            await _card(triager, _listing())
        with pytest.raises(TypeSafeUnavailable):
            await triager.detail(_listing(), "page")


class TestTheDecision:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("p_review,expected", [
        (0.91, "REVIEW"), (0.51, "REVIEW"), (0.5, "REJECT"), (0.04, "REJECT"),
    ])
    async def test_reject_when_p_reject_is_at_least_a_half(self, p_review, expected):
        decision = await _card(Triager(FakeClassifier(p_review), PROMPT), _listing())
        assert decision.decision == expected
        assert decision.p_review == pytest.approx(p_review)

    @pytest.mark.asyncio
    async def test_the_threshold_not_the_argmax_decides(self):
        """An answer whose `choice` disagrees with its own probabilities is
        decided by the probabilities — the threshold is the rule."""
        classifier = FakeClassifier(probabilities={"REVIEW": 0.4, "REJECT": 0.6}, choice="REVIEW")
        decision = await _card(Triager(classifier, PROMPT), _listing())
        assert decision.decision == "REJECT"

    @pytest.mark.asyncio
    async def test_a_missing_probability_is_the_complement(self):
        only_reject = await _card(Triager(FakeClassifier(probabilities={"REJECT": 0.2}),
                                          PROMPT), _listing())
        assert only_reject.decision == "REVIEW" and only_reject.p_review == pytest.approx(0.8)
        only_review = await _card(Triager(FakeClassifier(probabilities={"REVIEW": 0.3}),
                                          PROMPT), _listing())
        assert only_review.decision == "REJECT" and only_review.p_review == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_the_decision_carries_its_stage_version_and_model(self):
        triager = Triager(FakeClassifier(0.88), PROMPT)
        card = await _card(triager, _listing())
        detail = await triager.detail(_listing(), "page")
        assert card.stage == STAGE_CARD and detail.stage == STAGE_DETAIL
        assert card.criteria_version == criteria_version(PROMPT) == triager.version
        assert card.model == "typesafe/jev-test"
        assert card.confidence == pytest.approx(0.88)


class TestCriteriaVersion:
    def test_the_first_8_hex_of_the_sha256_of_the_text(self):
        assert criteria_version(PROMPT) == hashlib.sha256(PROMPT.encode()).hexdigest()[:8]
        assert len(criteria_version(PROMPT)) == 8

    def test_surrounding_whitespace_is_not_a_new_version(self):
        assert criteria_version(f"\n  {PROMPT}\n") == criteria_version(PROMPT)

    def test_any_change_to_the_words_is(self):
        assert criteria_version(PROMPT.replace("6.0", "6.5")) != criteria_version(PROMPT)


class TestTheReason:
    """The classifier cannot write, so the reason is a template."""

    def _decision(self, decision, p, stage, guard=None):
        return TriageDecision(decision=decision, p_review=p, confidence=0.9, stage=stage,
                              criteria_version="abcd1234", guard=guard)

    def test_a_detail_page_review(self):
        assert self._decision("REVIEW", 0.91, STAGE_DETAIL).reason == (
            "REVIEW · P(review)=0.91 · card + detail page")

    def test_a_card_reject(self):
        assert self._decision("REJECT", 0.04, STAGE_CARD).reason == "REJECT · P(review)=0.04 · card"

    def test_a_card_only_review_says_why_and_how_sure_the_guard_was(self):
        card = self._decision("REVIEW", 0.88, STAGE_CARD)
        final = card.on_card_only(0.03)
        assert final.reason == (
            "REVIEW · P(review)=0.88 · card only — detail page not readable (P=0.03)")
        assert final.stage == STAGE_CARD_ONLY and final.guard == 0.03
        assert final.decision == card.decision and final.p_review == card.p_review

    def test_the_record_for_the_evidence(self):
        record = self._decision("REJECT", 0.123456, STAGE_CARD).record()
        assert record["p_review"] == 0.1235 and record["reason"].startswith("REJECT")
        assert record["criteria_version"] == "abcd1234" and record["guard"] is None


def test_the_module_imports_nothing_from_notion():
    """Triage decides; the store writes. The question must work for any store."""
    import inspect

    assert "stores.notion" not in inspect.getsource(triage)
