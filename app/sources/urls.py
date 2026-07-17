"""URL shapes: the one the browser visits, and the one dedupe is decided on.

Ported from browserd (app/tasks/bizbuysell.py), where these were kept
byte-for-byte in sync with eta's normalize_listing_url.py so that two separate
codebases writing the same Notion database would agree on what "the same
listing" means.

They stay together here, and apart from any one source, because
`normalized_url` is a **dedupe key** — a store compares it against rows written
by any adapter, possibly months apart. If each source normalized in its own way,
two adapters seeing the same listing would write two rows and nobody would
notice until the database was full of duplicates.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


def canonical_url(url: str) -> str:
    """The listing's address as we store and re-visit it: scheme + host + path.

    Query and fragment go because they carry the session's baggage (utm tags, a
    scroll anchor) rather than the listing's identity.
    """
    p = urlparse(url)
    return urlunparse((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/") + "/", "", "", ""))


def normalize_url(url: str | None) -> str | None:
    """Canonical dedupe shape: host[:port]/path — no scheme, query, or fragment.

    Everything dropped here is something that can differ between two sightings of
    one listing: http vs https, a www prefix, a tracking parameter, a trailing
    slash. What remains is the part that identifies it.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    p = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (p.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    port = ""
    if p.port and not (
        (p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)
    ):
        port = f":{p.port}"
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return f"{host}{port}{path}"
