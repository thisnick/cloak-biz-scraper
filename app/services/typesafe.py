"""The TypeSafe Classifier (e.g. Jev): yes/no and multiple-choice answers about text.

Jev is a classifier, not a language model, and that difference is the reason it
is here. It cannot write anything: it takes some text (the "state") and named
questions about it, and returns for each one either a probability that a
statement holds (`noul`) or a pick from options the caller wrote, with a
probability for every option (`choice`). That is exactly the shape of the
decisions a sweep needs — which links on a page are the listings, whether a card
is a business for sale, REVIEW or REJECT — and it answers them in ~100 ms for a
fraction of a cent, deterministically enough to test. An LLM doing the same job
is slower, dearer, and free to answer a question nobody asked.

It is reached through OpenRouter (`POST /api/v1/systemone`) with an OpenRouter
key, so the key the user pastes is an OpenRouter key, and the errors they can
act on are OpenRouter's: a rejected key, or an account out of credits. Those two
are separate exception classes because they are the two things a person fixes
in different places, and a caller refusing a sweep up front needs to say which.

Everything is optional. Without a key nothing here is ever called, and the rest
of the app behaves exactly as it did before this module existed.

**The key travels in one place: the Authorization header.** It is never logged,
never put in a URL, and never echoed back from an error body — upstream error
text is trimmed and scrubbed before it reaches a message, because a message is
what gets shown on the Settings page, saved as the last check's summary, and
returned to an agent. The httpx exception that caused a failure is deliberately
not chained onto ours: it holds the request, and the request holds the header.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Union

import httpx

logger = logging.getLogger("cloakbiz.typesafe")

API = "https://openrouter.ai/api/v1/systemone"
DEFAULT_MODEL = "jev-latest"

_TIMEOUT_SEC = 30.0
# One request plus three retries. Each attempt is ~100 ms when healthy, so the
# budget is spent almost entirely in backoff — enough to ride out a blip, short
# enough that a real outage is reported while the person is still looking.
_MAX_ATTEMPTS = 4
# `check()`'s own budget: one attempt, ten seconds. It is asked before a sweep
# starts and from the Settings page, where the answer is wanted now; with the
# full budget a dead service took ~2 minutes to be refused.
CHECK_ATTEMPTS = 1
CHECK_TIMEOUT_SEC = 10.0
_BACKOFF_SEC = 0.5
# A Retry-After longer than this is a server asking us to go away for a while;
# waiting it out inside one request would just look like a hang.
_MAX_RETRY_AFTER_SEC = 20.0
# Classifier requests in flight at once — the one parallelism limit for
# everything that asks the classifier. The client holds every caller to it
# (one shared ceiling per process), and the callers that fan out — a sweep's
# per-listing requests above all — gate themselves on the same number, so a
# 50-listing page never opens 50 connections at once and earns 429s for all
# of them, and an outage is met by at most this many requests.
TYPESAFE_PARALLEL = 5
# TypeSafe's documented ceiling for one choice question.
_MAX_CHOICE_OPTIONS = 255
# Upstream error text shown to a person, at most. Enough for "Model x does not
# exist"; not enough to paste a whole HTML error page into a banner.
_ERROR_TEXT_LIMIT = 200

_SETTINGS_PATH = "Settings → TypeSafe Classifier (e.g. Jev)"

# The backoff's sleep, by name, so a test can skip the waiting without
# replacing asyncio.sleep for everything else running on the loop.
_sleep = asyncio.sleep


class TypeSafeError(RuntimeError):
    """Anything the classifier could not answer, phrased for someone with no terminal."""


class TypeSafeNotConfigured(TypeSafeError):
    """No OpenRouter key is saved, so there is nothing to ask with."""


class TypeSafeAuthError(TypeSafeError):
    """OpenRouter refused the key (401/403)."""


class TypeSafeCreditError(TypeSafeError):
    """The key is fine but its OpenRouter account has no credits left (402)."""


class TypeSafeUnavailable(TypeSafeError):
    """No answer after every retry: a timeout, a network failure, a 5xx or a 429."""


# ── answers ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Choice:
    """One option picked from the caller's criteria.

    `probabilities` covers every option, not just the winner, because the
    interesting decisions are made on the margin: "REJECT when P(REJECT) ≥ 0.5"
    and "fill the field only at confidence ≥ 0.8" both need more than the name
    of the option that came first.
    """

    choice: str
    probabilities: dict[str, float]
    confidence: float
    # The model that actually answered (e.g. "typesafe/jev-1.13-20260917"), not
    # the alias that was asked for — "jev-latest" moves, and a decision recorded
    # against it should say which Jev made it.
    model: str = ""


@dataclass(frozen=True)
class Noul:
    """The probability, 0–1, that the question's statement holds for the state
    (the API's `noul` field)."""

    probability: float
    model: str = ""


@dataclass(frozen=True)
class RawAnswer:
    """An answer type this module does not model (e.g. `score`), passed through
    unparsed rather than refused — asking one should not need a code change here."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    model: str = ""


Answer = Union[Choice, Noul, RawAnswer]


@dataclass(frozen=True)
class TypeSafeCheck:
    """The verdict of `check()`: does this key and model answer a question?

    `message` is written for the Settings page and is safe to store as the last
    check's summary. `error` is the exception behind a failure, so a caller can
    tell a key problem (refuse the call) from an outage (carry on without it).
    """

    ok: bool
    message: str
    model: str = ""
    cost: float | None = None
    error: TypeSafeError | None = None


@dataclass(frozen=True)
class _Reply:
    answers: dict[str, Answer]
    model: str
    cost: float | None


# ── the client ──────────────────────────────────────────────────────────────


class TypeSafeClient:
    """The one way this app asks the TypeSafe Classifier anything.

    Built once, on app.state, with *getters* for the key and model rather than
    the values: a key saved in Settings then applies to the next question with
    no restart, and the concurrency ceiling is genuinely shared by every caller
    instead of being one per short-lived client.
    """

    def __init__(self, key_getter: Callable[[], str], model_getter: Callable[[], str]) -> None:
        self._key_getter = key_getter
        self._model_getter = model_getter
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop: asyncio.AbstractEventLoop | None = None

    # -- public API --

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]], *,
                  attempts: int | None = None, timeout: float | None = None,
                  ) -> dict[str, Answer]:
        """Ask one or more named questions about `state`; answers keyed the same way.

        `state` is text, or a dict/list of text (a card's fields, say) — JSON is
        what goes over the wire. Questions are the API's own shape, e.g.
        `{"team": {"type": "choice", "instructions": "...", "criteria": {...}}}`,
        so a new question type needs nothing from this module.

        `attempts` and `timeout` (seconds per attempt) override the client's
        budget for this one call — for a caller that would rather have no answer
        soon than an answer after two minutes of retries (a best-effort check).
        """
        key, model = self._resolve(None, None)
        return (await self._call(key, model, state, questions,
                                 attempts=attempts, timeout=timeout)).answers

    async def choice(self, state: Any, instructions: str, criteria: dict[str, str], *,
                     attempts: int | None = None, timeout: float | None = None) -> Choice:
        """Pick one of `criteria` (option name → what it means) for `state`."""
        if not criteria:
            raise ValueError("a choice question needs at least one option")
        if len(criteria) > _MAX_CHOICE_OPTIONS:
            raise ValueError(f"a choice question takes at most {_MAX_CHOICE_OPTIONS} "
                             f"options, got {len(criteria)}")
        question = {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}
        answer = (await self.ask(state, {"q": question}, attempts=attempts, timeout=timeout))["q"]
        if not isinstance(answer, Choice):
            raise TypeSafeError(
                "The TypeSafe Classifier answered a choice question with something else."
            )
        return answer

    async def noul(self, state: Any, instructions: str, *, attempts: int | None = None,
                   timeout: float | None = None) -> float:
        """Probability (0–1) that `instructions` — a statement — holds for `state`."""
        answer = (await self.ask(state, {"q": {"type": "noul", "instructions": instructions}},
                                 attempts=attempts, timeout=timeout))["q"]
        if not isinstance(answer, Noul):
            raise TypeSafeError(
                "The TypeSafe Classifier answered a yes/no question with something else."
            )
        return answer.probability

    async def check(self, key: str | None = None, model: str | None = None, *,
                    attempts: int = CHECK_ATTEMPTS,
                    timeout: float = CHECK_TIMEOUT_SEC) -> TypeSafeCheck:
        """Ask one tiny question and report whether it was answered. Never raises.

        `key`/`model` test a candidate before it is saved (the Settings page
        tests what was typed, so a typo cannot replace a key that works); left
        out, the saved ones are used. What is being checked is the whole path —
        the key, the credits behind it, and the model name — which is why it
        asks a real question rather than calling an account endpoint. One
        attempt with a short timeout by default (`CHECK_ATTEMPTS`,
        `CHECK_TIMEOUT_SEC`): a check is asked while someone waits.
        """
        started = time.monotonic()
        try:
            key, model = self._resolve(key, model)
            reply = await self._call(
                key, model, "Hello, is this thing on?",
                {"check": {"type": "noul", "instructions": "The text is a greeting"}},
                attempts=attempts, timeout=timeout,
            )
        except TypeSafeError as exc:
            return TypeSafeCheck(ok=False, message=str(exc), model=model or "", error=exc)
        ms = (time.monotonic() - started) * 1000
        return TypeSafeCheck(
            ok=True,
            message=f"Working: {reply.model or model} answered in {ms:.0f} ms.",
            model=reply.model or model,
            cost=reply.cost,
        )

    # -- plumbing --

    def _resolve(self, key: str | None, model: str | None) -> tuple[str, str]:
        key = (self._key_getter() if key is None else key) or ""
        model = (self._model_getter() if model is None else model) or ""
        key, model = key.strip(), model.strip() or DEFAULT_MODEL
        if not key:
            raise TypeSafeNotConfigured(
                "No OpenRouter API key is saved for the TypeSafe Classifier (e.g. Jev). "
                f"Add one under {_SETTINGS_PATH}."
            )
        return key, model

    def _semaphore(self) -> asyncio.Semaphore:
        """The shared ceiling, made in (and for) the loop that is running now.

        An asyncio.Semaphore belongs to the first loop that waits on it, and this
        client outlives loops: it sits on app.state, and every TestClient (and
        any script that calls asyncio.run twice) brings a new one. A semaphore
        from a dead loop fails with "bound to a different event loop" the first
        time it is contended, so a new loop gets a new one.
        """
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(TYPESAFE_PARALLEL)
            self._sem_loop = loop
        return self._sem

    async def _call(self, key: str, model: str, state: Any,
                    questions: dict[str, dict[str, Any]], *, attempts: int | None = None,
                    timeout: float | None = None) -> _Reply:
        if not questions:
            raise ValueError("ask at least one question")
        attempts = max(1, int(attempts)) if attempts is not None else _MAX_ATTEMPTS
        timeout = float(timeout) if timeout is not None else _TIMEOUT_SEC
        body = {"model": model, "state": state, "questions": questions}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        sem = self._semaphore()
        last_error = "no attempt was made"

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, attempts + 1):
                delay = _BACKOFF_SEC * 2 ** (attempt - 1)
                try:
                    async with sem:
                        resp = await client.post(API, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    # Only the class and httpx's own sentence: the exception object
                    # carries the request, and the request carries the key.
                    last_error = f"{type(exc).__name__}: {_scrub(str(exc), key) or 'no detail'}"
                else:
                    status = resp.status_code
                    if status == 429 or status == 408 or status >= 500:
                        last_error = f"HTTP {status}"
                        if status == 429:
                            delay = _retry_after(resp.headers.get("Retry-After"), delay)
                    else:
                        return self._decode(resp, key, model, questions)

                if attempt < attempts:
                    logger.warning(
                        "typesafe: %s; retrying in %.1fs (attempt %d/%d)",
                        last_error, delay, attempt, attempts,
                    )
                    await _sleep(delay)

        tried = "after 1 attempt" if attempts == 1 else f"after {attempts} attempts"
        raise TypeSafeUnavailable(
            f"The TypeSafe Classifier did not answer {tried} ({last_error}). OpenRouter or "
            f"TypeSafe may be having trouble; try again in a few minutes."
        )

    def _decode(self, resp: httpx.Response, key: str, model: str,
                questions: dict[str, dict[str, Any]]) -> _Reply:
        try:
            payload = resp.json()
        except ValueError:
            payload = None

        # OpenRouter can report a failure inside a 200 as well as with a status,
        # so the error object is honoured wherever it appears.
        err = payload.get("error") if isinstance(payload, dict) else None
        if resp.status_code != 200 or err is not None:
            code = resp.status_code
            detail = ""
            if isinstance(err, dict):
                detail = str(err.get("message") or "")
                if resp.status_code == 200 and isinstance(err.get("code"), int):
                    code = err["code"]
            elif isinstance(err, str):
                detail = err
            raise _error_for(code, _scrub(detail, key))
        if not isinstance(payload, dict):
            raise TypeSafeError("The TypeSafe Classifier sent a reply this app cannot read.")

        answers_raw = payload.get("answers")
        if not isinstance(answers_raw, dict):
            raise TypeSafeError("The TypeSafe Classifier replied without any answers.")
        answered_by = str(payload.get("model") or model)
        answers: dict[str, Answer] = {}
        for name in questions:
            raw = answers_raw.get(name)
            if not isinstance(raw, dict):
                raise TypeSafeError(f"The TypeSafe Classifier returned no answer for {name!r}.")
            answers[name] = _answer(name, raw, answered_by)

        usage = payload.get("usage")
        cost = None
        if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
            cost = float(usage["cost"])
        return _Reply(answers=answers, model=answered_by, cost=cost)


# ── helpers ─────────────────────────────────────────────────────────────────


def _answer(name: str, raw: dict[str, Any], model: str) -> Answer:
    kind = str(raw.get("type") or "")
    try:
        if kind == "choice":
            probabilities = {str(k): float(v) for k, v in (raw.get("probabilities") or {}).items()}
            picked = str(raw["choice"])
            confidence = raw.get("confidence")
            return Choice(
                choice=picked,
                probabilities=probabilities,
                confidence=float(confidence if confidence is not None
                                 else probabilities.get(picked, 0.0)),
                model=model,
            )
        if kind == "noul":
            return Noul(probability=float(raw["noul"]), model=model)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TypeSafeError(
            f"The TypeSafe Classifier's answer to {name!r} was not in a shape this app can read."
        ) from None
    return RawAnswer(type=kind, data=dict(raw), model=model)


def _error_for(status: int, detail: str) -> TypeSafeError:
    """The exception a person can act on, for a status that retrying cannot fix."""
    said = f" (OpenRouter said: {detail})" if detail else ""
    if status in (401, 403):
        return TypeSafeAuthError(
            f"OpenRouter rejected the key (HTTP {status}). Check it was copied whole from "
            f"openrouter.ai/settings/keys and has not been disabled, then save it again "
            f"under {_SETTINGS_PATH}.{said}"
        )
    if status == 402:
        return TypeSafeCreditError(
            "The key works, but the OpenRouter account is out of credits (HTTP 402). Add "
            f"credits at openrouter.ai/settings/credits, then try again.{said}"
        )
    if status in (408, 429) or status >= 500:
        # Only reachable as an error inside a 200 — a real status like these is
        # retried in _call — and it is the same kind of trouble.
        return TypeSafeUnavailable(
            f"The TypeSafe Classifier could not answer (code {status}); try again in a "
            f"few minutes.{said}"
        )
    # The upstream reason goes inside the first sentence, which is the part the
    # Settings page keeps as the last check's summary.
    hint = ""
    if "model" in detail.lower():
        hint = f" Check the Model under {_SETTINGS_PATH} (the default is {DEFAULT_MODEL})."
    reason = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
    return TypeSafeError(f"The TypeSafe Classifier refused this request ({reason}).{hint}")


def _retry_after(value: str | None, fallback: float) -> float:
    """Seconds to wait, from a Retry-After header, or `fallback`.

    The header is either delay-seconds or an HTTP date (RFC 9110 allows both),
    and it comes from a server we do not control, so anything unparseable — or
    absurd — falls back to our own backoff rather than raising mid-retry.
    """
    if not value:
        return fallback
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return fallback
        if when is None:
            return fallback
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds != seconds:  # NaN
        return fallback
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SEC)


# Anything that looks like a credential, whether or not it is the key we sent:
# OpenRouter's own "sk-or-…" format and any bearer token echoed back.
_KEYLIKE = re.compile(r"(sk-or-[\w-]+|Bearer\s+\S+)", re.IGNORECASE)


def _scrub(text: str, key: str) -> str:
    """Upstream text made safe to show: no key, no key-shaped strings, one line, short."""
    if not text:
        return ""
    if key:
        text = text.replace(key, "***")
    text = _KEYLIKE.sub("***", text)
    text = " ".join(text.split())
    if len(text) > _ERROR_TEXT_LIMIT:
        text = text[: _ERROR_TEXT_LIMIT - 1].rstrip() + "…"
    return text
