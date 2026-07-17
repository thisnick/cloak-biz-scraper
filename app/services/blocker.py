"""Recognising an anti-bot interstitial.

Ported from browserd (app/tasks/blocker.py). A block is not an error: the page
loaded fine, it simply is not the page we asked for. Telling the two apart is
what makes the retry path meaningful — a block means the exit IP got flagged and
a fresh one is the fix, whereas a real error means retrying with a new IP is
pointless.
"""
from __future__ import annotations

import re

_BLOCK_MARKERS = re.compile(
    r"(access denied|pardon our interruption|verify you are a human|"
    r"are you a robot|unusual traffic|request unblock|attention required|"
    r"checking your browser|cf-browser-verification|px-captcha|just a moment|"
    r"enable javascript and cookies to continue)",
    re.IGNORECASE,
)


def text_contains_blocker(*parts: str | None) -> bool:
    """True if any fragment (body text, title) looks like a block or challenge page."""
    for p in parts:
        if p and _BLOCK_MARKERS.search(p):
            return True
    return False
