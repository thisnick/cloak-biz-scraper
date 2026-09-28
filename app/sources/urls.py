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
from typing import Iterable
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlparse, urlunparse

# Query parameters that say how someone arrived, never which listing they are
# looking at. Matched case-insensitively; `utm_*` is matched as a prefix.
_TRACKING = frozenset({
    "gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid", "twclid",
    "igshid", "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "mkt_tok",
    "ref", "ref_src",
})


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith("utm_") or key in _TRACKING


def canonical_url(url: str) -> str:
    """The listing's address as we store and re-visit it: scheme + host + path.

    Query and fragment go because they carry the session's baggage (utm tags, a
    scroll anchor) rather than the listing's identity.
    """
    p = urlparse(url)
    return urlunparse((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/") + "/", "", "", ""))


def normalize_url(url: str | None, keep_query: Iterable[str] | None = None) -> str | None:
    """Canonical dedupe shape: host[:port]/path — no scheme, query, or fragment.

    Everything dropped here is something that can differ between two sightings of
    one listing: http vs https, a www prefix, a tracking parameter, a trailing
    slash. What remains is the part that identifies it.

    `keep_query` is for sites where the query IS the identity
    (`listing.php?LID=5` and `?LID=6` are two listings). The named keys are
    kept, sorted, after the path; every other parameter is still dropped, so a
    tracking tag or a sort order cannot split one listing into two rows. Left
    out (None), the result is exactly what it always was — this is a key shared
    with rows already written, and with another codebase.
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
    base = f"{host}{port}{path}"
    if keep_query is None:
        return base
    keys = set(keep_query)
    kept = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k in keys)
    return f"{base}?{urlencode(kept)}" if kept else base


def listing_url(href: str | None) -> str | None:
    """The address to store for a listing found by following a link on a page.

    Unlike `canonical_url`, the query stays: on many sites it is the only thing
    naming the listing (`listing.php?LID=5`), and dropping it stores a link
    that opens the wrong page, or none. What goes is what cannot be the
    listing — the fragment, and tracking parameters (`utm_*`, `gclid`, `ref`,
    …) — so a link clicked from a newsletter and the same link on the results
    page store the same address. The remaining parameters are kept exactly as
    the site wrote them (order and encoding), because this is a URL to open,
    not a key to compare; `normalize_url` is the key.

    None for anything that is not an absolute http(s) link.
    """
    raw = (href or "").strip()
    if not raw:
        return None
    p = urlparse(raw)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    parts = [part for part in p.query.split("&") if part]
    query = "&".join(
        part for part in parts if not _is_tracking(unquote_plus(part.split("=", 1)[0]))
    )
    return urlunparse((p.scheme.lower(), p.netloc, p.path, p.params, query, ""))
