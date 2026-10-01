"""The Decision API client: what it sends, what it reads back, and how it fails.

Every call is faked with respx at the real endpoint, so these tests exercise the
real request/response shapes (captured from a live call on 2026-09-28) without a
network or an OpenRouter account.
"""
from __future__ import annotations

import asyncio
import json
import logging
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.services import typesafe
from app.services.typesafe import (
    API,
    Choice,
    Noul,
    RawAnswer,
    TypeSafeAuthError,
    TypeSafeClient,
    TypeSafeCreditError,
    TypeSafeError,
    TypeSafeNotConfigured,
    TypeSafeUnavailable,
)

KEY = "sk-or-v1-THIS-IS-THE-SECRET-KEY-0123456789abcdef"
MODEL_ANSWERED = "typesafe/jev-1.13-20260917"


def _reply(answers: dict, model: str = MODEL_ANSWERED, cost: float = 0.000014406) -> httpx.Response:
    return httpx.Response(200, json={
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": 343, "output_tokens": 48, "cost": cost},
        "id": "gen-1", "provider": "TypeSafe",
    })


def _noul(p: float = 0.24) -> httpx.Response:
    return _reply({"q": {"type": "noul", "noul": p}})


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "code": status}})


def _client(key: str = KEY, model: str = "jev-latest") -> TypeSafeClient:
    return TypeSafeClient(lambda: key, lambda: model)


@pytest.fixture
def slept(monkeypatch):
    """Record the backoff instead of waiting it out."""
    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    monkeypatch.setattr(typesafe, "_sleep", _record)
    return delays


class TestTheRequest:
    @respx.mock
    @pytest.mark.asyncio
    async def test_body_and_headers_are_the_documented_shape(self):
        route = respx.post(API).mock(return_value=_reply(
            {"q": {"type": "choice", "choice": "billing",
                   "probabilities": {"technical": 0, "billing": 1}, "confidence": 1}}))
        await _client().choice(
            "My card was charged twice", "Which team should handle this ticket?",
            {"billing": "Payment and charge issues", "technical": "Bugs and errors"},
        )
        request = route.calls.last.request
        assert json.loads(request.content) == {
            "model": "jev-latest",
            "state": "My card was charged twice",
            "questions": {"q": {
                "type": "choice",
                "instructions": "Which team should handle this ticket?",
                "criteria": {"billing": "Payment and charge issues",
                             "technical": "Bugs and errors"},
            }},
        }
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        # The header is the ONLY place the key goes.
        assert KEY not in str(request.url)
        assert KEY not in request.content.decode()

    @respx.mock
    @pytest.mark.asyncio
    async def test_state_may_be_structured(self):
        route = respx.post(API).mock(return_value=_noul())
        state = {"title": "Pool service route", "asking_price": "$1,200,000"}
        await _client().noul(state, "This is a business for sale")
        assert json.loads(route.calls.last.request.content)["state"] == state

    @respx.mock
    @pytest.mark.asyncio
    async def test_key_and_model_are_read_on_every_call(self):
        """A key saved in Settings must apply to the next question, no restart."""
        route = respx.post(API).mock(return_value=_noul())
        current = {"key": "sk-or-v1-first", "model": "jev-latest"}
        client = TypeSafeClient(lambda: current["key"], lambda: current["model"])
        await client.noul("x", "y")
        current.update(key="sk-or-v1-second", model="jev-2")
        await client.noul("x", "y")
        first, second = (c.request for c in route.calls)
        assert first.headers["Authorization"] == "Bearer sk-or-v1-first"
        assert second.headers["Authorization"] == "Bearer sk-or-v1-second"
        assert json.loads(second.content)["model"] == "jev-2"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_blank_model_means_the_default(self):
        route = respx.post(API).mock(return_value=_noul())
        await _client(model="  ").noul("x", "y")
        assert json.loads(route.calls.last.request.content)["model"] == "jev-latest"

    @pytest.mark.asyncio
    async def test_too_many_options_is_refused_before_sending(self):
        with pytest.raises(ValueError, match="at most 255"):
            await _client().choice("x", "y", {f"o{i}": "d" for i in range(256)})


