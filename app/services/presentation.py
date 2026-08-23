"""Turning machine facts into things a non-technical user can act on.

The `cloakbrowser` package raises errors written for the person who installed it
from a terminal. Our user deployed a button on Railway and has no shell. Where
that gap makes advice actively wrong, we fix it *here* — at the edge, where the
audience is known — and never by editing the diagnostics themselves. Those are
correct for their own audience, and rewriting them in place would degrade the
logs, which a maintainer does read.

Byte counts live here for the same reason: "how big is that" is a presentation
question with one right answer for this audience, and it was being answered in
three places at once — a service's refusals, the settings banners, and the
dashboard's own JavaScript. Two of those are Python and now share this; the
third cannot import it and is written to match, which is noted where it lives.
"""
from __future__ import annotations

import re

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(count: int) -> str:
    """Bytes as a person reads them: `747 MB`, `1.4 GB`, `64 B`.

    One decimal below ten and none above it, because "1.4 GB" is a number
    somebody can act on and "1.43 GB" is noise. A trailing ".0" is dropped —
    "1 KB", not "1.0 KB" — which is not only taste: the dashboard's own
    JavaScript copy has always rounded that way, and a test that runs both found
    the two surfaces disagreeing about every exact power of 1024. One of them
    had to move, and this is the reading a person would rather see.

    Used both in banners a human reads and in refusals a model reads — they want
    the same thing, and two formatters that agree today are two that disagree
    after the next edit.
    """
    value = float(count)
    for unit in _UNITS:
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            if value >= 10:
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{value:.0f} TB"  # pragma: no cover - unreachable, the loop returns

# From cloakbrowser/download.py:180 — appended to every "Pro binary could not be
# downloaded" RuntimeError:
#
#   "... Retry in a moment. To use the free binary instead, unset
#    CLOAKBROWSER_LICENSE_KEY."
#
# Wrong for this app, which is why it is worth intercepting rather than
# tolerating:
#   1. There is no terminal to unset a variable from — Railway's user has a web
#      form and nothing else.
#   2. The variable is not set anyway. config.purge_binary_env() removes it at
#      boot so a stale deploy-time value can never outrank the licence in the
#      settings store, so "unset it" is already done and changes nothing.
#   3. Public mode is a deliberate settings choice. A user who supplied a key
#      asked for Pro, so quietly unsetting it would recreate the exact silent
#      downgrade this app refuses.
#
# So a user who reads it goes hunting for a variable they cannot see, to enable a
# fallback we do not implement, on a machine they cannot log into.
_FREE_BINARY_ADVICE = re.compile(
    r"\s*To use the free binary instead,\s*unset\s+CLOAKBROWSER_LICENSE_KEY\.?",
    re.IGNORECASE,
)

_ACTIONABLE = (
    " This saved key will not be silently switched to the public build. Check the "
    "licence key in Settings is the one from your CloakBrowser account and has not "
    "expired; if it is correct, this is usually temporary — wait a moment and verify "
    "again."
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
