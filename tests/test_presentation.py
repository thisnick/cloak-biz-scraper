"""Carry-forward #2: the package's error tail offers advice our user cannot act
on, for a fallback we do not implement, via a variable we already removed."""
from __future__ import annotations

from app.services.presentation import humanize_binary_error

# Verbatim from cloakbrowser/download.py:180-184. If a package upgrade rewords
# this, the assertions below start failing and someone re-checks the tail —
# which is the point of pinning the literal string here.
PACKAGE_ERROR = (
    "Pro binary unavailable: HTTP 503. Your license is valid but the Pro binary "
    "could not be downloaded right now. Retry in a moment. To use the free binary "
    "instead, unset CLOAKBROWSER_LICENSE_KEY."
)


def test_the_unactionable_advice_is_removed():
    out = humanize_binary_error(PACKAGE_ERROR)
    assert "unset CLOAKBROWSER_LICENSE_KEY" not in out
    assert "free binary" not in out


def test_the_real_problem_survives():
    # Stripping advice must not strip the diagnosis. The user still needs to know
    # the download failed and that their licence itself was fine.
    out = humanize_binary_error(PACKAGE_ERROR)
    assert "Pro binary unavailable: HTTP 503" in out
    assert "license is valid" in out


def test_something_actionable_replaces_it():
    out = humanize_binary_error(PACKAGE_ERROR)
    assert "Settings" in out, "advice must name a place they can actually go"


def test_messages_without_the_tail_pass_through_untouched():
    pin_diagnosis = (
        "CloakBrowser 146.0.7680.177.5 is not available for download, so this pin "
        "cannot be satisfied and retrying will not help. Clear the pin in Settings."
    )
    assert humanize_binary_error(pin_diagnosis) == pin_diagnosis


def test_tail_stripped_even_when_nested_in_a_pin_diagnosis():
    # _diagnose_pin appends "Underlying error: {exc}" verbatim, so the tail can
    # arrive buried in the middle of an otherwise well-written message.
    nested = (
        "CloakBrowser 1.2.3.4 is not available for download. Clear the pin in "
        f"Settings. (Resolved platform: linux-arm64. Underlying error: {PACKAGE_ERROR})"
    )
    out = humanize_binary_error(nested)
    assert "unset CLOAKBROWSER_LICENSE_KEY" not in out
    assert "Resolved platform: linux-arm64" in out


def test_diagnose_pin_itself_is_untouched():
    """The package's own wording stays intact for the logs; we fix it at the edge.

    Carry-forward #2 says explicitly: don't change _diagnose_pin. This guards
    that boundary — the string it emits is for a maintainer reading logs, and
    only the UI needs the audience-specific rewrite.
    """
    from app.services.instances import _diagnose_pin

    diagnosis = _diagnose_pin(RuntimeError("404 not found"), "148.0.7778.215.5")
    assert "Underlying error: 404 not found" in diagnosis
    assert "Clear the pin in Settings" in diagnosis
