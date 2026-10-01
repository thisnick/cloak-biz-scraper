"""Any listing page: find the list by its links, and ask the classifier about the rest.

The BizBuySell adapters know their site's markup. This source knows none, and
that is the point: a broker's own site, a marketplace nobody has written an
adapter for, a page seen once. It reads a page in two halves.

* **What code can see, the probe finds** (`JS_PROBE`, in the page). Every link
  is grouped by the shape of its URL — `/listing/foo-bar/` and
  `/listing/baz-123/` are one group — because a list of listings is, almost
  always, many links of one shape. Each link's card is the largest element
  around it that holds no other link of its group, and a card's text is split
  into fields: `label → value` where the card labels them, a DOM slot where it
  does not. Next-page candidates are collected the same way. Before the probe
  runs, the page is scrolled to its bottom and back, so lazily rendered cards
  and pagers are there to be found.
* **What code cannot see, the TypeSafe Classifier (e.g. Jev) decides**: which
  group is the list of businesses for sale (and not the menu, the footer, or a
  "similar listings" rail), what each field holds, and which candidate is
  really the next page. Each of those is ONE request — the classifier reads the
  state once and answers every question in it, so a request per field or per
  link would pay for the same reading many times. (Measured on 2026-09-28:
  bundled answers were as accurate as separate ones.) The rest of a chosen
  list — a second link shape on the same tiles, or the detail links behind an
  "Unlock"/"Watch" group — is then found from the probe's own evidence,
  without asking again (`_whole_list`).

Whether each card is a business for sale NOW — not sold, pending or under
contract, not a menu link or an ad — is not asked here: once the page is down
to its array of cards, the sweep asks every question about one card together,
in one request per card (`services/legibility.py`), and drops the ones that
are not. A status field is still read (a site override's `drop_status` pins
which values to drop without asking anything).

**The list and the fields are decided on a sweep's first page and reused for
the pages after it; the next page is decided on every page; nothing is kept
between sweeps.** A later page asks only about fields the first page did not
have, and a later page without the first page's list (a changed layout) is
decided afresh. Reusing the first page's list is also what reads a last page
with one or two listings on it: one link has no neighbour to bound its card,
so it is found by the first page's card shapes instead (`_Decided`). A site
that changes its layout is read by its new layout on the next sweep, not with
last month's answers. A person can pin any part of it for one site with a
`SiteOverride` (`overrides.py`); a pinned part is never asked about.

What this source refuses to do is guess. A page with no list of businesses on it
is a loud error on page 1 (no retry: a new exit IP shows the same page), not an
empty result an agent would read as "no listings matched". A field is filled
only when the classifier is confident (≥ 0.8), the same "empty beats
confidently wrong" rule as `stores/money.py`; the card's excerpt keeps the text
either way. Money stays verbatim. And the listing id is left empty: stores
dedupe on it first, across sources, and one broker's "Listing #1234" is not
BizBuySell's listing 1234.
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..models import Listing
from ..services import extract
from ..services.blocker import text_contains_blocker
from ..services.typesafe import Choice, Noul, TypeSafeError, TypeSafeNotConfigured
from . import GENERIC_LABEL, GENERIC_NAME
from .base import CardPage, PageNotReached
from .overrides import MONEY_ROLES, ROLES, SiteOverride
from .urls import listing_url, normalize_url

logger = logging.getLogger("cloakbiz.generic")

# ── how the page is read ─────────────────────────────────────────────────────

# A group needs this many distinct links to be a candidate list at all.
MIN_GROUP_LINKS = 3
# Groups whose cards are mostly inside nav/header/footer/menus are the site's
# chrome, never its listings.
MAX_CHROME = 0.8
# Candidate groups offered to the classifier, the biggest (links × text) first.
MAX_CANDIDATES = 12
# Another candidate is more of the chosen list when its cards are the same tile
# in the same element and share at least this much of their fields (Sunbelt's
# two link shapes: 1.0; a franchise ad slotted into BizQuest's list: 0.36).
SAME_LIST_FIELDS = 0.75
# A card field is asked about when at least this many cards have it (every
# card, on a page with fewer): a label on two cards is a field of the list, a
# slot on one is that card's own text.
FIELD_MIN_CARDS = 2
# Values shown for each field asked about, spread across the page (first,
# middle, last…): the first three cards alone are often three of a kind.
FIELD_SAMPLES = 5
# A field fills a Listing only at this confidence; below it the field is left
# empty and the excerpt keeps the text.
FIELD_CONFIDENCE = 0.8
# A next-page candidate is followed at this probability or above.
NEXT_MIN = 0.5
# Bounds on one request, so a strange page cannot build an enormous one.
MAX_FIELDS = 40
MAX_STATUS_VALUES = 20
MAX_NEXT_LINKS = 12
# A page whose text is longer than this is not a bare challenge page, so a
# blocker phrase in its body ("just a moment's walk from the beach") is only
# believed when the page also has no list on it.
_BLOCK_BODY_CHARS = 5000
# Before it is read, a page is scrolled to the bottom in steps and back, so
# cards and pagers that only render when scrolled into view are on it
# (BusinessBroker.net showed 23 of its 53 listings unscrolled; BizQuest's page-2
# link never appeared). A step waits for what it revealed to load; the scroll
# stops at a bottom that no longer grows, or at the bounds, so a feed that
# grows forever costs at most SCROLL_BUDGET_S.
SCROLL_STEP_PX = 2500
SCROLL_PAUSE_MS = 800
SCROLL_MAX_STEPS = 10
SCROLL_BUDGET_S = 10.0
# At a bottom that did not grow, one longer wait before believing it: a lazy
# list that fetches its next batch slower than a step's pause (BusinessBroker.net
# once showed 23 of its 53 listings this way) would otherwise end the scroll.
SCROLL_BOTTOM_SETTLE_MS = 2000
_JS_SCROLL_STATE = (
    "() => [window.scrollY, window.innerHeight, Math.max("
    "document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)]"
)
_JS_SCROLL_TOP = "() => window.scrollTo(0, 0)"
# The probe gets this long to read a page; a page that takes longer (an endless
# DOM, a script that never yields) is not read rather than left to stall the
# sweep.
PROBE_TIMEOUT_S = 30.0
# After a next-page click, how long to wait for the page to show something new
# (more links, a taller document, another address, different links) before
# clicking once more, and then before reading it anyway.
CLICK_SETTLE_S = 10.0
CLICK_POLL_MS = 250
# The links' count, the document's height, the address, and a hash of every
# link's address: a pager that swaps ten cards for ten others in place changes
# none of the first three.
_JS_PAGE_SIZE = (
    "() => { let h = 0; for (const a of document.querySelectorAll('a[href]')) {"
    " const s = a.href; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; }"
    " return [document.querySelectorAll('a[href]').length, Math.max("
    "document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0), "
    "location.href, h]; }"
)

_GROUP_EXAMPLE_CHARS = 260
# Card shapes remembered per pattern for later pages (see `_shape_of`).
_MAX_SHAPES = 8
_CARD_EXAMPLE_CHARS = 400
# A Listing's excerpt, at most: the card's own markdown is usually a few lines,
# but a card that is most of the page would otherwise be stored (and sent to
# triage) whole.
EXCERPT_CHARS = 2000
_FIELD_VALUE_CHARS = 70
_TITLE_MAX_CHARS = 150

# The questions, exactly as measured on 2026-09-28 (the listing-group question
# picked the right group on 18/18 pages × 3 shuffles, "none" on the pages that
# had no list). The state each one reads is built next to where it is asked.
# The listing-group question's last sentence was added after the live gate,
# where single-listing pages picked their "similar listings" rail.
GROUP_QUESTION = (
    "This page is from a website that lists businesses for sale. The links on the page have "
    "been grouped by URL pattern, and each example is the text of the card or block around one "
    "link in that group. Which group is the page's list of businesses for sale, where each item "
    "is a different business being offered? A small 'similar listings', 'you may also like' or "
    "'recently viewed' section beside a single business's details is not the page's list — "
    "answer none for a page that is mainly about one business."
)
GROUP_NONE = "None of these groups is a list of businesses for sale"
FIELD_QUESTION = (
    "Each listing card on this page has the fields in the state. What does {field} hold?"
)
# Its second half is from the second live gate, where Empire Flippers' "Load
# More Listings" scored 0.46–0.49 read literally as "goes to page N".
NEXT_QUESTION = (
    "{link} in the state goes to the next page (page {page}) of the same list of businesses, or "
    "loads more businesses into this list (for example a 'Load more' or 'Show more listings' "
    "button)."
)
# How a candidate with no address of its own is described in the next-page
# state: it can only be clicked.
_SCRIPT_LINK = "(no address: a button or script link on this page)"

# Which Listing field each role fills. Roles not here (category, description,
# listing_id, other) stay in the excerpt only — see the module docstring for
# why listing_id in particular is never filled.
_LISTING_FIELD = {
    "title": "title",
    "location": "location",
    "asking_price": "asking_price",
    "cash_flow_sde": "cashflow",
    "ebitda": "ebitda",
    "revenue": "revenue",
}


# The probe. One pass over the page, no model:
#
# 1. Every link is resolved against document.baseURI — not location.href, which
#    is wrong on pages with a <base> tag — and grouped by URL shape: same host,
#    same number of path segments, same query keys. Within that, a segment
#    position whose values repeat across links (every link has "listing" there)
#    is a literal and splits the group by value; a position whose values are
#    mostly unique (slugs, ids) is a wildcard `{*}`. That is what joins a list
#    whose slugs happen to contain digits with the rest of it (BizQuest's 37+13,
#    Liberty's 9+3) while still keeping /listing-property/… apart from
#    /silicon-valley/… on the same site. At a wildcard, ids ("12899223-…",
#    "1012838.aspx") and plain words ("websites") are split when each side has
#    three or more, the ids as `{id}`: Flippa's listings sit at the site root
#    beside its menu. A link with and without its trailing slash is one link.
# 2. A link's card is its largest ancestor holding no other link of the group
#    (brought back down to its siblings' shape when a neighbouring tile has no
#    link to stop it — Liberty's sold tiles). Its text is split into lines
#    (block elements and <br>), and a line is a label when it ends in ':' or is
#    the same short text on ≥ 80% of cards; a label's value is the next line,
#    when it sits close by. Values are keyed by their label's text — not by
#    position, because cards drop a field when they have no value for it
#    (FCBB), which shifts every position after it — and unlabelled lines by
#    their DOM path. Each group also reports the shape most of its cards have
#    (tag and stable classes) and the elements they sit in, which is how two
#    groups are seen to be one list (Sunbelt's two link shapes).
# 3. Next-page candidates: rel=next, Next/›/»/Load more, and the next page
#    number inside a pager — but not a carousel's arrows and dots. Each
#    clickable one is marked data-cbs-next="<id>" so a script-only control
#    (href="#", a button) can be clicked from Python.
#
# The answer is bounded, so a pathological page (tens of thousands of links, a
# "card" that is the whole page) cannot build an enormous one: at most 60
# groups (pinned ones always kept), 200 cards read per group, 80 fields per
# card and 4000 characters of excerpt per card (the Listing keeps 2000). Python
# gives the probe PROBE_TIMEOUT_S to return at all.
#
# Called with {next_number, patterns} (the page number to look for in a pager,
# and any pinned listing patterns) and, on a later page of a sweep, {shapes,
# known_keys} (page 1's card shapes per pinned pattern, and the field keys
# already named — see `_Decided`). Returns JSON:
#   {url, title, body (first 5000 chars), body_chars,
#    groups: [{pattern, links, chrome, text_chars, varying_keys, paths_unique,
#              card_shape, containers, examples, cards}],   biggest (links × text) first
#    pager:  [{id, url|null, script_only, numbered, appears_as, selector}]}
# where `cards` is null except for the top 12 non-chrome groups and pinned ones:
#   [{card, pos, href, hrefs, link_text, heading, text, excerpt, labeled, slots,
#     shape, container, shaped (found by a known card shape)}]
# (`pos` is the card's link's place among the page's links: page order).
JS_PROBE = r"""
(args) => {
  const opts = args || {};
  const nextNumber = String(opts.next_number || 2);
  const wanted = new Set(opts.patterns || []);
  // What an earlier page of this sweep read (see _Decided): the card shape of
  // each pinned pattern, and the field keys whose meaning is already known.
  const pinShapes = opts.shapes || {};
  const known = new Set(opts.known_keys || []);
  const NONE = new Set();
  const MAX_CLIMB = 15;
  const MAX_DETAILED = 12;
  const MAX_GROUPS = 60, MAX_CARDS = 200, MAX_FIELDS = 80, MAX_EXCERPT = 4000;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const pageUrl = /^https?:/.test(location.href) ? location.href : document.baseURI;
  const here = (() => { try { const u = new URL(pageUrl); u.hash = ''; return u; } catch (_) { return null; } })();
  const isHere = (u) => !!here && u.origin === here.origin && u.pathname === here.pathname && u.search === here.search;

  // Marks from an earlier probe of this same document (a "Load more" click
  // keeps the DOM) would otherwise point at stale elements.
  for (const el of document.querySelectorAll('[data-cbs-next]')) el.removeAttribute('data-cbs-next');
  for (const el of document.querySelectorAll('[data-cbs-card]')) el.removeAttribute('data-cbs-card');

  // The same list as urls.py's _TRACKING — and, like it, not `ref`, which
  // sites use as the listing id (?ref=1234).
  const TRACKING = /^(utm_.*|gclid|gbraid|wbraid|dclid|fbclid|msclkid|yclid|twclid|igshid|mc_cid|mc_eid|_ga|_gl|_hsenc|_hsmi|mkt_tok)$/i;
  const resolve = (raw) => {
    if (raw == null) return null;
    try { const u = new URL(raw, document.baseURI); u.hash = ''; return u; } catch (_) { return null; }
  };

  // ── 1. links, grouped by URL shape ──
  // A link with and without a trailing slash is one link (Empire Flippers
  // prints both on every card). Counted as two, every id would look like a
  // value repeated across links — a literal — and each card would stop at its
  // own second link.
  const keyOf = (u) => u.origin + u.pathname.replace(/\/+$/, '') + u.search;
  const hrefOf = new Map();
  const keyOfA = new Map();
  const byLink = new Map();
  const anchorPos = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    anchorPos.set(a, anchorPos.size);
    const u = resolve(a.getAttribute('href'));
    if (!u || !/^https?:$/.test(u.protocol)) continue;
    const k = keyOf(u);
    hrefOf.set(a, u.href);
    keyOfA.set(a, k);
    if (isHere(u)) continue;
    if (!byLink.has(k)) byLink.set(k, { href: u.href, u, anchors: [] });
    byLink.get(k).anchors.push(a);
  }
  const coarse = new Map();
  for (const [k, item] of byLink) {
    const u = item.u;
    const segs = u.pathname.split('/').filter(Boolean).map((s) => s.toLowerCase());
    const keys = [...new Set([...u.searchParams.keys()].filter((q) => !TRACKING.test(q)))].sort();
    const shape = `${u.host}|${segs.length}|${keys.join('&')}`;
    if (!coarse.has(shape)) coarse.set(shape, { host: u.host, n: segs.length, keys, items: [] });
    coarse.get(shape).items.push({ key: k, href: item.href, u, segs, host: u.host, keys: keys.join('&') });
  }
  // At a wildcard position, ids ("12899223-a-branded-…", "1012838.aspx",
  // "bw2452180", "…-flowing-59917") do not merge with plain words ("websites",
  // "about") when each side has three or more: that is a site's listings next
  // to its menu (Flippa), not one list. The ids become `{id}`. A slug that
  // merely contains a number stays with the words — BizQuest's
  // "3-tahoe-area-laundromats-for-sale" is the same list as "flooring-store".
  const ID = /^(?:\d+|[a-z]{1,3}-?\d{4,}|\d{5,}[-_].*|.*[-_]\d{5,})(?:\.[a-z]{2,5})?$/;
  const WORD = /^\D+$/;
  const wild = (items, depth, parts, n, out) => {
    const ids = items.filter((it) => ID.test(it.segs[depth]));
    const words = items.filter((it) => WORD.test(it.segs[depth]));
    if (ids.length >= 3 && words.length >= 3) {
      refine(ids, depth + 1, parts.concat(['{id}']), n, out);
      refine(items.filter((it) => !ID.test(it.segs[depth])), depth + 1, parts.concat(['{*}']), n, out);
      return;
    }
    refine(items, depth + 1, parts.concat(['{*}']), n, out);
  };
  const refine = (items, depth, parts, n, out) => {
    if (depth === n) { out.push({ parts, items }); return; }
    const counts = new Map();
    for (const it of items) counts.set(it.segs[depth], (counts.get(it.segs[depth]) || 0) + 1);
    if (counts.size === 1) { refine(items, depth + 1, parts.concat([items[0].segs[depth]]), n, out); return; }
    if (counts.size * 2 <= items.length) {
      for (const [value, c] of counts) {
        if (c >= 2) refine(items.filter((it) => it.segs[depth] === value), depth + 1, parts.concat([value]), n, out);
      }
      const rest = items.filter((it) => counts.get(it.segs[depth]) === 1);
      if (rest.length) wild(rest, depth, parts, n, out);
      return;
    }
    wild(items, depth, parts, n, out);
  };
  const raw = [];
  for (const c of coarse.values()) {
    const out = [];
    refine(c.items, 0, [], c.n, out);
    for (const g of out) {
      const pattern = `${c.host}/${g.parts.join('/')}${c.keys.length ? '?' + c.keys.join('&') : ''}`;
      raw.push({ pattern, keys: c.keys, items: g.items });
    }
  }

  // A pinned pattern (a site override) that no group has exactly is read as a
  // wildcard over every link: this page may have grouped its links differently
  // (a last page with two listings, a segment that was the same on every link
  // of the page the pattern was copied from).
  for (const p of wanted) {
    if (raw.some((g) => g.pattern === p)) continue;
    const qi = p.indexOf('?');
    const parts = (qi < 0 ? p : p.slice(0, qi)).split('/');
    const host = parts.shift().toLowerCase();
    const segs = parts.filter(Boolean).map((s) => s.toLowerCase());
    const keys = qi < 0 ? '' : p.slice(qi + 1).split('&').filter(Boolean).sort().join('&');
    const items = [];
    for (const c of coarse.values()) {
      for (const it of c.items) {
        if (it.host === host && it.keys === keys && it.segs.length === segs.length
            && segs.every((s, i) => s === '{*}' || (s === '{id}' && ID.test(it.segs[i]))
                                    || s === it.segs[i])) items.push(it);
      }
    }
    if (items.length) raw.push({ pattern: p, keys: keys ? keys.split('&') : [], items });
  }

  // ── 2. cards ──
  // Class names that are build hashes or framework noise say nothing about
  // what an element is, and differ between otherwise identical cards.
  const stable = (el) => [...el.classList].filter((c) => !/\d{3,}|^css-|^sc-|^jsx-|^ng-|__[a-z0-9]{5,}$|^[a-z]{1,2}-[a-zA-Z0-9]{5,}$/.test(c));
  const CHROME = 'nav, footer, [role=navigation], [role=contentinfo], [role=banner], [role=menu], [role=menubar], [class*=menu], [id*=menu], [class*=footer], [id*=footer], [class*=navbar]';
  const inChrome = (el) => {
    if (el.closest(CHROME)) return true;
    const h = el.closest('header');
    return !!h && !h.closest('main, article, [role=main]');
  };
  const cardFor = (a, keySet, mine) => {
    let node = a;
    for (let p = node.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      let other = false;
      for (const x of p.querySelectorAll('a[href]')) {
        const h = keyOfA.get(x);
        if (h && h !== mine && keySet.has(h)) { other = true; break; }
      }
      if (other) break;
      node = p;
    }
    return node;
  };
  // A card that climbed past its siblings' shape (its row-mates have no link
  // of the group — a sold tile with no detail page) is brought back down to
  // the shape most cards have.
  // Bounded, so a page of Tailwind-long class lists cannot bloat the answer.
  const sig = (el) => (el.tagName + '.' + stable(el).sort().join('.')).slice(0, 300);
  // A pinned pattern whose cards an earlier page of the sweep read has their
  // shapes: the nearest element around the link (the link included) with the
  // tag and classes of one of those cards, sitting in an element shaped like
  // the one most of them sat in, is the card. Any of the earlier page's
  // shapes, not only the commonest: the one listing left on a last page may
  // be the "new" or "featured" tile (Empire Flippers). That needs no
  // neighbouring link to stop the climb, so the one listing on a last page is
  // still one card. Never a card holding another link of the group; null when
  // nothing within MAX_CLIMB levels fits.
  const shapedCard = (a, shape, keySet, mine) => {
    const tiles = new Set([shape.card, ...(shape.cards || [])]);
    let n = a;
    for (let i = 0; n && i <= MAX_CLIMB && n !== document.body && n !== document.documentElement; i++, n = n.parentElement) {
      if (!tiles.has(sig(n))) continue;
      if (shape.container && (!n.parentElement || sig(n.parentElement) !== shape.container)) continue;
      for (const x of n.querySelectorAll('a[href]')) {
        const h = keyOfA.get(x);
        if (h && h !== mine && keySet.has(h)) return null;
      }
      return n;
    }
    return null;
  };
  const align = (cards) => {
    const counts = new Map();
    for (const c of cards) counts.set(sig(c.el), (counts.get(sig(c.el)) || 0) + 1);
    let modal = null, most = 0;
    for (const [k, n] of counts) if (n > most) { modal = k; most = n; }
    if (most * 2 < cards.length) return;
    for (const c of cards) {
      if (c.shaped || sig(c.el) === modal) continue;
      for (let n = c.anchor; n && n !== c.el; n = n.parentElement) {
        if (sig(n) === modal) { c.el = n; break; }
      }
    }
  };
  // The element a group's cards sit in, named so that two groups can be seen
  // to share it (Sunbelt's one list whose cards link two ways).
  const listIds = new Map();
  const listOf = (el) => {
    if (!el) return null;
    if (!listIds.has(el)) listIds.set(el, 'l' + (listIds.size + 1));
    return listIds.get(el);
  };
  const groups = [];
  for (const g of raw) {
    // One link has no neighbour to bound its card, which would grow to the
    // whole page; below three, only a pinned pattern is worth reading, and
    // below two only one whose card shape is known.
    const pinned = wanted.has(g.pattern);
    const knownShape = pinned && pinShapes[g.pattern] && pinShapes[g.pattern].card ? pinShapes[g.pattern] : null;
    if (g.items.length < (pinned ? (knownShape ? 1 : 2) : 3)) continue;
    const keySet = new Set(g.items.map((it) => it.key));
    const cards = g.items.map((it) => {
      const anchor = byLink.get(it.key).anchors[0];
      const el = knownShape ? shapedCard(anchor, knownShape, keySet, it.key) : null;
      return { href: it.href, key: it.key, anchor, el: el || cardFor(anchor, keySet, it.key), shaped: !!el };
    });
    if (cards.length < 2 && !cards[0].shaped) continue;
    align(cards);
    let chrome = 0, chars = 0;
    const shapes = new Map();
    for (const c of cards) {
      c.text = clean(c.el.innerText || c.el.textContent);
      if (inChrome(c.el)) chrome++;
      chars += Math.min(c.text.length, 1000);
      shapes.set(sig(c.el), (shapes.get(sig(c.el)) || 0) + 1);
    }
    // The card's shape (tag and stable classes) most cards have, and the
    // elements those cards sit in.
    let shape = null, most = 0;
    for (const [k, count] of shapes) if (count > most) { shape = k; most = count; }
    if (most * 2 < cards.length) shape = null;
    const containers = shape
      ? [...new Set(cards.filter((c) => sig(c.el) === shape).map((c) => listOf(c.el.parentElement)))]
      : [];
    const varying = g.keys.filter((k) => new Set(g.items.map((it) => it.u.searchParams.get(k))).size > 1);
    const paths = new Set(g.items.map((it) => it.u.host + it.u.pathname.replace(/\/+$/, '')));
    groups.push({
      pattern: g.pattern,
      links: g.items.length,
      chrome: +(chrome / cards.length).toFixed(2),
      text_chars: Math.round(chars / cards.length),
      varying_keys: varying,
      paths_unique: paths.size === g.items.length,
      card_shape: shape,
      containers: containers.filter(Boolean),
      examples: cards.slice(0, 3).map((c) => c.text.slice(0, 300)),
      _cards: cards,
    });
  }
  groups.sort((a, b) => b.links * b.text_chars - a.links * a.text_chars);
  for (let i = groups.length - 1; i >= MAX_GROUPS; i--) {
    if (!wanted.has(groups[i].pattern)) groups.splice(i, 1);
  }

  // ── 3. fields ──
  const visible = (el) => (el.checkVisibility ? el.checkVisibility() : el.offsetParent !== null);
  const pathOf = (el, root) => {
    const parts = [];
    for (let n = el; n && n !== root; n = n.parentElement) {
      const cls = stable(n)[0];
      parts.unshift(n.tagName.toLowerCase() + (cls ? '.' + cls : ''));
    }
    const top = stable(root)[0];
    return [root.tagName.toLowerCase() + (top ? '.' + top : '')].concat(parts).join('>');
  };
  const display = new Map();
  const lineOf = (el, root) => {
    for (let n = el; n && n !== root; n = n.parentElement) {
      if (!display.has(n)) display.set(n, getComputedStyle(n).display);
      const d = display.get(n);
      if (d !== 'inline' && d !== 'contents') return n;
    }
    return root;
  };
  const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|svg)$/;
  const linesOf = (card) => {
    const out = [];
    let cur = null;
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.nodeType === 1 && SKIP.test(n.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.nodeType === 1) { if (n.tagName === 'BR') cur = null; continue; }
      const parent = n.parentElement;
      if (!parent || !visible(parent)) continue;
      const text = n.textContent;
      if (!text.trim()) { if (cur) cur.raw += text; continue; }
      const line = lineOf(parent, card);
      if (cur && cur.line === line) cur.raw += text;
      else { cur = { line, el: parent, raw: text }; out.push(cur); }
    }
    for (const l of out) l.text = clean(l.raw);
    return out.filter((l) => l.text);
  };
  const LETTER = /[A-Za-z]/;
  const INLINE = /^([^:$\d][^:$]{0,29}?)\s*:\s*(\S.*)$/;
  const isCta = (l) => {
    const c = l.el.closest('a, button, [role=button]');
    return !!c && clean(c.innerText || c.textContent) === l.text;
  };
  const near = (label, value, levels) => {
    let up = label.line;
    for (let i = 0; i < levels && up; i++) {
      up = up.parentElement;
      if (up && up.contains(value.line)) return true;
    }
    return label.line.contains(value.line);
  };
  const colonLabel = (l) => l.text.length <= 40 && l.text.endsWith(':') && LETTER.test(l.text);
  const pairs = (lines, i, levels, maxChars) => {
    const v = lines[i + 1];
    return !!v && !v.isLabel && !isCta(v) && v.text.length <= maxChars && near(lines[i], v, levels);
  };
  // `prior` is the field keys an earlier page of the sweep read on these cards:
  // a label there is a label here, even on a last page with too few cards for
  // it to recur, so the field keeps its key (and its known meaning).
  const fieldsFor = (cards, prior) => {
    const all = cards.map((c) => linesOf(c.el));
    // "Label: value" on one line counts as a label only when the label recurs
    // across cards; a title that happens to contain a colon is still a title.
    const inlineOn = new Map();
    for (const lines of all) {
      const here = new Set();
      for (const l of lines) {
        const m = l.text.match(INLINE);
        if (m && m[1].split(/\s+/).length <= 4 && LETTER.test(m[1]) && !m[2].startsWith('//')) {
          l.inline = [clean(m[1]), m[2]];
          here.add(l.inline[0]);
        }
      }
      for (const k of here) inlineOn.set(k, (inlineOn.get(k) || 0) + 1);
    }
    for (const lines of all) {
      for (const l of lines) {
        if (l.inline && !prior.has(l.inline[0])
            && (inlineOn.get(l.inline[0]) || 0) < Math.max(2, 0.3 * cards.length)) l.inline = null;
        l.isLabel = !l.inline && colonLabel(l);
      }
    }
    if (prior.size) {
      for (const lines of all) {
        lines.forEach((l, i) => {
          if (!l.isLabel && !l.inline && l.text.length <= 40 && prior.has(l.text) && !isCta(l)
              && pairs(lines, i, 2, 80)) l.isLabel = true;
        });
      }
    }
    // A short text on ≥ 80% of cards is a label — when it sits next to a value
    // on most of them. The second half is what keeps a value that merely
    // happens to be the same everywhere ("California", "ACTIVE") from
    // swallowing the line after it.
    if (cards.length >= 3) {
      const onCards = new Map();
      for (const lines of all) for (const t of new Set(lines.map((l) => l.text))) onCards.set(t, (onCards.get(t) || 0) + 1);
      const tally = new Map();
      all.forEach((lines) => lines.forEach((l, i) => {
        if (l.isLabel || l.inline || l.text.length > 40 || !LETTER.test(l.text) || /[\d$€£:]/.test(l.text)) return;
        if ((onCards.get(l.text) || 0) < 0.8 * cards.length || isCta(l)) return;
        const t = tally.get(l.text) || { seen: 0, paired: 0 };
        t.seen++;
        if (pairs(lines, i, 2, 80)) t.paired++;
        tally.set(l.text, t);
      }));
      for (const lines of all) {
        lines.forEach((l, i) => {
          const t = tally.get(l.text);
          if (t && t.paired * 2 >= t.seen && pairs(lines, i, 2, 80)) l.isLabel = true;
        });
      }
    }
    const out = all.map((lines, ci) => {
      const labeled = {}, slots = {}, seen = new Map();
      let fieldCount = 0;
      const put = (obj, key, value) => {
        if (fieldCount >= MAX_FIELDS) return;
        fieldCount++;
        let k = key, i = 2;
        while (k in obj) k = `${key} (${i++})`;
        obj[k] = value.slice(0, 500);
      };
      for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.isLabel) {
          if (pairs(lines, i, 3, 1000)) { put(labeled, clean(l.text.replace(/:\s*$/, '')), lines[i + 1].text); i++; }
          continue;
        }
        if (l.inline) { put(labeled, l.inline[0], l.inline[1]); continue; }
        const p = pathOf(l.line, cards[ci].el);
        const idx = seen.get(p) || 0;
        seen.set(p, idx + 1);
        put(slots, `${p}#${idx}`, l.text);
      }
      return { labeled, slots };
    });
    // One slot whose element's class names its value (div.activeButton,
    // div.soldButton, div.contractButton) is one field, not three: slots that
    // differ only in the last element's class and never share a card are
    // merged under the class-less key.
    const VARIANT = /^((?:.*>)?[a-z][a-z0-9-]*)(?:\.[^>#]+)?(#\d+)$/i;
    const members = new Map();
    for (const f of out) {
      for (const k of Object.keys(f.slots)) {
        const m = k.match(VARIANT);
        if (!m) continue;
        const base = m[1] + m[2];
        if (!members.has(base)) members.set(base, new Set());
        members.get(base).add(k);
      }
    }
    // A last page with one of those cards has one variant only; it is still
    // the class-less slot an earlier page read.
    const rename = new Map();
    for (const [base, keys] of members) {
      if (keys.size < 2 && !(prior.has(base) && ![...keys].some((k) => prior.has(k)))) continue;
      if (out.some((f) => [...keys].filter((k) => k in f.slots).length > 1)) continue;
      for (const k of keys) rename.set(k, base);
    }
    if (rename.size) {
      for (const f of out) {
        const slots = {};
        for (const [k, v] of Object.entries(f.slots)) slots[rename.get(k) || k] = v;
        f.slots = slots;
      }
    }
    return out;
  };
  const detail = (g, gi) => {
    const read = g._cards.slice(0, MAX_CARDS);
    const fields = fieldsFor(read, wanted.has(g.pattern) ? known : NONE);
    return read.map((c, i) => {
      if (!c.el.hasAttribute('data-cbs-card')) c.el.setAttribute('data-cbs-card', `g${gi}c${i}`);
      let linkText = '';
      const own = c.el.matches('a[href]') ? [c.el, ...c.el.querySelectorAll('a[href]')] : [...c.el.querySelectorAll('a[href]')];
      for (const a of own) {
        if (keyOfA.get(a) !== c.key) continue;
        const t = clean(a.innerText || a.textContent) || clean(a.getAttribute('title')) || clean(a.getAttribute('aria-label'));
        if (t.length > linkText.length) linkText = t;
      }
      const h = c.el.querySelector('h1, h2, h3, h4, h5, h6') || c.el.querySelector('[class*=title i]');
      const host = new URL(c.href).host;
      const hrefs = [];
      const listed = new Set();
      for (const a of own) {
        const x = hrefOf.get(a);
        if (x && new URL(x).host === host && !listed.has(keyOfA.get(a))) {
          listed.add(keyOfA.get(a));
          hrefs.push(x);
        }
        if (hrefs.length >= 20) break;
      }
      let excerpt = c.text;
      try { if (window.__cbsMarkdown) excerpt = window.__cbsMarkdown(c.el.innerHTML).trim(); } catch (_) {}
      const out = {
        card: c.el.getAttribute('data-cbs-card'),
        pos: anchorPos.get(c.anchor),
        href: c.href,
        hrefs,
        link_text: linkText.slice(0, 500),
        heading: h ? clean(h.innerText || h.textContent).slice(0, 500) : '',
        text: c.text.slice(0, 2000),
        excerpt: excerpt.slice(0, MAX_EXCERPT),
        labeled: fields[i].labeled,
        slots: fields[i].slots,
        shape: sig(c.el),
        container: c.el.parentElement ? sig(c.el.parentElement) : null,
      };
      if (c.shaped) out.shaped = true;
      return out;
    });
  };
  let detailed = 0;
  groups.forEach((g, gi) => {
    const top = g.chrome < 0.8 && detailed < MAX_DETAILED;
    if (top) detailed++;
    g.cards = (top || wanted.has(g.pattern)) ? detail(g, gi) : null;
  });
  for (const g of groups) delete g._cards;

  // ── 4. next-page candidates ──
  // A pager is a nav, a list, or an element whose class, id or label says
  // pagination — as a word, not a substring: WordPress puts "page-template"
  // and "page-id-253146" on <body> and "#page" around everything, and a
  // substring match made every control on QuietLight "in a pagination block".
  // The walk stops below <body> for the same reason.
  const PAGER_NAME = /pagina|pager|paging|pagenav|(^|[\s_-])pages([\s_-]|$)|page-?numbers?|page-?(item|link)s?([\s_-]|$)/i;
  const PAGER_LABEL = /pagina|pager|paging|\bpages\b|page navigation|\bpage\s*\d/i;
  const pagerBlock = (el) => {
    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (/^(NAV|UL|OL)$/.test(n.tagName) || n.getAttribute('role') === 'navigation') return n;
      if (PAGER_NAME.test(typeof n.className === 'string' ? n.className : '') || PAGER_NAME.test(n.id || '')) return n;
      if (PAGER_LABEL.test(n.getAttribute('aria-label') || '')) return n;
    }
    return null;
  };
  // A cookie or privacy banner's own buttons ("Show more", "Accept") are never
  // the next page (QuietLight's CookieYes "Show more" was clicked as one).
  // Known consent tools and the words they use, in an id, a class or a data-*
  // attribute of the control or anything around it. `cmp` only as a whole
  // word or a known tool's prefix: Adobe's components are all "cmp-…".
  const CONSENT = /cookie|consent|gdpr|ccpa|onetrust|optanon|ot-sdk|didomi|osano|truste([^a-z]|$)|trustarc|iubenda|termly|usercentrics|cmplz|sp_message|(^|[^a-z])cky([^a-z]|$)|cc-(window|banner|revoke)|(^|[\s_-])cmp([\s_-]*$|\s)|cmpbox|qc-cmp/i;
  const inConsent = (el) => {
    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (CONSENT.test(n.id || '') || CONSENT.test(typeof n.className === 'string' ? n.className : '')) return true;
      for (const a of n.attributes) {
        if (a.name.startsWith('data-') && (CONSENT.test(a.name) || (a.value.length <= 60 && CONSENT.test(a.value)))) return true;
      }
      if (/cookie|consent|privacy/i.test(n.getAttribute('aria-label') || '') && n.getAttribute('role') === 'dialog') return true;
    }
    return false;
  };
  const NEXT = /^(next( page)?|next\s*[›»>→]|[›»>→]|>>|older( posts| entries)?|(load|show|view|see) more.*|more results)$/i;
  const selectorFor = (el) => {
    if (el.id && !/\d{3,}/.test(el.id)) return '#' + CSS.escape(el.id);
    const cls = stable(el).slice(0, 3).map((c) => '.' + CSS.escape(c)).join('');
    return cls ? el.tagName.toLowerCase() + cls : null;
  };
  const pager = [];
  const byKey = new Map();
  const add = (el, how) => {
    const rawHref = el.tagName === 'A' || el.tagName === 'LINK' ? el.getAttribute('href') : null;
    const u = rawHref && !/^\s*(#|javascript:)/i.test(rawHref) ? resolve(rawHref) : null;
    const real = !!u && /^https?:$/.test(u.protocol) && !isHere(u);
    if (!real && (el.tagName === 'LINK' || !visible(el))) return;
    if (el.tagName !== 'LINK' && inConsent(el)) return;
    const inPager = !!pagerBlock(el);
    const key = real ? 'url:' + u.href : `script:${how}:${inPager}`;
    let cand = byKey.get(key);
    if (!cand) {
      cand = { id: 'n' + (pager.length + 1), url: real ? u.href : null, script_only: !real, appears_as: [], selector: null, numbered: true };
      byKey.set(key, cand);
      pager.push(cand);
      if (el.tagName !== 'LINK') {
        el.setAttribute('data-cbs-next', cand.id);
        cand.selector = selectorFor(el);
      }
    }
    // A page-number control only ever means "page N"; a Next/» one means
    // "the next page" wherever it is used, so only the latter can be pinned.
    if (how !== `text '${nextNumber}'`) cand.numbered = false;
    const note = inPager && how !== 'rel=next' ? `${how} (in a pagination block)` : how;
    if (!cand.appears_as.includes(note)) cand.appears_as.push(note);
  };
  // A carousel's own Next arrow and numbered dots (Empire Flippers'
  // testimonials) turn the carousel, never the page; so does a photo
  // lightbox's (FCBB's PhotoSwipe "Next (arrow right)" scored 0.80 against its
  // pager's 0.82 on the saved page).
  const CAROUSEL = '[class*=carousel i], [class*=slick-slider], [class*=swiper], .glide, .splide, .flickity-enabled, [aria-roledescription=carousel i], '
    + '[class*=lightbox i], #lightbox, .pswp, [class*=fancybox], .lg-outer, .lg-container, .mfp-wrap, [class*=glightbox]';
  for (const l of document.querySelectorAll('link[rel~=next][href], a[rel~=next]')) add(l, 'rel=next');
  for (const el of document.querySelectorAll('a, button, [role=button], [role=link]')) {
    if (pager.length >= 30) break;
    if (el.closest(CAROUSEL)) continue;
    const t = clean(el.innerText || el.textContent);
    const aria = clean(el.getAttribute('aria-label') || el.getAttribute('title'));
    if (t && NEXT.test(t)) add(el, `text '${t.slice(0, 40)}'`);
    else if (!t && aria && /\bnext\b|load more|show more/i.test(aria)) add(el, `label '${aria.slice(0, 40)}'`);
    else if (t === nextNumber && pagerBlock(el)) add(el, `text '${t}'`);
    else if (!t && /(^|[\s_-])next([\s_-]|$)/i.test(typeof el.className === 'string' ? el.className : '')) add(el, `class '${el.className.slice(0, 60)}'`);
  }

  const body = clean(document.body ? document.body.innerText : '');
  return JSON.stringify({
    url: pageUrl,
    title: document.title || '',
    body: body.slice(0, 5000),
    body_chars: body.length,
    groups,
    pager,
  });
}
"""


# ── the source ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Next:
    """How to reach the next page, decided on the page before it.

    For a control the probe found, `target` is its `data-cbs-next` mark, and
    `selector` (a stable CSS selector for it, when it has one and is not a page
    number) and `appears_as` (how it read: "text 'Load more'") are how it is
    found again when a site re-renders it and the mark is lost.
    """

    kind: str  # "goto" | "click"
    target: str  # a URL, or a CSS selector
    selector: str | None = None
    appears_as: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Decided:
    """The list a page decided on, pinned for the later pages of the same sweep.

    `patterns` are the link patterns that were read, "same list" ones
    included; `action` is an action pattern that was picked ("Contact seller",
    "Unlock") and read through the detail links inside its cards, when that is
    how the list was read (`_whole_list`). `shapes` is each pattern's card
    shape — the tag and stable classes most of its cards had (`card`), every
    such shape its cards had (`cards`), and those of the element most of them
    sat in (`container`) — which is how a later page finds the card around a
    link with no neighbouring link to stop at: the one listing on a last page.
    `identity` is each pattern's query keys that named the listing
    (`varying_keys`) and whether its paths did (`paths_unique`): a last page
    with one `listing.aspx?LID=…` link has no second link for its LID to vary
    against, and without them every such listing would be stored under the
    same address.
    """

    page: int
    patterns: tuple[str, ...]
    action: str | None
    shapes: dict[str, dict[str, Any]]
    identity: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def pinned(self) -> list[str]:
        """Every pattern the probe must read on a later page."""
        return list(dict.fromkeys([*self.patterns, *([self.action] if self.action else [])]))


@dataclass
class _Card:
    """One card of the chosen list, with the address it will be stored under."""

    raw: dict[str, Any]
    href: str
    url: str
    normalized_url: str
    values: dict[str, str]


class GenericSource:
    """One swept URL on a site with no adapter of its own.

    Created per swept URL (not registered in `sources.SOURCES`), with the
    classifier it cannot work without and the site's override, if any. It holds
    what it decided on the current attempt — the page it is on, how to reach
    the next one, the list and field decisions later pages reuse, a record of
    every decision — and `begin()` forgets all of it, because the sweep reuses
    one source object across retries.

    Within one attempt, the list and the fields are decided on the first page
    and reused on the pages after it (`_Decided`, `_known_fields`): a later
    page asks only about field keys no earlier page had, and a page that does
    not have the first page's list at all (a changed layout) is decided
    afresh, and that decision is the one reused from then on. The next page is
    decided on every page.

    `decisions` has one JSON-safe dict per page read, saying what was decided
    and by whom ("override", "jev", "probe" when there was nothing to ask, or
    "page N" when page N's answer was reused): `page`, `url`; `listing_links`
    {by, patterns, confidence, model, candidates, and when they apply
    same_list, instead_of, left_out, cards_by_shape (cards found by the reused
    card shape), missing (the reused patterns a page did not have)};
    `fields` [{key, labeled, role, by, confidence, used}];
    `status` {by: "override", unavailable, values} when a site override's
    `drop_status` applied to a status field; `next_page` {by, rule (a URL, "click" or
    "none"), probability, candidates, appears_as, selector}; `cards`, `kept`,
    `dropped_unavailable`; and `blocked` or `error` when the page ended that
    way. `suggested_override()` turns it into a paste-ready `SiteOverride`.
    """

    name = GENERIC_NAME
    label = GENERIC_LABEL
    describes = ("Any other site's page of businesses for sale, read with the TypeSafe "
                 "Classifier (e.g. Jev)")
    example = "https://www.websiteclosers.com/businesses-for-sale/"
    # It picks the cards itself, so the sweep may drop single cards its
    # per-listing request judges not to be a business for sale now (see `Source`).
    chooses_cards = True

    def __init__(self, url: str, classifier, override: SiteOverride | None = None) -> None:
        self.url = url
        p = urlparse(url)
        self.host = (p.hostname or "").lower()
        self.site = self.host[4:] if self.host.startswith("www.") else self.host
        self.warmup_url = f"{(p.scheme or 'https').lower()}://{p.netloc}/"
        self.override = override
        self._classifier = classifier
        self.decisions: list[dict[str, Any]] = []
        self._page = 1
        self._next: _Next | None = None
        # The control clicked to reach the page being read (None when it was
        # reached by its address), and the listings the page before showed:
        # a click the site ignored leaves the same listings in front of us.
        self._clicked: str | None = None
        self._last_urls: frozenset[str] = frozenset()
        # Reused by the later pages of this attempt, never kept past it.
        self._decided: _Decided | None = None
        self._known_fields: dict[str, dict[str, Any]] = {}

    # -- the Source protocol --

    def begin(self) -> None:
        """Forget the last attempt: back to page 1, nothing decided."""
        self._page = 1
        self._next = None
        self._clicked = None
        self._last_urls = frozenset()
        self.decisions = []
        self._forget()

    def _forget(self) -> None:
        """Drop the list and field decisions later pages would reuse."""
        self._decided = None
        self._known_fields = {}

    def matches(self, url: str) -> bool:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        site = host[4:] if host.startswith("www.") else host
        return p.scheme.lower() in ("http", "https") and site == self.site

    def page_url(self, url: str, page: int) -> str:
        """Page 1 is the URL as given; later pages are reached by `advance`."""
        return url

    def has_next_page(self) -> bool:
        """Whether the page just read showed a way to the next one."""
        return self._next is not None

    async def advance(self, page, n: int) -> bool:
        """Go to page `n` the way the previous page said to: a URL, or a click.

        False when the previous page showed no next page. A control that was
        there and cannot be clicked raises PageNotReached: the pages after it
        exist and were not read, and the sweep has to say so.
        """
        step, self._next = self._next, None
        self._clicked = None
        if step is None:
            return False
        if step.kind == "goto":
            await page.goto(step.target, wait_until="domcontentloaded", timeout=120_000)
        else:
            control = ", ".join(step.appears_as) or step.target
            before = await _page_size(page)
            how = await self._click(page, n, step)
            record = self.decisions[-1].get("next_page") if self.decisions else None
            if isinstance(record, dict):
                record["clicked_by"] = how or "nothing"
            if how is None:
                raise PageNotReached(
                    f"the next-page control on page {n - 1} ({control}) could not be clicked")
            await _loaded(page)
            if not await _settle(page, before, self.url):
                # A click the page ignored. FCBB's "4" once did nothing for the
                # whole wait; page 3 was read again, looked like the end, and
                # pages 4-6 were never read. Once more, but only by the mark:
                # it is on the element the probe saw, so while it is there the
                # page has not been redrawn — a "Next" found again by its
                # selector on a page that did move would skip one.
                again = None
                if step.target.startswith("[data-cbs-next=") and await _unchanged(page, before):
                    again = "mark" if await _try_click(page, step.target, self.url) else None
                if isinstance(record, dict):
                    record["clicked_again_by"] = again or "nothing"
                if again is not None:
                    logger.info("generic: clicked the next-page control on %s again", self.url)
                    await _loaded(page)
                    await _settle(page, before, self.url)
            self._clicked = control
        self._page = n
        return True

    async def _click(self, page, n: int, step: _Next) -> str | None:
        """Click the next-page control; how it was found, or None when it could not be.

        By its mark first. A site that re-renders the control after the probe
        marked it (FacetWP's "Load more" on Synergy, re-drawn as it scrolls into
        view) leaves the mark on nothing, so then by the stable selector the
        probe recorded — when exactly one visible element has it — and last by
        probing the pager again and finding the control that reads the same.
        """
        if await _try_click(page, step.target, self.url):
            # A pinned `click:<css>` is its own selector; the probe's controls
            # are clicked by the mark it left on them.
            how = "mark" if step.target.startswith("[data-cbs-next=") else "selector"
            logger.info("generic: clicked the next-page control on %s by its %s", self.url, how)
            return how
        if step.selector and await _try_click(page, step.selector, self.url, exactly_one=True):
            logger.info("generic: clicked the next-page control on %s by its selector %s",
                        self.url, step.selector)
            return "selector"
        if step.appears_as:
            try:
                probe = await _run_probe(page, {"next_number": n, "patterns": []})
            except Exception as exc:  # noqa: BLE001 — a re-probe that fails ends paging
                logger.warning("generic: could not probe %s again for its next-page control: %s",
                               self.url, exc)
                probe = None
            wanted = set(step.appears_as)
            again = next((c for c in (probe or {}).get("pager") or []
                          if isinstance(c, dict) and not c.get("url")
                          and wanted & set(c.get("appears_as") or [])), None)
            if again is not None:
                mark = f'[data-cbs-next="{again.get("id")}"]'
                if await _try_click(page, mark, self.url):
                    logger.info("generic: clicked the next-page control on %s after probing "
                                "the pager again (%s)", self.url, ", ".join(sorted(wanted)))
                    return "re-probe"
        logger.warning("generic: the next-page control on %s could not be clicked by its mark, "
                       "its selector or a fresh probe; paging stops", self.url)
        return None

    async def cards(self, page) -> CardPage:
        n = self._page
        self._next = None
        clicked, self._clicked = self._clicked, None
        record: dict[str, Any] = {"page": n, "url": getattr(page, "url", "") or self.url}
        self.decisions.append(record)

        await _scroll_through(page)
        await extract.inject(page)
        links = list(self.override.listing_links) if self.override else []
        args: dict[str, Any] = {"next_number": n + 1, "patterns": links}
        decided = self._decided
        if decided is not None and n > decided.page:
            # The list an earlier page decided on is read here with its card
            # shape, so a last page with one or two listings is still read,
            # and with the field keys already known, so they keep their keys.
            args = {"next_number": n + 1,
                    "patterns": list(dict.fromkeys([*links, *decided.pinned])),
                    "shapes": decided.shapes, "known_keys": list(self._known_fields)}
        try:
            probe = await _run_probe(page, args)
        except asyncio.TimeoutError:
            message = (f"Reading the listings on {record['url']} took longer than "
                       f"{PROBE_TIMEOUT_S:.0f} s, so the page was not used.")
            record["error"] = message
            return CardPage(listings=[], retry=False, error=message)
        if not isinstance(probe, dict):
            raise RuntimeError(f"the page at {record['url']} could not be read")
        title = str(probe.get("title") or "")
        record["url"] = str(probe.get("url") or record["url"])

        if self._blocked(probe):
            record["blocked"] = True
            return CardPage(listings=[], blocked=True, title=title)
        try:
            return await self._read(probe, n, title, record, clicked)
        except TypeSafeError as exc:
            record["error"] = str(exc)
            return CardPage(
                listings=[], title=title, retry=False,
                error=f"Could not read the listings on {record['url']}: {exc}",
            )

    # -- the decisions, in order --

    async def _ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict:
        if self._classifier is None:
            raise TypeSafeNotConfigured(
                "Reading this site needs the TypeSafe Classifier (e.g. Jev), and none is set up."
            )
        return await self._classifier.ask(state, questions)

    async def _read(self, probe: dict, n: int, title: str, record: dict,
                    clicked: str | None = None) -> CardPage:
        groups = [g for g in probe.get("groups") or [] if isinstance(g, dict)]
        chosen = await self._choose_groups(groups, n, title, record)
        if not chosen:
            # On page 1 the source fails; on a later page — reached through a
            # next-page link that was judged real — the sweep stops there and
            # keeps the pages before it, saying why (see ScrapeService).
            message = record["listing_links"].get("error") or (
                f"Found no list of businesses for sale on {record['url']}"
                + (f" (page {n})." if n > 1 else ".")
            )
            return CardPage(listings=[], title=title, error=message, retry=False)

        cards = _cards_of(chosen)
        urls = frozenset(c.url for c in cards)
        if clicked and urls and urls == self._last_urls:
            # The click did not take: this is the page before, read again.
            # Taken for the end of the list it would end paging with nothing
            # said, and every page after it would go unread.
            message = (f"Page {n} showed the same {len(urls)} listings as page {n - 1}: "
                       f"clicking the next-page control ({clicked}) did not change the page.")
            record["error"] = message
            return CardPage(listings=[], title=title, error=message, retry=False)
        self._last_urls = urls
        listing_hrefs = {c.href for c in cards} | {c.url for c in cards}
        fields_task = self._decide_fields(cards, title, record)
        next_task = self._decide_next(probe, n, title, listing_hrefs, record)
        results = await asyncio.gather(fields_task, next_task, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        roles, unavailable = results[0]
        self._next = results[1]

        listings: list[Listing] = []
        dropped = 0
        for card in cards:
            if any(card.values.get(key, "") in unavailable
                   for key, (role, _) in roles.items() if role == "status"):
                dropped += 1
                continue
            listings.append(self._listing(card, roles))
        record["cards"] = len(cards)
        record["kept"] = len(listings)
        record["dropped_unavailable"] = dropped
        return CardPage(listings=listings, title=title, seen_urls=[c.url for c in cards])

    async def _choose_groups(self, groups: list[dict], n: int, title: str,
                             record: dict) -> list[dict]:
        """The group(s) that are the page's list of businesses for sale; [] for none.

        An override decides on every page. Otherwise the first page asks the
        classifier, and the pages after it read the same patterns without
        asking (`_reuse`) — unless a page does not have them, which is then
        decided afresh, and that decision is reused from then on.
        """
        if self.override and self.override.listing_links:
            wanted = self.override.listing_links
            chosen = [g for g in groups if g.get("pattern") in wanted and g.get("cards")]
            replaced, left_out, action = None, 0, None
            if len(chosen) == 1 and _looks_like_action(chosen[0]):
                # One action pattern pinned is read as when the classifier
                # picks it: through the detail links inside its cards. Never
                # widened to more of the list — that is what pinning is for.
                action = chosen[0]["pattern"]
                chosen, _, replaced, left_out = _whole_list(chosen[0], self._pool(groups),
                                                            widen=False)
            if chosen and self._decided is None:
                self._decided = _decided(n, chosen, action, groups)
            elif chosen and n > self._decided.page:
                chosen = _as_before(chosen, self._decided)
            record["listing_links"] = {"by": "override", "patterns": [g["pattern"] for g in chosen]}
            if replaced:
                record["listing_links"]["instead_of"] = replaced
            if left_out:
                record["listing_links"]["left_out"] = left_out
            if not chosen:
                on_page = [g.get("pattern") for g in _candidates(groups)]
                record["listing_links"]["error"] = (
                    f"None of the listing link patterns in the site override for {self.site} "
                    f"({', '.join(wanted)}) matched a list of links on {record['url']}"
                    + (f"; the page's biggest groups are: {', '.join(on_page[:5])}." if on_page
                       else ".")
                )
            return chosen

        missing: dict[str, Any] | None = None
        decided = self._decided
        if decided is not None and n > decided.page:
            chosen = self._reuse(groups, decided, record)
            if chosen:
                return chosen
            # Not this page's layout (a redesign, an A/B page): decided afresh,
            # fields too, and what is decided here is reused next.
            missing = {"decided_on_page": decided.page, "patterns": decided.pinned}
            logger.info("generic: page %d of %s does not have page %d's list (%s); deciding "
                        "it afresh", n, self.url, decided.page, ", ".join(decided.pinned))
            self._forget()

        candidates = _candidates(groups)
        if not candidates:
            record["listing_links"] = {"by": "probe", "patterns": [], "candidates": 0}
            if missing:
                record["listing_links"]["missing"] = missing
            return []
        options = {f"group_{_letter(i)}": g for i, g in enumerate(candidates)}
        criteria = {
            name: (f"{g['links']} links like {g['pattern']}; examples: "
                   + " || ".join(str(e)[:_GROUP_EXAMPLE_CHARS] for e in (g.get("examples") or [])[:3]))
            for name, g in options.items()
        }
        criteria["none"] = GROUP_NONE
        state = {"page_title": title, "page_url": record["url"]}
        answer = _choice(await self._ask(
            state, {"listing_group": {"type": "choice", "instructions": GROUP_QUESTION,
                                      "criteria": criteria}}), "listing_group")
        picked = options.get(answer.choice)
        chosen, same, replaced, left_out = (_whole_list(picked, candidates) if picked
                                            else ([], [], None, 0))
        record["listing_links"] = {
            "by": "jev", "patterns": [g["pattern"] for g in chosen],
            "confidence": round(answer.confidence, 3), "model": answer.model,
            "candidates": len(candidates),
        }
        if same:
            record["listing_links"]["same_list"] = same
        if replaced:
            record["listing_links"]["instead_of"] = replaced
        if left_out:
            record["listing_links"]["left_out"] = left_out
        if missing:
            record["listing_links"]["missing"] = missing
        if chosen:
            action = picked["pattern"] if picked and _looks_like_action(picked) else None
            self._decided = _decided(n, chosen, action, groups)
        return chosen

    def _pool(self, groups: list[dict]) -> list[dict]:
        """Where an action group's detail links are looked for: the patterns an
        earlier page read them through first (however few links they have
        here), then the page's candidates."""
        decided = self._decided
        mine = [] if decided is None else [
            g for p in decided.patterns if p != decided.action
            for g in groups if g.get("pattern") == p and g.get("cards")]
        return mine + [g for g in _candidates(groups) if all(g is not m for m in mine)]

    def _reuse(self, groups: list[dict], decided: _Decided, record: dict) -> list[dict]:
        """The groups of `decided`'s list on this page, read the way it was read; [] when
        the page does not have them.

        A pattern whose card shape is known counts only when at least one of
        its cards here has that shape: the same link pattern around other
        tiles is another page layout, which is decided afresh.
        """
        pinned = set(decided.pinned)
        here: dict[str, dict] = {}
        for g in groups:
            pattern = g.get("pattern")
            if pattern not in pinned or pattern in here or not g.get("cards"):
                continue
            if decided.shapes.get(pattern) and not any(c.get("shaped") for c in g["cards"]):
                continue
            here[pattern] = g
        replaced, left_out = None, 0
        if decided.action:
            picked = here.get(decided.action)
            if picked is None:
                return []
            chosen, _, replaced, left_out = _whole_list(picked, self._pool(groups), widen=False)
        else:
            chosen = [here[p] for p in decided.patterns if p in here]
        if not chosen:
            return []
        chosen = _as_before(chosen, decided)
        record["listing_links"] = {
            "by": f"page {decided.page}", "patterns": [g["pattern"] for g in chosen],
            "cards_by_shape": sum(1 for g in chosen for c in g.get("cards") or []
                                  if c.get("shaped")),
        }
        if replaced:
            record["listing_links"]["instead_of"] = replaced
        if left_out:
            record["listing_links"]["left_out"] = left_out
        return chosen

    async def _decide_fields(self, cards: list[_Card], title: str,
                             record: dict) -> tuple[dict[str, tuple[str, float]], set[str]]:
        """What each common field holds (role, confidence), then which statuses
        a site override drops.

        A key an earlier page of this attempt already decided keeps that
        answer, on however few cards it appears here; only the rest are asked
        about, and when there is no rest, nothing is asked.
        """
        n = int(record.get("page") or 1)
        keys = _field_keys(cards, set(self._known_fields))
        roles: dict[str, tuple[str, float]] = {}
        report: list[dict] = []
        ask: dict[str, str] = {}
        pinned = self.override.fields if self.override else {}
        for key, labeled in keys:
            rule = _pinned(pinned, key)
            if rule is not None:
                report.append({"key": key, "labeled": labeled, "role": rule, "by": "override"})
                self._known_fields.setdefault(key, {"role": rule, "by": "override"})
                if rule != "ignore":
                    roles[key] = (rule, 1.0)
                continue
            before = self._known_fields.get(key)
            if before is not None and before.get("by") != "override":
                report.append({"key": key, "labeled": labeled, "role": before["role"],
                               "by": f"page {before['page']}",
                               "confidence": round(before["confidence"], 3),
                               "used": before["used"]})
                if before["used"]:
                    roles[key] = (before["role"], before["confidence"])
                continue
            ask[f"field_{len(ask) + 1}"] = key

        if ask:
            labeled_keys = dict(keys)
            fields_state = {}
            questions = {}
            for name, key in ask.items():
                values = [c.values[key] for c in cards if c.values.get(key)]
                fields_state[name] = {
                    "text_just_before_this_field": key if labeled_keys[key] else None,
                    "values_on_some_cards": _spread(values, FIELD_SAMPLES, _FIELD_VALUE_CHARS),
                }
                criteria = ROLES
                if _looks_like_money(values):
                    criteria = {r: ROLES[r] for r in (*MONEY_ROLES, "other")}
                questions[name] = {"type": "choice", "criteria": dict(criteria),
                                   "instructions": FIELD_QUESTION.format(field=name)}
            example = str(cards[0].raw.get("text") or "")[:_CARD_EXAMPLE_CHARS]
            state = {"site": title, "example_listing_card": example, "fields": fields_state}
            answers = await self._ask(state, questions)
            for name, key in ask.items():
                answer = _choice(answers, name)
                used = answer.confidence >= FIELD_CONFIDENCE
                report.append({"key": key, "labeled": labeled_keys[key], "role": answer.choice,
                               "by": "jev", "confidence": round(answer.confidence, 3),
                               "used": used})
                self._known_fields[key] = {"role": answer.choice, "by": "jev", "page": n,
                                           "confidence": answer.confidence, "used": used}
                if used:
                    roles[key] = (answer.choice, answer.confidence)
        record["fields"] = report

        unavailable = await self._decide_status(cards, roles, record)
        return roles, unavailable

    async def _decide_status(self, cards: list[_Card], roles: dict[str, tuple[str, float]],
                             record: dict) -> set[str]:
        """The status values on this page a site override's `drop_status` drops.

        Nothing is asked: whether a card is a business for sale now (not sold,
        pending or under contract) is part of the one request the sweep makes
        per card. The override is a person's deterministic rule for one site —
        a value containing any of its words (case-insensitive) is dropped
        before that request is made.
        """
        drop = self.override.drop_status if self.override else None
        status_keys = [k for k, (role, _) in roles.items() if role == "status"]
        if drop is None or not status_keys:
            return set()
        values = _distinct([c.values[k] for c in cards for k in status_keys if c.values.get(k)],
                           MAX_STATUS_VALUES, None)
        if not values:
            return set()
        needles = [d.lower() for d in drop]
        gone = {v for v in values if any(d in v.lower() for d in needles)}
        record["status"] = {"by": "override", "unavailable": sorted(gone), "values": values}
        return gone

    async def _decide_next(self, probe: dict, n: int, title: str, listing_hrefs: set[str],
                           record: dict) -> _Next | None:
        """How to reach page n+1 from this page, or None when there is no next page."""
        rule = self.override.next_page if self.override else None
        if rule is not None:
            record["next_page"] = {"by": "override", "rule": rule}
            if rule == "none":
                return None
            if rule.startswith("click:"):
                return _Next("click", rule[len("click:"):].strip())
            return _Next("goto", rule.replace("{page}", str(n + 1)))

        here = str(probe.get("url") or "")
        candidates = [
            c for c in probe.get("pager") or []
            if isinstance(c, dict) and not (c.get("url") and (c["url"] in listing_hrefs
                                                             or c["url"] == here))
        ][:MAX_NEXT_LINKS]
        if not candidates:
            record["next_page"] = {"by": "probe", "rule": "none", "candidates": 0}
            return None
        links = {f"link_{i}": c for i, c in enumerate(candidates, 1)}
        state = {
            "current_page_url": here,
            "page_title": title,
            "links": {name: {"url": c.get("url") or _SCRIPT_LINK,
                             "appears_as": list(c.get("appears_as") or [])}
                      for name, c in links.items()},
        }
        answers = await self._ask(state, {
            name: {"type": "noul", "instructions": NEXT_QUESTION.format(link=name, page=n + 1)}
            for name in links
        })
        scored = [(_noul(answers, name), name) for name in links]
        best_p, best = max(scored, key=lambda s: s[0])
        chosen = links[best]
        step: _Next | None = None
        refused = ""
        if best_p >= NEXT_MIN:
            if chosen.get("url"):
                # The address came from the page, so it is checked here before
                # the browser is sent to it: a web address, on this site.
                target = listing_url(str(chosen["url"]))
                refused = _off_site(target, self.host)
                if not refused:
                    step = _Next("goto", target)
            else:
                step = _Next("click", f'[data-cbs-next="{chosen["id"]}"]',
                             selector=None if chosen.get("numbered") else chosen.get("selector"),
                             appears_as=tuple(chosen.get("appears_as") or ()))
        decided: dict[str, Any] = {
            "by": "jev",
            "rule": "none" if step is None else step.target if step.kind == "goto" else "click",
            "probability": round(best_p, 3),
            "candidates": len(candidates),
        }
        if refused:
            decided["refused"] = {"url": str(chosen["url"])[:300], "why": refused}
            logger.warning("generic: next page %s on %s not followed: %s",
                           str(chosen["url"])[:300], self.url, refused)
        if step is not None and step.kind == "click":
            decided["appears_as"] = list(chosen.get("appears_as") or [])
            # A selector someone could pin: only for a Next-style control, never
            # for a page number, which would mean the same page every time.
            if chosen.get("selector") and not chosen.get("numbered"):
                decided["selector"] = chosen["selector"]
        record["next_page"] = decided
        return step

    # -- building the result --

    def _listing(self, card: _Card, roles: dict[str, tuple[str, float]]) -> Listing:
        filled: dict[str, str] = {}
        best: dict[str, float] = {}
        # For each Listing field, the most confident field of that role that has
        # a value on this card.
        for key, (role, confidence) in roles.items():
            target = _LISTING_FIELD.get(role)
            value = card.values.get(key, "").strip()
            if not target or not value or confidence <= best.get(target, -1.0):
                continue
            if role in MONEY_ROLES:
                value = _strip_money_label(value)
            filled[target] = value
            best[target] = confidence
        if not filled.get("title"):
            filled["title"] = _fallback_title(card.raw) or _described_title(card, roles)
        return Listing(
            listing_id="",
            url=card.url,
            normalized_url=card.normalized_url,
            title=filled.get("title", ""),
            location=filled.get("location", ""),
            asking_price=filled.get("asking_price", ""),
            revenue=filled.get("revenue", ""),
            cashflow=filled.get("cashflow", ""),
            ebitda=filled.get("ebitda", ""),
            excerpt=_excerpt(str(card.raw.get("excerpt") or card.raw.get("text") or "")),
            source=self.site,
        )

    def _blocked(self, probe: dict) -> bool:
        title = str(probe.get("title") or "")
        body = str(probe.get("body") or "")
        if text_contains_blocker(title):
            return True
        if not text_contains_blocker(body):
            return False
        small = int(probe.get("body_chars") or len(body)) < _BLOCK_BODY_CHARS
        return small or not _candidates([g for g in probe.get("groups") or [] if isinstance(g, dict)])

    # -- diagnostics --

    def suggested_override(self) -> dict[str, Any]:
        """A paste-ready `SiteOverride` that pins what this attempt decided.

        Built from page 1 (where every decision is made from the fullest page)
        plus any status values an override dropped on later pages. Parts
        nothing was decided about are left out, so pasting it pins exactly what
        was seen.
        """
        out: dict[str, Any] = {"match": self.site}
        first = next((d for d in self.decisions if d.get("listing_links")), None)
        if first is None:
            return out
        links = first["listing_links"]
        patterns = list(links.get("patterns") or [])
        # Detail links read only inside an action group's cards are pinned as
        # that action pattern, which is read the same way (`_whole_list`):
        # the detail pattern pinned would bring back the menu links of its shape.
        if links.get("left_out") and links.get("instead_of"):
            patterns = [links["instead_of"]]
        if patterns:
            out["listing_links"] = patterns
        fields = {f["key"]: (f["role"] if f.get("by") == "override" or f.get("used") else "ignore")
                  for f in first.get("fields") or []}
        if fields:
            out["fields"] = fields
        nxt = first.get("next_page")
        if nxt:
            rule = str(nxt.get("rule") or "")
            if nxt.get("by") == "override":
                out["next_page"] = rule
            elif rule.startswith("http"):
                template = _page_template(rule, first.get("page", 1) + 1)
                if template:
                    out["next_page"] = template
            elif rule == "click" and nxt.get("selector"):
                out["next_page"] = f"click:{nxt['selector']}"
            elif rule == "none" and nxt.get("by") == "jev":
                out["next_page"] = "none"
        # Only statuses judged gone: pinning an empty list would mean "never
        # drop anything", which is not what seeing only live listings showed.
        gone: list[str] = []
        for d in self.decisions:
            for v in (d.get("status") or {}).get("unavailable") or []:
                if v not in gone:
                    gone.append(v)
        if gone:
            out["drop_status"] = gone
        return out


# ── helpers ──────────────────────────────────────────────────────────────────


async def _run_probe(page, args: dict) -> Any:
    """JS_PROBE's answer, parsed; raises asyncio.TimeoutError after PROBE_TIMEOUT_S."""
    raw = await asyncio.wait_for(page.evaluate(JS_PROBE, args), PROBE_TIMEOUT_S)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _try_click(page, selector: str, url: str, *, exactly_one: bool = False) -> bool:
    """Click the first element `selector` finds; False when there is none, or —
    with `exactly_one` — when it is not exactly one visible element, or the
    click fails (detached, covered, re-rendered)."""
    try:
        target = page.locator(selector)
        count = await target.count()
        if not count or (exactly_one and (count != 1 or not await target.first.is_visible())):
            logger.info("generic: next-page control %s is not there on %s (%d found)",
                        selector, url, count)
            return False
    except Exception as exc:  # noqa: BLE001 — the caller tries the next way to find it
        logger.info("generic: could not find %s on %s: %s", selector, url, exc)
        return False
    # A control on a page that never stops moving (an infinite scroll still
    # loading, an animated sticky bar) fails Playwright's "stable" check on
    # every try although it is there and works (Synergy's Load more). The
    # humanized click comes first; then the same click without the waits;
    # then the element's own click(), which fires its handlers without the
    # pointer at all.
    ways = (
        ("click", lambda: target.first.click(timeout=15_000)),
        ("forced click", lambda: target.first.click(force=True, timeout=5_000)),
        ("script click", lambda: target.first.evaluate("(el) => el.click()")),
    )
    for how, attempt in ways:
        try:
            await attempt()
        except Exception as exc:  # noqa: BLE001 — the next way, then the caller's next way
            logger.info("generic: could not %s %s on %s: %s", how, selector, url, exc)
            continue
        if how != "click":
            logger.info("generic: %s worked for %s on %s", how, selector, url)
        return True
    return False


async def _page_size(page) -> list | None:
    """(links on the page, document height, address, a hash of the links) —
    None when it cannot be read."""
    try:
        size = await page.evaluate(_JS_PAGE_SIZE)
    except Exception:  # noqa: BLE001 — then there is nothing to wait on
        return None
    return size if isinstance(size, list) and len(size) >= 3 else None


def _moved(before: list, now: Any) -> bool:
    """Whether the page shows more, or other, than it did at `before`."""
    if not (isinstance(now, list) and len(now) >= 3):
        return False
    return (now[0] > before[0] or now[1] > before[1] or now[2] != before[2]
            or (len(now) > 3 and len(before) > 3 and now[3] != before[3]))


async def _unchanged(page, before: list | None) -> bool:
    """True only when the page can be measured and shows what it did at `before`."""
    if before is None:
        return False
    try:
        now = await page.evaluate(_JS_PAGE_SIZE)
    except Exception:  # noqa: BLE001 — navigating: the page is not the one measured
        return False
    return isinstance(now, list) and len(now) >= 3 and not _moved(before, now)


async def _loaded(page) -> None:
    """Wait for a navigation a click started; one that loads in place never fires it."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception:  # noqa: BLE001 — a click that loads in place never fires it
        pass


async def _settle(page, before: list | None, url: str) -> bool:
    """After a click, wait (at most CLICK_SETTLE_S) for the page to show more:
    more links, a taller document, another address or other links. True when
    it did.

    A "Load more" fetches its cards after the click returns; reading the page
    before they arrive would find only the cards already seen, which ends
    paging. Without a first measurement there is nothing to compare, so no wait.
    """
    if before is None:
        return False
    deadline = time.monotonic() + CLICK_SETTLE_S
    while time.monotonic() < deadline:
        try:
            await page.wait_for_timeout(CLICK_POLL_MS)
            now = await page.evaluate(_JS_PAGE_SIZE)
        except Exception:  # noqa: BLE001 — the click navigated: the page is new
            return True
        if _moved(before, now):
            return True
    logger.info("generic: nothing new appeared on %s within %.0f s of the next-page click",
                url, CLICK_SETTLE_S)
    return False


async def _scroll_through(page) -> int:
    """Scroll to the bottom in steps, then back to the top; the steps taken.

    Stops at a bottom that did not grow during the last step, after
    SCROLL_MAX_STEPS steps, or once SCROLL_BUDGET_S has passed. Scrolling is a
    help, not a requirement: a page that cannot be scrolled is read as it is.
    """
    started = time.monotonic()
    steps = 0
    try:
        state = await page.evaluate(_JS_SCROLL_STATE)
        if not state:
            return 0
        height = state[2]
        await page.mouse.move(640, 400)
        while steps < SCROLL_MAX_STEPS and time.monotonic() - started < SCROLL_BUDGET_S:
            await page.mouse.wheel(0, SCROLL_STEP_PX)
            await page.wait_for_timeout(SCROLL_PAUSE_MS)
            steps += 1
            state = await page.evaluate(_JS_SCROLL_STATE)
            if not state:
                break
            top, view, grown = state
            if top + view >= grown - 2 and grown == height:
                await page.wait_for_timeout(SCROLL_BOTTOM_SETTLE_MS)
                state = await page.evaluate(_JS_SCROLL_STATE)
                if not state or state[2] == height:
                    break
                grown = state[2]
            height = grown
        await page.evaluate(_JS_SCROLL_TOP)
    except Exception as exc:  # noqa: BLE001 — an unscrollable page is still read
        logger.info("generic: could not scroll %s: %s", getattr(page, "url", ""), exc)
    return steps


def _candidates(groups: list[dict]) -> list[dict]:
    """The groups worth offering as the page's list, biggest first."""
    usable = [g for g in groups
              if int(g.get("links") or 0) >= MIN_GROUP_LINKS
              and float(g.get("chrome") or 0.0) < MAX_CHROME
              and g.get("cards")]
    usable.sort(key=lambda g: -int(g.get("links") or 0) * int(g.get("text_chars") or 0))
    return usable[:MAX_CANDIDATES]


def _decided(page: int, chosen: list[dict], action: str | None,
             groups: list[dict]) -> _Decided:
    """What later pages reuse of this page's list: its patterns, how it was read,
    each pattern's card shape (from the cards actually read) and what in its
    links names a listing."""
    shapes: dict[str, dict[str, Any]] = {}
    identity: dict[str, dict[str, Any]] = {}
    for g in [*chosen, *(g for g in groups if action and g.get("pattern") == action)]:
        shape = _shape_of(g.get("cards") or [])
        if shape and g["pattern"] not in shapes:
            shapes[g["pattern"]] = shape
        identity.setdefault(g["pattern"], {
            "varying_keys": list(g.get("varying_keys") or []),
            "paths_unique": bool(g.get("paths_unique", True)),
        })
    return _Decided(page=page, patterns=tuple(g["pattern"] for g in chosen), action=action,
                    shapes=shapes, identity=identity)


def _as_before(groups: list[dict], decided: _Decided | None) -> list[dict]:
    """`groups` with what an earlier page learned about their links' identity.

    A query key that named the listing there still does here, and paths that
    were shared there still are, however few links this page has to show it.
    """
    if decided is None:
        return groups
    out = []
    for g in groups:
        before = decided.identity.get(str(g.get("pattern") or ""))
        if before:
            keys = list(dict.fromkeys([*(g.get("varying_keys") or []), *before["varying_keys"]]))
            g = {**g, "varying_keys": keys,
                 "paths_unique": bool(g.get("paths_unique", True)) and before["paths_unique"]}
        out.append(g)
    return out


def _shape_of(cards: list[dict]) -> dict[str, Any] | None:
    """The shape most of these cards have (`card`), every shape they have
    (`cards`, the commonest first), and the element most of the commonest sit
    in (`container`).

    None when no shape is on at least half the cards: then there is no one
    tile to look for. The container is left out (None) the same way. Every
    shape is kept, not only the commonest, because the listing a later page is
    left with may be the odd one out ("new", "featured").
    """
    shapes = collections.Counter(str(c["shape"]) for c in cards if c.get("shape"))
    if not shapes:
        return None
    card, count = shapes.most_common(1)[0]
    if count * 2 < len(cards):
        return None
    homes = collections.Counter(str(c.get("container") or "") for c in cards
                                if c.get("shape") == card)
    home, most = homes.most_common(1)[0]
    return {"card": card, "cards": [s for s, _ in shapes.most_common(_MAX_SHAPES)],
            "container": home if home and most * 2 >= count else None}


def _whole_list(picked: dict, candidates: list[dict], *,
                widen: bool = True) -> tuple[list[dict], list[str], str | None, int]:
    """The chosen group and the rest of its list, read through detail links.

    The classifier picks one group, but a list is not always one group:

    * **One list, two link shapes** (Sunbelt: half its cards link to
      `/business-search/business-details/…`, half to `/<office>/…/listing-details/…`).
      A candidate whose cards are the same tile (`card_shape`), in the same
      element, with the same fields, is the rest of the list, and is read with
      it — decided here, without asking again.
    * **An action link** ("Watch", "Unlock Listing", "Contact seller") on every
      card is a group of its own, and can be the one picked (Flippa's
      `watch_item?…`, whose every "title" was "Watch"). When another group
      links the same cards, those links are the listings' addresses and
      titles; the action group only adds what they do not cover. Only the
      detail links *inside* the action group's cards are read, one per card
      (`_inside`): the rest of that group is whatever else on the site has the
      same URL shape — BusinessesForSale's `/us/{*}` is its listings and also
      "Sell Your Business", "Login" and "FAQs" in its menu.

    `widen=False` skips the first (a pinned pattern is the whole list).

    Returns (groups to read, patterns read as the same list, the picked pattern
    when it was an action link replaced by the detail links, and how many
    detail links were left out for being outside the action group's cards).
    """
    same = [g for g in candidates if widen and g is not picked and _one_list(picked, g)]
    groups = [picked, *same]
    left_out = 0
    if _looks_like_action(picked):
        if not any(_covers(g, picked) >= 0.5 for g in groups if not _looks_like_action(g)):
            partner = next((g for g in candidates
                            if g not in groups and not _looks_like_action(g)
                            and _covers(g, picked) >= 0.5), None)
            if partner is not None:
                groups.append(partner)
        groups, left_out = _bounded(groups)
    details = [g for g in groups if not _looks_like_action(g)]
    kept = []
    for g in groups:
        # An action group whose every card a detail group reads adds nothing.
        if _looks_like_action(g) and details and _covers_all(details, g):
            continue
        kept.append(g)
    patterns = [g["pattern"] for g in kept]
    same_patterns = {g["pattern"] for g in same}
    replaced = picked["pattern"] if picked["pattern"] not in patterns else None
    return kept, [p for p in patterns[1:] if p in same_patterns], replaced, left_out


def _bounded(groups: list[dict]) -> tuple[list[dict], int]:
    """Each detail group cut down to its links inside the action groups' cards.

    Returns the groups (a detail group with nothing left inside is dropped) and
    how many detail cards were left out. Without an action group, or without a
    detail group, the groups are returned as they are.
    """
    actions = [g for g in groups if _looks_like_action(g)]
    if not actions or len(actions) == len(groups):
        return groups, 0
    out: list[dict] = []
    left_out = 0
    for g in groups:
        if not _looks_like_action(g):
            inside = _inside(g, actions)
            left_out += len(g.get("cards") or []) - len(inside["cards"])
            g = inside
        if g.get("cards"):
            out.append(g)
    return out, left_out


def _inside(group: dict, actions: list[dict]) -> dict:
    """`group` with only its cards inside the action groups' cards, one per card.

    A detail card is inside an action card when it is the same element, links
    to the action link, or is linked from it (`_linked`). The first such detail
    card (page order) is the one an action card is read through; another
    detail link in the same action card is not a second listing.
    """
    owners = [_index_of([card]) for a in actions for card in a.get("cards") or []]
    taken: set[int] = set()
    cards = []
    for card in group.get("cards") or []:
        hits = {i for i, owner in enumerate(owners) if _linked(card, *owner)}
        if not hits or hits <= taken:
            continue
        taken |= hits
        cards.append(card)
    return {**group, "cards": cards, "links": len(cards)}


def _one_list(group: dict, other: dict) -> bool:
    """Whether `other`'s cards are more of `group`'s list: same tile, same place, same fields."""
    shape = group.get("card_shape")
    if not shape or other.get("card_shape") != shape:
        return False
    if not set(group.get("containers") or []) & set(other.get("containers") or []):
        return False
    mine, theirs = _template(group), _template(other)
    if not mine or not theirs:
        return False
    return len(mine & theirs) / len(mine | theirs) >= SAME_LIST_FIELDS


def _template(group: dict) -> set[str]:
    """The field keys (labels and slots) on at least half of a group's cards."""
    cards = group.get("cards") or []
    counts: dict[str, int] = {}
    for card in cards:
        for bucket in ("labeled", "slots"):
            for key in card.get(bucket) or {}:
                counts[key] = counts.get(key, 0) + 1
    return {k for k, n in counts.items() if n * 2 >= len(cards)}


# Words that make a link an action on a listing rather than its page, at the
# start of a link's text or as a literal word of its pattern.
_ACTION = re.compile(
    r"(?<![a-z])(?:(?:un)?watch(?:list)?|unlock|contact|save|share|compare|favou?rites?|follow|"
    r"bookmark|wishlist|register|(?:log|sign)[\s_-]?(?:in|on|up))(?![a-z])",
    re.IGNORECASE,
)


def _looks_like_action(group: dict) -> bool:
    """Whether a group's links do something to a listing rather than show it.

    Judged by the literal words of its pattern ("watch_item", "/unlock/") and
    by its link text when most cards share it ("Watch", "Contact Seller") —
    never by a wildcard's values or a title, which may say anything
    ("Save-A-Lot Grocery").
    """
    path = str(group.get("pattern") or "").split("?", 1)[0].partition("/")[2]
    if _ACTION.search(path):
        return True
    texts = [str(c.get("link_text") or "").strip() for c in group.get("cards") or []]
    if not texts:
        return False
    common, count = collections.Counter(texts).most_common(1)[0]
    return count * 2 > len(texts) and len(common) <= 40 and bool(_ACTION.match(common))


def _linked(card: dict, ids: set[str], hrefs: set[str], links: set[str]) -> bool:
    """Whether `card` is one of the cards described by (ids, hrefs, links): the
    same element, or a card that links to one of them or is linked from one."""
    if card.get("card") and card["card"] in ids:
        return True
    return card.get("href") in links or bool(set(card.get("hrefs") or []) & hrefs)


def _index(groups: list[dict]) -> tuple[set[str], set[str], set[str]]:
    return _index_of([c for g in groups for c in g.get("cards") or []])


def _index_of(cards: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """(card ids, card links, every link inside the cards) — what `_linked` matches."""
    ids: set[str] = set()
    hrefs: set[str] = set()
    links: set[str] = set()
    for c in cards:
        if c.get("card"):
            ids.add(c["card"])
        if c.get("href"):
            hrefs.add(c["href"])
        links.update(c.get("hrefs") or [])
    return ids, hrefs, links


def _covers(group: dict, other: dict) -> float:
    """The share of `other`'s cards that are also `group`'s cards."""
    cards = other.get("cards") or []
    if not cards:
        return 0.0
    index = _index([group])
    return sum(1 for c in cards if _linked(c, *index)) / len(cards)


def _covers_all(groups: list[dict], other: dict) -> bool:
    index = _index(groups)
    return all(_linked(c, *index) for c in other.get("cards") or [])


def _letter(i: int) -> str:
    """A, B, … Z, AA, AB, … — option names that carry no ranking."""
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def _choice(answers: dict, name: str) -> Choice:
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, Choice):
        raise TypeSafeError(f"The TypeSafe Classifier did not answer the question {name!r}.")
    return answer


def _noul(answers: dict, name: str) -> float:
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, Noul):
        raise TypeSafeError(f"The TypeSafe Classifier did not answer the question {name!r}.")
    return answer.probability


