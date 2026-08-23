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

import math
import re

# KB upward. Bytes are handled before the loop, exactly as the dashboard's copy
# does, so the two cannot part company at the first boundary.
_UNITS = ("KB", "MB", "GB", "TB")


def human_size(count: int) -> str:
    """Bytes as a person reads them: `747 MB`, `1.4 GB`, `64 B`.

    One decimal below ten and none above it, because "1.4 GB" is a number
    somebody can act on and "1.43 GB" is noise. A trailing ".0" is dropped —
    "1 KB", not "1.0 KB".

    **This is a deliberate transliteration of the dashboard's JavaScript, not an
    independent implementation of the same rule, and the arithmetic is copied
    step for step on purpose.** The two were written to the same description and
    still disagreed: Python's format-string rounding is half-to-even and
    `Math.round` is half-up, so they parted at every `.5` boundary — 10752 bytes
    read `11 KB` in the row and `10 KB` in the banner beneath it. A shared
    *description* is not a shared implementation; only the same operations in
    the same order are. `floor(x + 0.5)` is half-up, and a node-driven test
    compares the two over the boundary cases a hand-written parameter list is
    exactly the kind of thing to miss.

    Used both in banners a human reads and in refusals a model reads — they want
    the same thing, and two formatters that agree today are two that disagree
    after the next edit.
    """
    if count < 1024:
        return f"{int(count)} B"
    value = float(count)
    index = -1
    while True:
        value /= 1024
        index += 1
        if not (value >= 1024 and index < len(_UNITS) - 1):
            break
    rounded = (math.floor(value + 0.5) if value >= 10
               else math.floor(value * 10 + 0.5) / 10)
    text = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
    return f"{text} {_UNITS[index]}"


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