class TestTheAnswers:
    @respx.mock
    @pytest.mark.asyncio
    async def test_choice_carries_every_probability_and_the_real_model(self):
        respx.post(API).mock(return_value=_reply(
            {"q": {"type": "choice", "choice": "billing",
                   "probabilities": {"technical": 0.1, "billing": 0.9}, "confidence": 0.9}}))
        answer = await _client().choice("x", "y", {"billing": "b", "technical": "t"})
        assert answer == Choice(choice="billing",
                                probabilities={"technical": 0.1, "billing": 0.9},
                                confidence=0.9, model=MODEL_ANSWERED)

    @respx.mock
    @pytest.mark.asyncio
    async def test_noul_is_a_probability(self):
        respx.post(API).mock(return_value=_noul(0.24))
        assert await _client().noul("x", "The customer conveys urgency") == 0.24

    @respx.mock
    @pytest.mark.asyncio
    async def test_ask_answers_several_questions_by_name(self):
        respx.post(API).mock(return_value=_reply({
            "team": {"type": "choice", "choice": "billing",
                     "probabilities": {"technical": 0, "billing": 1}, "confidence": 1},
            "urgent": {"type": "noul", "noul": 0.24},
            "tone": {"type": "score", "score": 3},
        }))
        answers = await _client().ask("My card was charged twice", {
            "team": {"type": "choice", "instructions": "Which team?",
                     "criteria": {"billing": "b", "technical": "t"}},
            "urgent": {"type": "noul", "instructions": "The customer conveys urgency"},
            "tone": {"type": "score", "instructions": "How angry"},
        })
        assert isinstance(answers["team"], Choice) and answers["team"].choice == "billing"
        assert answers["urgent"] == Noul(probability=0.24, model=MODEL_ANSWERED)
        # A type this module does not model is passed through, not refused.
        assert isinstance(answers["tone"], RawAnswer)
        assert answers["tone"].type == "score" and answers["tone"].data["score"] == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_missing_answer_is_an_error_not_a_guess(self):
        respx.post(API).mock(return_value=_reply({"other": {"type": "noul", "noul": 1}}))
        with pytest.raises(TypeSafeError, match="no answer for 'q'"):
            await _client().noul("x", "y")

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_malformed_answer_is_an_error(self):
        respx.post(API).mock(return_value=_reply({"q": {"type": "noul", "noul": "lots"}}))
        with pytest.raises(TypeSafeError, match="not in a shape"):
            await _client().noul("x", "y")


class TestRetries:
    @respx.mock
    @pytest.mark.asyncio
    async def test_5xx_is_retried_with_backoff(self, slept):
        route = respx.post(API).mock(side_effect=[
            httpx.Response(503), httpx.Response(502), _noul(0.7)])
        assert await _client().noul("x", "y") == 0.7
        assert route.call_count == 3
        assert slept == [0.5, 1.0]

    @respx.mock
    @pytest.mark.asyncio
    async def test_transport_errors_are_retried(self, slept):
        route = respx.post(API).mock(side_effect=[
            httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), _noul(0.7)])
        assert await _client().noul("x", "y") == 0.7
        assert route.call_count == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_honours_a_numeric_retry_after(self, slept):
        respx.post(API).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}), _noul()])
        await _client().noul("x", "y")
        assert slept == [3.0]

    @respx.mock
    @pytest.mark.asyncio
    async def test_429_with_an_http_date_retry_after_does_not_crash(self, slept):
        """RFC 9110 allows a date here; float() on it is how NotionClient would die."""
        when = datetime.now(timezone.utc) + timedelta(seconds=5)
        respx.post(API).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": format_datetime(when, usegmt=True)}),
            _noul()])
        await _client().noul("x", "y")
        assert len(slept) == 1 and 0 <= slept[0] <= 6

    @pytest.mark.parametrize("header", ["soon", "", "Wed, 99 Foo 2026", "nan", "-5"])
    def test_a_nonsense_retry_after_falls_back_to_the_backoff(self, header):
        assert typesafe._retry_after(header, 1.5) in (1.5, 0.0)

    def test_a_huge_retry_after_is_capped(self):
        assert typesafe._retry_after("86400", 1.0) == typesafe._MAX_RETRY_AFTER_SEC

    @respx.mock
    @pytest.mark.asyncio
    async def test_giving_up_says_what_went_wrong_last(self, slept):
        route = respx.post(API).mock(side_effect=[
            httpx.ConnectError("refused"), httpx.Response(500),
            httpx.Response(502), httpx.Response(503)])
        with pytest.raises(TypeSafeUnavailable) as caught:
            await _client().noul("x", "y")
        assert route.call_count == typesafe._MAX_ATTEMPTS
        assert "HTTP 503" in str(caught.value)
        # No sleep after the last attempt: nothing is left to wait for.
        assert len(slept) == typesafe._MAX_ATTEMPTS - 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_last_error_is_kept_even_when_it_is_a_transport_error(self, slept):
        respx.post(API).mock(side_effect=httpx.ConnectError("connection refused"))
        with pytest.raises(TypeSafeUnavailable) as caught:
            await _client().noul("x", "y")
        assert "ConnectError" in str(caught.value) and "connection refused" in str(caught.value)
        # Not chained: the httpx exception holds the request, which holds the key.
        assert caught.value.__cause__ is None and caught.value.__context__ is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_client_errors_are_not_retried(self, slept):
        route = respx.post(API).mock(return_value=_error(401, "No auth credentials found"))
        with pytest.raises(TypeSafeAuthError):
            await _client().noul("x", "y")
        assert route.call_count == 1 and slept == []