def _cards_of(groups: list[dict]) -> list[_Card]:
    """The chosen groups' cards as one list, each card once, with its address.

    A card can belong to two chosen groups (a listing link and an "unlock" or
    "contact" link in the same tile); it is one listing. When one of the groups
    is an action (`_looks_like_action`), the card is read through the other
    group's link — its detail page, and the title that link carries; otherwise
    under the shorter link. Its address prefers a link in the same card whose
    path is a prefix of the group link's (`…/listing/5` over
    `…/listing/5/contact`), when that link belongs to this card alone. Cards
    come back in page order when the probe said where they were.
    """
    everywhere: dict[str, int] = {}
    for g in groups:
        for c in g.get("cards") or []:
            for h in set(c.get("hrefs") or []):
                everywhere[h] = everywhere.get(h, 0) + 1
    acting = [_looks_like_action(g) for g in groups]
    details = _index([g for g, act in zip(groups, acting) if not act])

    by_card: dict[str, _Card] = {}
    order: list[str] = []
    for g, act in zip(groups, acting):
        keep = list(g.get("varying_keys") or []) if not g.get("paths_unique", True) else []
        for c in g.get("cards") or []:
            if act and _linked(c, *details):
                continue  # read through its detail link instead
            href = str(c.get("href") or "")
            if g.get("paths_unique", True):
                href = _detail_href(href, c.get("hrefs") or [], everywhere)
            url = listing_url(href)
            if not url:
                continue
            card = _Card(raw=c, href=str(c.get("href") or ""), url=url,
                         normalized_url=normalize_url(href, keep_query=keep) or "",
                         values=_values_of(c))
            ident = str(c.get("card") or url)
            prior = by_card.get(ident)
            if prior is None:
                by_card[ident] = card
                order.append(ident)
            elif len(card.url) < len(prior.url):
                by_card[ident] = card

    seen: set[str] = set()
    out: list[_Card] = []
    for ident in order:
        card = by_card[ident]
        if card.url in seen:
            continue
        seen.add(card.url)
        out.append(card)
    if len(groups) > 1 and all(isinstance(c.raw.get("pos"), int) for c in out):
        out.sort(key=lambda c: c.raw["pos"])
    return out


