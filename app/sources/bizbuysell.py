"""BizBuySell listing pages: search results, and broker profiles.

Ported from browserd (app/tasks/bizbuysell.py): the card-extraction JS, the
paging rules, and the listing-id rules. Two adapters live here because
BizBuySell publishes for-sale listings two ways — a region search feed
(`BizBuySellSerp`) and a broker's own profile (`BizBuySellBroker`) — and they
page and lay out their cards differently while landing every listing in the
same `Listing` shape.

Two changes from the search port, both required by this project's contract:

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
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

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
# A broker profile is /business-broker/<agent-slug>/<company-slug>/<id>/. It is
# disjoint from _SERP_PATH — "business-broker" never contains "businesses-for-sale"
# — so a URL matches at most one of the two adapters.
_BROKER_PATH = re.compile(r"^/business-broker/[^/]+/[^/]+/\d+/?$", re.IGNORECASE)


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


# A broker profile lists a broker's own for-sale businesses, laid out unlike the
# search feed: the cards are `.bdProfile*` tiles, and each listing's clean name,
# description, and canonical url are also mirrored in an ld+json Product node
# keyed by the listing id. This reads the Product nodes first and uses them when
# present, falling back to the tile's own text — so a listing survives either the
# structured data or the markup going missing, not needing both.
#
# Money is kept verbatim for the same reason the search port keeps it: browserd's
# broker scraper ran the tile through a `/\$[\d,.]+/` regex, which turns
# "$1,258,000 + Inventory" into "$1,258,000" — a different figure, invented at
# extraction. Here the tile's asking-price element is kept as written, with only
# the "Asking Price:" label furniture stripped.
JS_BROKER = r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const LABEL = /^\s*(asking price|asking|price)\s*:?\s*/i;
  const value = (s) => { const v = clean(s).replace(LABEL, '').trim(); return v || null; };
  const locationRe = /\b(?:[A-Z][A-Za-z .'-]+,\s*(?:CA|California)|California|Relocatable|United States)\b/;

  // ld+json Product nodes carry the clean name/description/url per listing,
  // keyed by productid (== the BizBuySell listing id).
  const productsById = new Map();
  const visit = (n) => {
    if (!n || typeof n !== 'object') return;
    if (Array.isArray(n)) { for (const i of n) visit(i); return; }
    const t = Array.isArray(n['@type']) ? n['@type'] : [n['@type']];
    if (t.includes('Product') && n.productid) productsById.set(String(n.productid), n);
    for (const v of Object.values(n)) visit(v);
  };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { visit(JSON.parse(s.textContent || '')); } catch (_) {}
  }

  const anchors = [...document.querySelectorAll([
    'a[href*="/business-opportunity/"]',
    'a[href*="/listings/Profile/?q="]',
    'a.bdProfileMyListingsListingLink',
  ].join(','))];
  const seen = new Set();
  const cards = [];
  for (const a of anchors) {
    const rawHref = new URL(a.getAttribute('href'), window.location.href);
    const listingId = rawHref.searchParams.get('q')
      || (rawHref.pathname.match(/\/(\d+)\/?$/) || [null, null])[1];
    const product = listingId ? productsById.get(String(listingId)) : null;
    const href = product?.url || rawHref.href.split('?')[0];
    const key = listingId || href;
    if (seen.has(key)) continue; seen.add(key);

    // The card is the nearest wrapper big enough to be a listing tile; climb a
    // few parents when the markup has no obvious container.
    let card = a.closest('article, li, [class*="card"], [class*="listing"], [data-testid*="listing"]');
    if (!card) {
      let node = a.parentElement;
      for (let i = 0; node && i < 5; i++, node = node.parentElement) {
        if (clean(node.innerText).length > 120) { card = node; break; }
      }
    }
    const raw = clean((card || a).innerText);
    if (!raw || raw.length < 20) continue;

    const cardTitle = clean(card && card.querySelector('.bdProfileFiguresBusiness, h3, h2, h1') ? card.querySelector('.bdProfileFiguresBusiness, h3, h2, h1').innerText : '');
    const cardLocation = clean(card && card.querySelector('.bdProfileFiguresLocation') ? card.querySelector('.bdProfileFiguresLocation').innerText : '');
    const cardPrice = clean(card && card.querySelector('.bdProfileFiguresAsking') ? card.querySelector('.bdProfileFiguresAsking').innerText : '');
    const cardDesc = clean(card && card.querySelector('.bdProfileMyListingsData_summary') ? card.querySelector('.bdProfileMyListingsData_summary').innerText : '');
    const anchorHeading = a.querySelector('h3,h2,h1') ? a.querySelector('h3,h2,h1').innerText : a.innerText;
    const heading = clean((product && product.name) || cardTitle || anchorHeading) || null;

    cards.push({
      listing_id: listingId,
      url: href,
      title: (heading ? heading.replace(/\s+-\s+BizBuySell$/, '') : null) || null,
      location: cardLocation || (raw.match(locationRe) || [null])[0],
      asking_price: value(cardPrice),
      description: clean((product && product.description) || cardDesc || raw).slice(0, 1200),
    });
  }
  const bodyText = clean(document.body ? document.body.innerText : '');
  const blocked = /Access Denied|Pardon Our Interruption|verify you are a human/i.test(bodyText);
  return JSON.stringify({ title: document.title || '', url: location.href, blocked, cards });
})()
"""


