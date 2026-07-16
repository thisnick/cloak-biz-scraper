"""Turning library errors into things a non-technical user can act on.

The `cloakbrowser` package raises errors written for the person who installed it
from a terminal. Our user deployed a button on Railway and has no shell. Where
that gap makes advice actively wrong, we fix it *here* — at the edge, where the
audience is known — and never by editing the diagnostics themselves. Those are
correct for their own audience, and rewriting them in place would degrade the
logs, which a maintainer does read.
"""
from __future__ import annotations

import re

# From cloakbrowser/download.py:180 — appended to every "Pro binary could not be
# downloaded" RuntimeError:
#
#   "... Retry in a moment. To use the free binary instead, unset
#    CLOAKBROWSER_LICENSE_KEY."
#
# Wrong three times over for this app, which is why it is worth intercepting
# rather than tolerating:
#   1. There is no terminal to unset a variable from — Railway's user has a web
#      form and nothing else.
#   2. The variable is not set anyway. config.purge_binary_env() removes it at
#      boot so a stale deploy-time value can never outrank the licence in the
#      settings store, so "unset it" is already done and changes nothing.
#   3. The escape hatch it offers does not exist here. This app launches the Pro
#      binary and only the Pro binary; falling back to the free tier is not a
#      mode we have, so following the advice could not help even if it worked.
#
# So a user who reads it goes hunting for a variable they cannot see, to enable a
# fallback we do not implement, on a machine they cannot log into.
_FREE_BINARY_ADVICE = re.compile(
    r"\s*To use the free binary instead,\s*unset\s+CLOAKBROWSER_LICENSE_KEY\.?",
    re.IGNORECASE,
)

_ACTIONABLE = (
    " This app only runs the licensed Pro browser. Check the licence key in Settings "
    "is the one from your CloakBrowser account and has not expired; if it is correct, "
    "this is usually temporary — wait a moment and verify again."
)


def humanize_binary_error(message: str) -> str:
    """Strip advice our user cannot act on, and say what they can do instead.

    Only rewrites when the offending sentence is actually present, so a message
    that never carried it is passed through untouched — including the pin
    diagnosis from `_diagnose_pin`, which is already written for this audience
    and names a real action ("clear the pin in Settings").
    """
    cleaned, count = _FREE_BINARY_ADVICE.subn("", message)
    if not count:
        return message
    return cleaned.rstrip() + _ACTIONABLE