def _detail_href(href: str, hrefs: list[str], everywhere: dict[str, int]) -> str:
    p = urlparse(href)
    path = p.path.rstrip("/")
    best, best_len = href, len(path)
    for other in hrefs:
        q = urlparse(other)
        other_path = q.path.rstrip("/")
        if q.netloc != p.netloc or not other_path or other_path == path:
            continue
        if everywhere.get(other, 0) > 1:  # shared by several cards: an index, not a listing
            continue
        if path.startswith(other_path + "/") and len(other_path) < best_len:
            best, best_len = other, len(other_path)
    return best


def _values_of(card: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for bucket in ("labeled", "slots"):
        for key, value in (card.get(bucket) or {}).items():
            if isinstance(value, str) and value.strip() and key not in values:
                values[key] = value.strip()
    return values


def _field_keys(cards: list[_Card], known: set[str] = frozenset()) -> list[tuple[str, bool]]:
    """The fields common enough to ask about: (key, is it a label), card order.

    A field is common when at least FIELD_MIN_CARDS cards have it (every card,
    on a page with fewer). A key an earlier page of the sweep decided (`known`)
    counts on any card that has it: its meaning is settled, and a last page
    with one card still needs its price read.
    """
    counts: dict[str, int] = {}
    labeled: dict[str, bool] = {}
    for card in cards:
        for key in (card.raw.get("labeled") or {}):
            labeled.setdefault(key, True)
        for key in (card.raw.get("slots") or {}):
            labeled.setdefault(key, False)
        for key, value in card.values.items():
            counts[key] = counts.get(key, 0) + 1
    need = max(1, min(FIELD_MIN_CARDS, len(cards)))
    common = [k for k in labeled
              if counts.get(k, 0) >= (1 if k in known else need)]
    if len(common) > MAX_FIELDS:
        keep = set(sorted(common, key=lambda k: -counts[k])[:MAX_FIELDS])
        common = [k for k in common if k in keep]
    return [(k, labeled[k]) for k in common]


def _pinned(fields: dict[str, str], key: str) -> str | None:
    if key in fields:
        return fields[key]
    folded = _fold(key)
    for name, rule in fields.items():
        if _fold(name) == folded:
            return rule
    return None


def _fold(key: str) -> str:
    return " ".join(key.replace(":", " ").split()).lower()


def _spread(values: list[str], limit: int, chars: int | None) -> list[str]:
    """Up to `limit` distinct values, taken across the page rather than from its
    top: the first, the last and evenly between, in page order."""
    distinct = _distinct(values, len(values), chars)
    if len(distinct) <= limit:
        return distinct
    last = len(distinct) - 1
    picks = sorted({round(i * last / (limit - 1)) for i in range(limit)})
    return [distinct[i] for i in picks]


def _distinct(values: list[str], limit: int, chars: int | None) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()
        if chars is not None:
            v = v[:chars]
        if v and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out


_MONEY = re.compile(r"(?:US\s*|USD\s*)?[$€£]\s*-?\d")
_DIGIT = re.compile(r"\d")


def _looks_like_money(values: list[str]) -> bool:
    """Mostly short amounts ("$1.2M", "$ 540,166"), among the values with a digit.

    Values with no digit ("Not Disclosed", "N/A", "Sign in to view") say
    nothing either way; a long text that mentions a price is a description.
    """
    money = sum(1 for v in values if len(v) <= 40 and _MONEY.search(v))
    digits = sum(1 for v in values if _DIGIT.search(v))
    return money > 0 and money * 2 >= digits


# A money value's own label, when the card printed both in one line with no
# colon ("Asking Price $1,250,000"). It is furniture, not part of the amount —
# the same call the BizBuySell adapter makes.
_MONEY_LABEL = re.compile(
    r"^\s*(?:asking(?: price)?|price|list price|cash ?flow|sde|seller'?s discretionary earnings|"
    r"ebitda|adj(?:usted)?\.? ebitda|revenue|gross(?: revenue| sales| income)?|sales|"
    r"net (?:profit|income)|(?:adjusted )?earnings|income|profit|turnover)\s*:?\s*(?=[$€£]|US)",
    re.IGNORECASE,
)


def _strip_money_label(value: str) -> str:
    return _MONEY_LABEL.sub("", value, count=1).strip() or value


_CTA = re.compile(r"^(view|read|see|learn|more|details?|click|contact|request|get|open)\b", re.I)


def _fallback_title(card: dict) -> str:
    """The link's own text, else the card's first heading.

    The link text loses to the heading when it holds the heading and more (a
    link wrapping the whole tile), when it is a call to action ("View
    details"), or when it is too long to be a title.
    """
    link = str(card.get("link_text") or "").strip()
    heading = str(card.get("heading") or "").strip()
    usable = 3 <= len(link) <= _TITLE_MAX_CHARS and not _CTA.match(link)
    if heading and (not usable or (heading in link and heading != link)):
        return heading
    return link if usable else heading


def _excerpt(text: str) -> str:
    """`text` cut to EXCERPT_CHARS, at a word where there is one, marked with "…"."""
    text = text.strip()
    if len(text) <= EXCERPT_CHARS:
        return text
    cut = text[: EXCERPT_CHARS - 1]
    space = cut.rfind(" ")
    if space > EXCERPT_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


_DESCRIBED_CHARS = 100


def _described_title(card: _Card, roles: dict[str, tuple[str, float]]) -> str:
    """A title for a card with no name of its own: what it is, and which one.

    Empire Flippers' tiles have no name, heading or titled link ("View
    Listing") — only a number, a niche and a summary — so they are titled by
    their category (or, without one, the start of their description) and their
    listing number: "Apparel & Accessories, Home · #97637". A number alone is
    not a title.
    """
    best: dict[str, tuple[str, float]] = {}
    for key, (role, confidence) in roles.items():
        value = card.values.get(key, "").strip()
        if (role in ("category", "description", "listing_id") and value
                and confidence > best.get(role, ("", -1.0))[1]):
            best[role] = (value, confidence)
    what = best.get("category", ("", 0.0))[0]
    if not what:
        what = best.get("description", ("", 0.0))[0]
        if len(what) > _DESCRIBED_CHARS:
            what = what[:_DESCRIBED_CHARS].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    if not what:
        return ""
    number = best.get("listing_id", ("", 0.0))[0]
    return " · ".join(p for p in (what, number) if p)[:_TITLE_MAX_CHARS]


# Second-level labels under a two-letter country code that are not a site of
# their own ("example.co.uk", "example.com.au"): the registrable domain is one
# label longer there. A heuristic, not the public-suffix list — it only has to
# tell a site's own subdomains from somewhere else.
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go",
                           "ltd", "plc", "gen", "biz"})


def _registrable(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _off_site(url: str | None, host: str) -> str:
    """Why the browser must not follow `url` from a page on `host`; "" when it may.

    It may when `url` is a web address on the same site: the same host, or
    another host under the same registrable domain (www.fcbb.com and
    sfbay.fcbb.com). An IP address only matches itself.
    """
    if not url:
        return "not a web address (only http and https links are followed)"
    other = (urlparse(url).hostname or "").lower()
    here = host.lower()
    if other == here:
        return ""
    for name in (other, here):
        try:
            ipaddress.ip_address(name)
            return f"on another site ({other}), not {here}"
        except ValueError:
            pass
    if _registrable(other) == _registrable(here):
        return ""
    return f"on another site ({other}), not {here}"


def _page_template(url: str, page: int) -> str | None:
    """`url` with its page number replaced by {page}, when exactly one is there."""
    hits = list(re.finditer(rf"(?<!\d){page}(?!\d)", url))
    if len(hits) != 1:
        return None
    m = hits[0]
    return url[: m.start()] + "{page}" + url[m.end():]
