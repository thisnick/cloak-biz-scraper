"""BizBuySell search-results pages.

Ported from browserd (app/tasks/bizbuysell.py): the card-extraction JS, the
`/N/` path-segment paging, and the listing-id rules. The broker-profile half of
that module is deliberately left behind — brokers are out of scope for v1.

Two changes from the port, both required by this project's contract:

* **Money comes back verbatim.** browserd ran the card text through a
  `/\\$[\\d,]+|Not Disclosed/` regex, which turns "$81,000 + Inventory" into
  "$81,000" — a different price, invented at the point of extraction and
  unrecoverable afterwards. Here the card's own text is kept as-is and the
  decision about what it means as a number belongs to the store.
* **Each card carries an `excerpt`.** It is rendered from the card's own HTML
  through the shared markdown pipeline (`services/extract.py`), so no detail
  page is fetched to build it — 50 cards would otherwise mean 50 page loads.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from ..models import Listing
from ..services import extract
from .base import CardPage
from .urls import canonical_url, normalize_url

HOST = "www.bizbuysell.com"
WARMUP_URL = "https://www.bizbuysell.com/"

# A search-results path always contains a "…businesses-for-sale" segment:
#   /california/san-francisco-bay-area-businesses-for-sale/
#   /sacramento-area-businesses-for-sale/2/
# A listing's own page (/business-opportunity/<slug>/<id>/) and a broker profile
# never do, which is what keeps them out — they are different jobs, and one of
# them (archive_page) already exists.
_SERP_PATH = re.compile(r"/[^/]*businesses-for-sale(/|$)", re.IGNORECASE)
_LISTING_ID = re.compile(r"/business-opportunity/[^/]+/(\d+)/?$", re.IGNORECASE)


def listing_id_from(url: str | None) -> str | None:
    """BizBuySell's numeric listing id, from a listing URL or a ?q= profile link."""
    raw = (url or "").strip()
    if not raw:
        return None
    p = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (p.hostname or "").lower()
    if not host.endswith("bizbuysell.com"):
        return None
    q = parse_qs(p.query).get("q", [None])[0]
    if q and q.isdigit():
        return q
    m = _LISTING_ID.search(p.path)
    return m.group(1) if m else None


# Cards are anchors wrapping the whole tile. Everything read here is read as the
# card presents it: the money fields keep their own text, and `excerpt` is the
# card rendered through the same markdown rules the archive path uses.
JS_CARDS = r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const text = (root, sel) => { const e = root.querySelector(sel); return e ? clean(e.textContent) : null; };
  // Cards label their figures ("Asking Price: $1,258,000"). The label is the
  // card's furniture, not part of the amount; everything after it is kept
  // exactly as written, including any "+ Inventory" or "Not Disclosed".
  const LABEL = /^\s*(asking price|price|cash flow|cashflow|sde|ebitda|revenue|gross revenue|gross income)\s*:?\s*/i;
  const value = (s) => { const v = clean(s).replace(LABEL, '').trim(); return v || null; };
  const after = (raw, re) => { const m = (raw || '').match(re); return m ? value(m[0]) : null; };
  const listingId = (href, id) => {
    if (id && /^\d+$/.test(id)) return id;
    const m = (href || '').match(/\/business-opportunity\/[^/]+\/(\d+)\/?/i);
    return m ? m[1] : null;
  };

  const cards = []; const seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href*="/business-opportunity/"]'))) {
    const href = a.href; if (!href || seen.has(href)) continue; seen.add(href);
    const raw = clean(a.innerText || a.textContent);
    const asking = value(text(a, 'p.asking-price:not(.hide-on-desktop)') || text(a, 'p.asking-price'))
                || after(raw, /Asking(?: Price)?:\s*[^\n]+/i);
    const cashflow = value(text(a, '.cash-flow') || text(a, '.cash-flow-on-mobile'))
                  || after(raw, /Cash Flow:\s*[^\n]+/i);
    const ebitda = after(raw, /EBITDA:\s*[^\n]+/i);
    const revenue = after(raw, /(?:Gross )?Revenue:\s*[^\n]+/i);
    cards.push({
      listing_id: listingId(href, a.id),
      url: href,
      title: text(a, '.title') || clean(a.getAttribute('aria-label')) || null,
      location: text(a, '.location'),
      asking_price: asking,
      cashflow: cashflow,
      ebitda: ebitda,
      revenue: revenue,
      // The card's own content, as markdown, via the same rules the archive
      // path uses. No detail page is fetched for this.
      excerpt: window.__cbsMarkdown(a.innerHTML, ['.hide-on-desktop']).trim(),
    });
  }
  const bodyText = clean(document.body ? document.body.innerText : '');
  const blocked = /Access Denied|Pardon Our Interruption|verify you are a human/i.test(bodyText);
  return JSON.stringify({ title: document.title || '', url: location.href, blocked, cards });
})()
"""


class BizBuySellSerp:
    """The BizBuySell search-results adapter."""

    name = "bizbuysell_serp"
    describes = "BizBuySell search results (bizbuysell.com … businesses-for-sale)"
    example = "https://www.bizbuysell.com/california/san-francisco-bay-area-businesses-for-sale/"
    warmup_url = WARMUP_URL

    def matches(self, url: str) -> bool:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        if not (host == "bizbuysell.com" or host.endswith(".bizbuysell.com")):
            return False
        return bool(_SERP_PATH.search(p.path or ""))

    def page_url(self, url: str, page: int) -> str:
        """BizBuySell pages a search with a bare `/N/` segment on the path.

        Page 1 has no segment, so the trailing number is stripped first — which
        also makes this idempotent when handed a URL that is already page 3.
        """
        p = urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        if parts and parts[-1].isdigit():
            parts = parts[:-1]
        if page > 1:
            parts.append(str(page))
        path = "/" + "/".join(parts) + "/"
        return urlunparse((p.scheme or "https", p.netloc or HOST, path, "", p.query, ""))

    async def cards(self, page) -> CardPage:
        await extract.inject(page)
        raw = await page.evaluate(JS_CARDS)
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return CardPage(listings=[], blocked=False)

        listings: list[Listing] = []
        for c in data.get("cards", []):
            url = canonical_url(c.get("url") or "")
            if not url:
                continue
            cashflow = c.get("cashflow") or ""
            # The .cash-flow element sometimes holds the EBITDA figure instead;
            # recording it as cash flow would file one number under two names.
            if cashflow.lower().startswith("ebitda"):
                cashflow = ""
            listings.append(
                Listing(
                    listing_id=c.get("listing_id") or listing_id_from(url) or "",
                    url=url,
                    normalized_url=normalize_url(url) or "",
                    title=c.get("title") or "",
                    location=c.get("location") or "",
                    asking_price=c.get("asking_price") or "",
                    revenue=c.get("revenue") or "",
                    cashflow=cashflow,
                    ebitda=c.get("ebitda") or "",
                    excerpt=c.get("excerpt") or "",
                    source=self.name,
                )
            )
        return CardPage(
            listings=listings, blocked=bool(data.get("blocked")), title=data.get("title") or ""
        )
