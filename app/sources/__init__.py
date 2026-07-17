"""The source registry: URL in, adapter out."""
from __future__ import annotations

from .base import CardPage, Source, UnsupportedURL
from .bizbuysell import BizBuySellSerp

# v1 is BizBuySell only. The list is the single place a new site is added, and
# the error message below is generated from it, so a new adapter cannot be added
# without the "what is supported?" answer following it.
SOURCES: list[Source] = [BizBuySellSerp()]


def for_url(url: str) -> Source:
    """The adapter for this URL, or raise `UnsupportedURL` naming what is supported."""
    for source in SOURCES:
        if source.matches(url):
            return source
    raise UnsupportedURL(url, SOURCES)


def supported(url: str) -> bool:
    return any(s.matches(url) for s in SOURCES)


__all__ = ["CardPage", "Source", "SOURCES", "UnsupportedURL", "for_url", "supported"]
