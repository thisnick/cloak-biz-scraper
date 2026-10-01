"""The source registry: URL in, adapter out.

Two kinds of source read a page. A site adapter (`SOURCES`) knows its site's
markup and is chosen by URL pattern. Every other site is read by the generic
reader (`generic.GenericSource`), which finds the list on the page by its links
and asks the Decision API what code cannot see. The sweep
(`services/scrape.py`) makes that choice, because only it knows whether a
classifier key is saved; this module answers
the two questions it asks first: does an adapter read this URL (`for_url`), and
does an adapter own this site (`owner_of`)?
"""
from __future__ import annotations

from urllib.parse import urlparse

from .base import CardPage, PageNotReached, Source, UnsupportedURL
from .bizbuysell import BizBuySellBroker, BizBuySellSerp

# The site adapters: BizBuySell, via two — the region search feed and a
# broker's own profile. Their URL paths are disjoint (`businesses-for-sale` vs
# `business-broker`), so order is not load-bearing — `for_url` still returns
# exactly one. The list is the single place a site adapter is added, and the
# error message is generated from it, so a new adapter cannot be added without
# the "what is supported?" answer following it. The generic reader is not in
# it: it is built per swept URL, and it is not a site that can be named here.
SOURCES: list[Source] = [BizBuySellSerp(), BizBuySellBroker()]

# The generic reader's source id and label, here so `label_for` can answer for
# it without importing the reader (and, through it, the classifier client).
GENERIC_NAME = "generic"
GENERIC_LABEL = "Any site"


def for_url(url: str) -> Source:
    """The site adapter for this URL, or raise `UnsupportedURL` naming what is supported."""
    for source in SOURCES:
        if source.matches(url):
            return source
    raise UnsupportedURL(url, SOURCES)


def supported(url: str) -> bool:
    return any(s.matches(url) for s in SOURCES)


def owner_of(url: str) -> Source | None:
    """The site adapter that owns this URL's host, whether or not it reads the URL.

    The generic reader is for sites nobody has written an adapter for. A page on
    a site that has one, but that the adapter does not match — a BizBuySell
    listing's own page — is a page the adapter chose not to read, and handing
    it to the generic reader would sweep a "similar listings" rail as if it
    were a search.
    """
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for source in SOURCES:
        for owned in getattr(source, "hosts", ()):
            if host == owned or host.endswith("." + owned):
                return source
    return None


def label_for(name: str) -> str:
    """The human display label for a source id (a Listing/Job `source`).

    Resolved from the adapter that owns that `name`, so a new source's label
    lives with the source and nothing here has to be updated. Falls back to the
    raw `name` when no adapter matches — an old job whose source was retired must
    still render *something* rather than break the page. (A generically read
    listing's `source` is its site, e.g. "websiteclosers.com", which is its own
    best label.)
    """
    if name == GENERIC_NAME:
        return GENERIC_LABEL
    for source in SOURCES:
        if source.name == name:
            return source.label
    return name


__all__ = [
    "CardPage", "PageNotReached", "Source", "SOURCES", "UnsupportedURL", "GENERIC_NAME",
    "GENERIC_LABEL", "for_url", "supported", "owner_of", "label_for",
]
