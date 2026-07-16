"""What we tell a user whose pinned version will not download.

Separate from test_instances.py because these are sync and that module marks
everything asyncio.

These assert the *causal contract*, not the wording. An earlier version of this
file checked only that certain phrases were present, which let a confidently
false explanation ship: it blamed the machine's architecture, when probing the
download API shows a withdrawn version 404s on every architecture alike. A
message can be well-worded and still be a lie, so what follows constrains what we
are allowed to claim.
"""
from __future__ import annotations

import pytest

from app.services.instances import _diagnose_pin

PIN = "146.0.7680.177.5"

# What the package actually raises. The last sentence is the problem: the
# condition is permanent, so "retry in a moment" is advice that never pays off.
NOT_FOUND = RuntimeError(
    "Pro binary unavailable: Client error '404 Not Found' for url "
    "'https://cloakbrowser.dev/api/download/146.0.7680.177.5'. Your license is "
    "valid but the Pro binary could not be downloaded right now. Retry in a moment."
)


def _tagged(monkeypatch, tag: str) -> str:
    monkeypatch.setattr("cloakbrowser.config.get_platform_tag", lambda: tag)
    return _diagnose_pin(NOT_FOUND, PIN)


def test_identifies_the_pin_as_the_subject(monkeypatch):
    assert PIN in _tagged(monkeypatch, "linux-arm64")


def test_contradicts_the_packages_retry_advice(monkeypatch):
    # The entire reason this function exists.
    assert "retrying will not help" in _tagged(monkeypatch, "linux-arm64")


def test_offers_a_way_out(monkeypatch):
    assert "Clear the pin" in _tagged(monkeypatch, "linux-arm64")


def test_preserves_the_underlying_error(monkeypatch):
    # Never swallow the original: it carries the URL and the status code.
    assert "404" in _tagged(monkeypatch, "linux-arm64")


def test_the_tag_is_the_only_arch_varying_token(monkeypatch):
    """A weak invariant, and worth being honest about how weak.

    It proves only that nothing *besides* the tag string changes with the
    platform. The false version this file exists to prevent passed this check
    too — interpolating the tag into a sentence that blames the tag still varies
    by exactly one token. The real guard is the forbidden-claims test below;
    this one just stops a second, differently-worded arch branch creeping in.
    """
    arm = _tagged(monkeypatch, "linux-arm64")
    x64 = _tagged(monkeypatch, "linux-x64")
    assert arm.replace("linux-arm64", "<TAG>") == x64.replace("linux-x64", "<TAG>")


def test_platform_is_context_not_a_verdict(monkeypatch):
    """This is the test that catches the regression.

    It is an assertion about wording, which is usually a smell — but the claim a
    message makes *is* its wording, and the failure mode here was a message that
    read well while asserting something the download API contradicts. So the
    constraint is negative: these are the things a 404 does not license us to say.
    """
    msg = _tagged(monkeypatch, "linux-arm64")
    assert "linux-arm64" in msg, "the tag is useful context for a bug report"
    lowered = msg.lower()
    for false_claim in (
        "no build for this machine",
        "has no build",
        "architecture",
        "counterpart",
        "per-platform",
        "exists for linux",
    ):
        assert false_claim not in lowered, f"asserts an unproven cause: {false_claim!r}"


def test_names_no_cause_it_cannot_know(monkeypatch):
    """A 404 cannot distinguish retired from mistyped from never-existed."""
    msg = _tagged(monkeypatch, "linux-arm64").lower()
    assert "is not available for download" in msg  # observed, not inferred
    # "withdrawn once superseded" is offered as something to check, not asserted
    # as the diagnosis — so the message must not declare this pin retired.
    assert "has been withdrawn" not in msg
    assert "was retired" not in msg


class TestStaysSilent:
    """Only speak when a pin is genuinely implicated."""

    def test_when_unpinned(self):
        # Unpinned, a 404 is something else entirely — don't invent a pin problem.
        assert _diagnose_pin(NOT_FOUND, "") is None

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("connection reset by peer"),
            RuntimeError("Pro binary unavailable: 503 Service Unavailable"),
            RuntimeError("License validation failed"),
            TimeoutError("read timed out"),
        ],
    )
    def test_for_failures_that_may_be_transient(self, exc):
        # These really might clear on a retry; overriding that advice would be
        # its own false claim.
        assert _diagnose_pin(exc, PIN) is None