class TestPlainLanguageErrors:
    @pytest.mark.parametrize("status", [401, 403])
    @respx.mock
    @pytest.mark.asyncio
    async def test_a_rejected_key(self, status):
        respx.post(API).mock(return_value=_error(status, "User not found."))
        with pytest.raises(TypeSafeAuthError) as caught:
            await _client().noul("x", "y")
        assert "OpenRouter rejected the key" in str(caught.value)
        assert "Settings → Decision API" in str(caught.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_empty_account(self):
        respx.post(API).mock(return_value=_error(402, "Insufficient credits"))
        with pytest.raises(TypeSafeCreditError) as caught:
            await _client().noul("x", "y")
        assert "the OpenRouter account is out of credits" in str(caught.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_error_inside_a_200_is_still_an_error(self):
        respx.post(API).mock(return_value=httpx.Response(
            200, json={"error": {"message": "Insufficient credits", "code": 402}}))
        with pytest.raises(TypeSafeCreditError):
            await _client().noul("x", "y")

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_unknown_model_points_at_the_model_setting(self):
        respx.post(API).mock(return_value=_error(400, "Model typesafe/jev-nope does not exist"))
        with pytest.raises(TypeSafeError) as caught:
            await _client(model="jev-nope").noul("x", "y")
        assert not isinstance(caught.value, (TypeSafeAuthError, TypeSafeCreditError))
        message = str(caught.value)
        # The upstream reason is in the first sentence — the part Settings keeps.
        assert message.startswith(
            "The Decision API refused this request "
            "(HTTP 400: Model typesafe/jev-nope does not exist).")
        assert "Check the Model" in message

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_key_is_refused_without_calling_anyone(self, blank):
        route = respx.post(API).mock(return_value=_noul())
        with pytest.raises(TypeSafeNotConfigured) as caught:
            await _client(key=blank).noul("x", "y")
        assert "Settings → Decision API" in str(caught.value)
        assert route.call_count == 0

    def test_every_error_is_a_typesafe_error(self):
        for cls in (TypeSafeAuthError, TypeSafeCreditError, TypeSafeUnavailable,
                    TypeSafeNotConfigured):
            assert issubclass(cls, TypeSafeError)


class TestTheKeyNeverLeaks:
    """Upstream text is echoed into messages a person reads, a verdict saved on
    the volume, and what an agent is told. None of them may carry the key."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_error_body_that_echoes_the_key_is_scrubbed(self, caplog):
        caplog.set_level(logging.DEBUG)
        respx.post(API).mock(return_value=_error(
            401, f"Invalid API key {KEY}; header was Bearer {KEY}"))
        with pytest.raises(TypeSafeAuthError) as caught:
            await _client().noul("x", "y")
        assert KEY not in str(caught.value)
        assert "THIS-IS-THE-SECRET" not in str(caught.value)
        assert KEY not in caplog.text

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_key_shaped_string_is_scrubbed_even_when_it_is_not_ours(self):
        other = "sk-or-v1-someone-elses-key-999"
        respx.post(API).mock(return_value=_error(400, f"bad request for {other}"))
        with pytest.raises(TypeSafeError) as caught:
            await _client().noul("x", "y")
        assert other not in str(caught.value)

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_and_giving_up_never_log_the_key(self, caplog, slept):
        caplog.set_level(logging.DEBUG)
        respx.post(API).mock(side_effect=[
            httpx.ConnectError(f"proxy said no to Bearer {KEY}"),
            httpx.Response(500, text=f"upstream error {KEY}"),
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(503, json={"error": {"message": KEY}}),
        ])
        with pytest.raises(TypeSafeUnavailable) as caught:
            await _client().noul("x", "y")
        assert KEY not in str(caught.value)
        assert KEY not in caplog.text
        assert "retrying" in caplog.text  # the retries were logged — just not the key

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_long_error_body_is_trimmed(self):
        respx.post(API).mock(return_value=_error(400, "x" * 5000))
        with pytest.raises(TypeSafeError) as caught:
            await _client().noul("x", "y")
        assert len(str(caught.value)) < 400


class TestCheck:
    @respx.mock
    @pytest.mark.asyncio
    async def test_a_working_key(self):
        respx.post(API).mock(return_value=_reply({"check": {"type": "noul", "noul": 0.9}}))
        result = await _client().check()
        assert result.ok is True
        assert result.model == MODEL_ANSWERED
        assert result.cost == pytest.approx(0.000014406)
        assert result.message.startswith(f"Working: {MODEL_ANSWERED} answered in ")
        assert result.error is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_it_checks_the_candidate_not_the_saved_key(self):
        """Settings tests what was typed before saving it."""
        route = respx.post(API).mock(return_value=_reply({"check": {"type": "noul", "noul": 1}}))
        await _client(key="sk-or-v1-saved").check(key="sk-or-v1-typed", model="jev-typed")
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer sk-or-v1-typed"
        assert json.loads(request.content)["model"] == "jev-typed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_rejected_key_is_a_verdict_not_an_exception(self):
        respx.post(API).mock(return_value=_error(401, "User not found."))
        result = await _client().check()
        assert result.ok is False
        assert "OpenRouter rejected the key" in result.message
        assert isinstance(result.error, TypeSafeAuthError)
        assert KEY not in result.message

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_empty_account_is_a_verdict(self):
        respx.post(API).mock(return_value=_error(402, "Insufficient credits"))
        result = await _client().check()
        assert result.ok is False and isinstance(result.error, TypeSafeCreditError)

    @pytest.mark.asyncio
    async def test_no_key_is_a_verdict(self):
        result = await _client(key="").check()
        assert result.ok is False and isinstance(result.error, TypeSafeNotConfigured)

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_outage_is_a_verdict_that_says_so(self, slept):
        respx.post(API).mock(return_value=httpx.Response(503))
        result = await _client().check()
        assert result.ok is False and isinstance(result.error, TypeSafeUnavailable)


class TestPerCallBudget:
    """A call can trade the retry budget for an answer now."""

    @pytest.fixture
    def timeouts(self, monkeypatch):
        seen: list[float] = []
        real = httpx.AsyncClient

        def client(*args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return real(*args, **kwargs)

        monkeypatch.setattr(typesafe.httpx, "AsyncClient", client)
        return seen

    @respx.mock
    @pytest.mark.asyncio
    async def test_check_makes_one_attempt_with_a_ten_second_timeout(self, slept, timeouts):
        """A check is asked while someone waits: before a sweep starts, or on the
        Settings page. With the full budget a dead service took two minutes."""
        route = respx.post(API).mock(return_value=httpx.Response(503))
        result = await _client().check()
        assert route.call_count == 1 and slept == []
        assert timeouts == [10.0]
        assert "did not answer after 1 attempt (HTTP 503)" in result.message

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_call_can_name_its_own_budget(self, slept, timeouts):
        route = respx.post(API).mock(return_value=httpx.Response(503))
        with pytest.raises(TypeSafeUnavailable):
            await _client().noul("x", "y", attempts=2, timeout=5)
        assert route.call_count == 2 and len(slept) == 1
        assert timeouts == [5.0]

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_default_budget_is_unchanged(self, slept, timeouts):
        route = respx.post(API).mock(return_value=httpx.Response(503))
        with pytest.raises(TypeSafeUnavailable):
            await _client().ask("x", {"q": {"type": "noul", "instructions": "y"}})
        assert route.call_count == typesafe._MAX_ATTEMPTS == 4
        assert timeouts == [30.0]


class TestConcurrency:
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_more_than_twenty_requests_are_in_flight(self):
        """One ceiling, twenty, for everything that asks the classifier in this
        process. Five is each sweep's own default (`TYPESAFE_PARALLEL`); the
        ceiling is what lets a sweep asking for more actually get it, and what
        still bounds two sweeps together."""
        assert typesafe.TYPESAFE_PARALLEL == 5
        assert typesafe.TYPESAFE_MAX_PARALLEL == 20
        in_flight = peak = 0

        async def answer(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return _noul()

        respx.post(API).mock(side_effect=answer)
        client = _client()
        await asyncio.gather(*(client.noul("x", "y") for _ in range(50)))
        assert peak == typesafe.TYPESAFE_MAX_PARALLEL

    def test_one_client_survives_a_new_event_loop(self):
        """The client lives on app.state and outlives loops (every TestClient,
        every asyncio.run). A semaphore from a dead loop fails as soon as it is
        contended, so contend it in two loops in a row."""
        client = _client()

        async def answer(request):
            await asyncio.sleep(0.005)
            return _noul()

        async def burst():
            await asyncio.gather(*(client.noul("x", "y") for _ in range(20)))

        with respx.mock:
            respx.post(API).mock(side_effect=answer)
            asyncio.run(burst())
            asyncio.run(burst())