async def _click_for_sale(page) -> bool:
    """Open the For-Sale tab when the bp_cfspg URL did not land on it.

    Ported from browserd's broker scraper. Deliberately broad — a link, a tab, a
    button, or a bare "Businesses For Sale" heading — because the control that
    reveals the for-sale listings is rendered differently across profiles, and a
    profile whose listings never rendered looks exactly like a profile with none.
    """
    for loc in (
        page.get_by_role("link", name=re.compile(r"for sale", re.I)),
        page.get_by_role("tab", name=re.compile(r"for sale", re.I)),
        page.get_by_role("button", name=re.compile(r"for sale", re.I)),
        page.get_by_text(re.compile(r"^\s*(businesses )?for sale\b", re.I)),
    ):
        try:
            if await loc.count():
                await loc.first.click(timeout=5000)
                await page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    return False


class BizBuySellBroker:
    """The BizBuySell broker-profile adapter.

    Same site and same `Listing` shape as `BizBuySellSerp`, but a different page:
    a broker's profile, whose For-Sale tab is paged by a `bp_cfspg` query param
    rather than a path segment, and whose cards are the profile's own tiles.
    """

    name = "bizbuysell_broker"
    describes = "BizBuySell broker profile (bizbuysell.com/business-broker/…)"
    example = "https://www.bizbuysell.com/business-broker/murali-barathi/krea-business/41243/"
    warmup_url = WARMUP_URL

    # BizBuySell serves a broker's For-Sale tab ten cards at a time; the page size
    # travels in the URL alongside the page number.
    _PAGE_SIZE = 10

    def matches(self, url: str) -> bool:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        if not (host == "bizbuysell.com" or host.endswith(".bizbuysell.com")):
            return False
        return bool(_BROKER_PATH.match(p.path or ""))

    def page_url(self, url: str, page: int) -> str:
        """BizBuySell pages a broker's For-Sale tab with a `bp_cfspg` query param.

        The page size (`bplt`) and the `#bdProfileTabs` anchor go alongside it:
        .../business-broker/<slug>/<company>/<id>/?bp_cfspg=2&bplt=10#bdProfileTabs.
        Any existing `bp_cfspg` is dropped first, which makes this idempotent when
        handed a URL that is already a later page.
        """
        p = urlparse(url)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        q.pop("bp_cfspg", None)
        q.setdefault("bplt", str(self._PAGE_SIZE))
        # bp_cfspg first, deterministically, so re-paging a URL that already
        # carried one produces the same string as paging the bare profile did —
        # dict-reinsertion order would otherwise put a re-set param last.
        ordered = {"bp_cfspg": str(page), **q}
        return urlunparse(
            (p.scheme or "https", p.netloc or HOST, p.path, "", urlencode(ordered), "bdProfileTabs")
        )

    async def cards(self, page) -> CardPage:
        data = await self._read(page)
        # A profile's for-sale listings can sit behind a tab the bp_cfspg URL did
        # not activate. When nothing came back and the page is not a block, click
        # through to For-Sale and read once more. Harmless on a genuinely empty
        # later page: any listings it re-surfaces are ones the sweep has already
        # seen, so the sweep still stops.
        if not data.get("blocked") and not data.get("cards"):
            if await _click_for_sale(page):
                data = await self._read(page)

        listings: list[Listing] = []
        for c in data.get("cards", []):
            url = canonical_url(c.get("url") or "")
            if not url:
                continue
            listings.append(
                Listing(
                    listing_id=c.get("listing_id") or listing_id_from(url) or "",
                    url=url,
                    normalized_url=normalize_url(url) or "",
                    title=c.get("title") or "",
                    location=c.get("location") or "",
                    asking_price=c.get("asking_price") or "",
                    excerpt=c.get("description") or "",
                    source=self.name,
                )
            )
        return CardPage(
            listings=listings, blocked=bool(data.get("blocked")), title=data.get("title") or ""
        )

    async def _read(self, page) -> dict:
        raw = await page.evaluate(JS_BROKER)
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else {}
