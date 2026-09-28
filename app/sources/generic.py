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
  does not. Next-page candidates are collected the same way.
* **What code cannot see, the TypeSafe Classifier (e.g. Jev) decides**: which
  group is the list of businesses for sale (and not the menu, the footer, or a
  "similar listings" rail), what each field holds, which statuses mean the
  business is gone, and which candidate is really the next page. Each of those
  is ONE request per page — the classifier reads the state once and answers
  every question in it, so a request per field or per link would pay for the
  same reading many times. (Measured on 2026-09-28: bundled answers were as
  accurate as separate ones.)

**Every decision is made fresh on every page**, and nothing is kept between
sweeps: a site that changes its layout is read by its new layout, not with last
month's answers. A person can pin any part of it for one site with a
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
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..models import Listing
from ..services import extract
from ..services.blocker import text_contains_blocker
from ..services.typesafe import Choice, Noul, TypeSafeError, TypeSafeNotConfigured
from . import GENERIC_LABEL, GENERIC_NAME
from .base import CardPage
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
# A card field is asked about when at least this share of cards has it.
FIELD_PRESENCE = 0.3
# A field fills a Listing only at this confidence; below it the field is left
# empty and the excerpt keeps the text.
FIELD_CONFIDENCE = 0.8
# A next-page candidate is followed at this probability or above.
NEXT_MIN = 0.5
# A status value at this probability or above means "no longer available".
UNAVAILABLE_MIN = 0.5
# Bounds on one request, so a strange page cannot build an enormous one.
MAX_FIELDS = 40
MAX_STATUS_VALUES = 20
MAX_NEXT_LINKS = 12
# A page whose text is longer than this is not a bare challenge page, so a
# blocker phrase in its body ("just a moment's walk from the beach") is only
# believed when the page also has no list on it.
_BLOCK_BODY_CHARS = 5000

_GROUP_EXAMPLE_CHARS = 260
_CARD_EXAMPLE_CHARS = 400
_FIELD_VALUE_CHARS = 70
_TITLE_MAX_CHARS = 150

# The questions, exactly as measured on 2026-09-28 (the listing-group question
# picked the right group on 18/18 pages × 3 shuffles, "none" on the pages that
# had no list). The state each one reads is built next to where it is asked.
GROUP_QUESTION = (
    "This page is from a website that lists businesses for sale. The links on the page have "
    "been grouped by URL pattern, and each example is the text of the card or block around one "
    "link in that group. Which group is the page's list of businesses for sale, where each item "
    "is a different business being offered?"
)
GROUP_NONE = "None of these groups is a list of businesses for sale"
FIELD_QUESTION = (
    "Each listing card on this page has the fields in the state. What does {field} hold?"
)
STATUS_QUESTION = (
    "{status} in the state means the business is no longer available: sold, pending, or under "
    "contract"
)
NEXT_QUESTION = (
    "{link} in the state goes to the next page (page {page}) of the same list as the current page."
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
#    /silicon-valley/… on the same site.
# 2. A link's card is its largest ancestor holding no other link of the group
#    (brought back down to its siblings' shape when a neighbouring tile has no
#    link to stop it — Liberty's sold tiles). Its text is split into lines
#    (block elements and <br>), and a line is a label when it ends in ':' or is
#    the same short text on ≥ 80% of cards; a label's value is the next line,
#    when it sits close by. Values are keyed by their label's text — not by
#    position, because cards drop a field when they have no value for it
#    (FCBB), which shifts every position after it — and unlabelled lines by
#    their DOM path.
# 3. Next-page candidates: rel=next, Next/›/»/Load more, and the next page
#    number inside a pager. Each clickable one is marked data-cbs-next="<id>" so
#    a script-only control (href="#", a button) can be clicked from Python.
#
# Called with {next_number, patterns} (the page number to look for in a pager,
# and any pinned listing patterns). Returns JSON:
#   {url, title, body (first 5000 chars), body_chars,
#    groups: [{pattern, links, chrome, text_chars, varying_keys, paths_unique,
#              examples, cards}],   biggest (links × text) first
#    pager:  [{id, url|null, script_only, numbered, appears_as, selector}]}
# where `cards` is null except for the top 12 non-chrome groups and pinned ones:
#   [{card, href, hrefs, link_text, heading, text, excerpt, labeled, slots}]
JS_PROBE = r"""
(args) => {
  const opts = args || {};
  const nextNumber = String(opts.next_number || 2);
  const wanted = new Set(opts.patterns || []);
  const MAX_DETAILED = 12;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const pageUrl = /^https?:/.test(location.href) ? location.href : document.baseURI;
  const here = (() => { try { const u = new URL(pageUrl); u.hash = ''; return u; } catch (_) { return null; } })();
  const isHere = (u) => !!here && u.origin === here.origin && u.pathname === here.pathname && u.search === here.search;

  // Marks from an earlier probe of this same document (a "Load more" click
  // keeps the DOM) would otherwise point at stale elements.
  for (const el of document.querySelectorAll('[data-cbs-next]')) el.removeAttribute('data-cbs-next');
  for (const el of document.querySelectorAll('[data-cbs-card]')) el.removeAttribute('data-cbs-card');

  const TRACKING = /^(utm_.*|gclid|gbraid|wbraid|dclid|fbclid|msclkid|yclid|twclid|igshid|mc_cid|mc_eid|_ga|_gl|_hsenc|_hsmi|mkt_tok|ref|ref_src)$/i;
  const resolve = (raw) => {
    if (raw == null) return null;
    try { const u = new URL(raw, document.baseURI); u.hash = ''; return u; } catch (_) { return null; }
  };

  // ── 1. links, grouped by URL shape ──
  const hrefOf = new Map();
  const byHref = new Map();
  for (const a of document.querySelectorAll('a[href]')) {
    const u = resolve(a.getAttribute('href'));
    if (!u || !/^https?:$/.test(u.protocol)) continue;
    hrefOf.set(a, u.href);
    if (isHere(u)) continue;
    if (!byHref.has(u.href)) byHref.set(u.href, { u, anchors: [] });
    byHref.get(u.href).anchors.push(a);
  }
  const coarse = new Map();
  for (const [href, item] of byHref) {
    const u = item.u;
    const segs = u.pathname.split('/').filter(Boolean).map((s) => s.toLowerCase());
    const keys = [...new Set([...u.searchParams.keys()].filter((k) => !TRACKING.test(k)))].sort();
    const key = `${u.host}|${segs.length}|${keys.join('&')}`;
    if (!coarse.has(key)) coarse.set(key, { host: u.host, n: segs.length, keys, items: [] });
    coarse.get(key).items.push({ href, u, segs, host: u.host, keys: keys.join('&') });
  }
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
      if (rest.length) refine(rest, depth + 1, parts.concat(['{*}']), n, out);
      return;
    }
    refine(items, depth + 1, parts.concat(['{*}']), n, out);
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
            && segs.every((s, i) => s === '{*}' || s === it.segs[i])) items.push(it);
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
  const cardFor = (a, hrefSet, mine) => {
    let node = a;
    for (let p = node.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      let other = false;
      for (const x of p.querySelectorAll('a[href]')) {
        const h = hrefOf.get(x);
        if (h && h !== mine && hrefSet.has(h)) { other = true; break; }
      }
      if (other) break;
      node = p;
    }
    return node;
  };
  // A card that climbed past its siblings' shape (its row-mates have no link
  // of the group — a sold tile with no detail page) is brought back down to
  // the shape most cards have.
  const sig = (el) => el.tagName + '.' + stable(el).sort().join('.');
  const align = (cards) => {
    const counts = new Map();
    for (const c of cards) counts.set(sig(c.el), (counts.get(sig(c.el)) || 0) + 1);
    let modal = null, most = 0;
    for (const [k, n] of counts) if (n > most) { modal = k; most = n; }
    if (most * 2 < cards.length) return;
    for (const c of cards) {
      if (sig(c.el) === modal) continue;
      for (let n = c.anchor; n && n !== c.el; n = n.parentElement) {
        if (sig(n) === modal) { c.el = n; break; }
      }
    }
  };
  const groups = [];
  for (const g of raw) {
    // One link has no neighbour to bound its card, which would grow to the
    // whole page; below three, only a pinned pattern is worth reading.
    if (g.items.length < (wanted.has(g.pattern) ? 2 : 3)) continue;
    const hrefSet = new Set(g.items.map((it) => it.href));
    const cards = g.items.map((it) => {
      const anchor = byHref.get(it.href).anchors[0];
      return { href: it.href, anchor, el: cardFor(anchor, hrefSet, it.href) };
    });
    align(cards);
    let chrome = 0, chars = 0;
    for (const c of cards) {
      c.text = clean(c.el.innerText || c.el.textContent);
      if (inChrome(c.el)) chrome++;
      chars += Math.min(c.text.length, 1000);
    }
    const varying = g.keys.filter((k) => new Set(g.items.map((it) => it.u.searchParams.get(k))).size > 1);
    const paths = new Set(g.items.map((it) => it.u.host + it.u.pathname.replace(/\/+$/, '')));
    groups.push({
      pattern: g.pattern,
      links: g.items.length,
      chrome: +(chrome / cards.length).toFixed(2),
      text_chars: Math.round(chars / cards.length),
      varying_keys: varying,
      paths_unique: paths.size === g.items.length,
      examples: cards.slice(0, 3).map((c) => c.text.slice(0, 300)),
      _cards: cards,
    });
  }
  groups.sort((a, b) => b.links * b.text_chars - a.links * a.text_chars);

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
  const fieldsFor = (cards) => {
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
        if (l.inline && (inlineOn.get(l.inline[0]) || 0) < Math.max(2, 0.3 * cards.length)) l.inline = null;
        l.isLabel = !l.inline && colonLabel(l);
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
      const put = (obj, key, value) => {
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
    const rename = new Map();
    for (const [base, keys] of members) {
      if (keys.size < 2) continue;
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
    const fields = fieldsFor(g._cards);
    return g._cards.map((c, i) => {
      if (!c.el.hasAttribute('data-cbs-card')) c.el.setAttribute('data-cbs-card', `g${gi}c${i}`);
      let linkText = '';
      const own = c.el.matches('a[href]') ? [c.el, ...c.el.querySelectorAll('a[href]')] : [...c.el.querySelectorAll('a[href]')];
      for (const a of own) {
        if (hrefOf.get(a) !== c.href) continue;
        const t = clean(a.innerText || a.textContent) || clean(a.getAttribute('title')) || clean(a.getAttribute('aria-label'));
        if (t.length > linkText.length) linkText = t;
      }
      const h = c.el.querySelector('h1, h2, h3, h4, h5, h6') || c.el.querySelector('[class*=title i]');
      const host = new URL(c.href).host;
      const hrefs = [];
      for (const a of own) {
        const x = hrefOf.get(a);
        if (x && new URL(x).host === host && !hrefs.includes(x)) hrefs.push(x);
        if (hrefs.length >= 20) break;
      }
      let excerpt = c.text;
      try { if (window.__cbsMarkdown) excerpt = window.__cbsMarkdown(c.el.innerHTML).trim(); } catch (_) {}
      return {
        card: c.el.getAttribute('data-cbs-card'),
        href: c.href,
        hrefs,
        link_text: linkText.slice(0, 500),
        heading: h ? clean(h.innerText || h.textContent).slice(0, 500) : '',
        text: c.text.slice(0, 2000),
        excerpt,
        labeled: fields[i].labeled,
        slots: fields[i].slots,
      };
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
  const PAGERISH = '[class*=pag i], [id*=pag i], [aria-label*=pag i], nav, [role=navigation], ul, ol';
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
    const inPager = !!el.closest(PAGERISH);
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
  for (const l of document.querySelectorAll('link[rel~=next][href], a[rel~=next]')) add(l, 'rel=next');
  for (const el of document.querySelectorAll('a, button, [role=button], [role=link]')) {
    if (pager.length >= 30) break;
    const t = clean(el.innerText || el.textContent);
    const aria = clean(el.getAttribute('aria-label') || el.getAttribute('title'));
    if (t && NEXT.test(t)) add(el, `text '${t.slice(0, 40)}'`);
    else if (!t && aria && /\bnext\b|load more|show more/i.test(aria)) add(el, `label '${aria.slice(0, 40)}'`);
    else if (t === nextNumber && el.closest(PAGERISH)) add(el, `text '${t}'`);
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
    """How to reach the next page, decided on the page before it."""

    kind: str  # "goto" | "click"
    target: str  # a URL, or a CSS selector


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
    the next one, a record of every decision — and `begin()` forgets all of it,
    because the sweep reuses one source object across retries.

    `decisions` has one JSON-safe dict per page read, saying what was decided
    and by whom ("override", "jev", or "probe" when there was nothing to ask):
    `page`, `url`; `listing_links` {by, patterns, confidence, model,
    candidates}; `fields` [{key, labeled, role, by, confidence, used}];
    `status` {by, unavailable, values} when the cards have a status field;
    `next_page` {by, rule (a URL, "click" or "none"), probability, candidates,
    appears_as, selector}; `cards`, `kept`, `dropped_unavailable`; and `blocked`
    or `error` when the page ended that way. `suggested_override()` turns it
    into a paste-ready `SiteOverride`.
    """

    name = GENERIC_NAME
    label = GENERIC_LABEL
    describes = ("Any other site's page of businesses for sale, read with the TypeSafe "
                 "Classifier (e.g. Jev)")
    example = "https://www.websiteclosers.com/businesses-for-sale/"

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

    # -- the Source protocol --

    def begin(self) -> None:
        """Forget the last attempt: back to page 1, nothing decided."""
        self._page = 1
        self._next = None
        self.decisions = []

    def matches(self, url: str) -> bool:
        p = urlparse((url or "").strip())
        host = (p.hostname or "").lower()
        site = host[4:] if host.startswith("www.") else host
        return p.scheme.lower() in ("http", "https") and site == self.site

    def page_url(self, url: str, page: int) -> str:
        """Page 1 is the URL as given; later pages are reached by `advance`."""
        return url

    async def advance(self, page, n: int) -> bool:
        """Go to page `n` the way the previous page said to: a URL, or a click."""
        step, self._next = self._next, None
        if step is None:
            return False
        if step.kind == "goto":
            await page.goto(step.target, wait_until="domcontentloaded", timeout=120_000)
        else:
            try:
                target = page.locator(step.target)
                if not await target.count():
                    logger.info("generic: next-page control %s is gone on %s", step.target, self.url)
                    return False
                await target.first.click(timeout=15_000)
            except Exception as exc:  # noqa: BLE001 — a control that cannot be clicked ends paging
                logger.warning("generic: could not click %s on %s: %s", step.target, self.url, exc)
                return False
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:  # noqa: BLE001 — a click that loads in place never fires it
                pass
        self._page = n
        return True

    async def cards(self, page) -> CardPage:
        n = self._page
        self._next = None
        record: dict[str, Any] = {"page": n, "url": getattr(page, "url", "") or self.url}
        self.decisions.append(record)

        await extract.inject(page)
        links = list(self.override.listing_links) if self.override else []
        raw = await page.evaluate(JS_PROBE, {"next_number": n + 1, "patterns": links})
        probe = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(probe, dict):
            raise RuntimeError(f"the page at {record['url']} could not be read")
        title = str(probe.get("title") or "")
        record["url"] = str(probe.get("url") or record["url"])

        if self._blocked(probe):
            record["blocked"] = True
            return CardPage(listings=[], blocked=True, title=title)
        try:
            return await self._read(probe, n, title, record)
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

    async def _read(self, probe: dict, n: int, title: str, record: dict) -> CardPage:
        groups = [g for g in probe.get("groups") or [] if isinstance(g, dict)]
        chosen = await self._choose_groups(groups, title, record)
        if not chosen:
            if n == 1:
                message = record["listing_links"].get("error") or (
                    f"Found no list of businesses for sale on {record['url']}."
                )
                return CardPage(listings=[], title=title, error=message, retry=False)
            return CardPage(listings=[], title=title)

        cards = _cards_of(chosen)
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

    async def _choose_groups(self, groups: list[dict], title: str, record: dict) -> list[dict]:
        """The group(s) that are the page's list of businesses for sale; [] for none."""
        if self.override and self.override.listing_links:
            wanted = self.override.listing_links
            chosen = [g for g in groups if g.get("pattern") in wanted and g.get("cards")]
            record["listing_links"] = {"by": "override", "patterns": [g["pattern"] for g in chosen]}
            if not chosen:
                on_page = [g.get("pattern") for g in _candidates(groups)]
                record["listing_links"]["error"] = (
                    f"None of the listing link patterns in the site override for {self.site} "
                    f"({', '.join(wanted)}) matched a list of links on {record['url']}"
                    + (f"; the page's biggest groups are: {', '.join(on_page[:5])}." if on_page
                       else ".")
                )
            return chosen

        candidates = _candidates(groups)
        if not candidates:
            record["listing_links"] = {"by": "probe", "patterns": [], "candidates": 0}
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
        record["listing_links"] = {
            "by": "jev", "patterns": [picked["pattern"]] if picked else [],
            "confidence": round(answer.confidence, 3), "model": answer.model,
            "candidates": len(candidates),
        }
        return [picked] if picked else []

    async def _decide_fields(self, cards: list[_Card], title: str,
                             record: dict) -> tuple[dict[str, tuple[str, float]], set[str]]:
        """What each common field holds (role, confidence), then which statuses mean gone."""
        keys = _field_keys(cards)
        roles: dict[str, tuple[str, float]] = {}
        report: list[dict] = []
        ask: dict[str, str] = {}
        pinned = self.override.fields if self.override else {}
        for key, labeled in keys:
            rule = _pinned(pinned, key)
            if rule is not None:
                report.append({"key": key, "labeled": labeled, "role": rule, "by": "override"})
                if rule != "ignore":
                    roles[key] = (rule, 1.0)
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
                    "values_on_three_cards": _distinct(values, 3, _FIELD_VALUE_CHARS),
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
                if used:
                    roles[key] = (answer.choice, answer.confidence)
        record["fields"] = report

        unavailable = await self._decide_status(cards, roles, record)
        return roles, unavailable

    async def _decide_status(self, cards: list[_Card], roles: dict[str, tuple[str, float]],
                             record: dict) -> set[str]:
        """The status values on this page that mean the business is gone."""
        status_keys = [k for k, (role, _) in roles.items() if role == "status"]
        values = _distinct([c.values[k] for c in cards for k in status_keys if c.values.get(k)],
                           MAX_STATUS_VALUES, None)
        if not values:
            return set()
        drop = self.override.drop_status if self.override else None
        if drop is not None:
            needles = [d.lower() for d in drop]
            gone = {v for v in values if any(d in v.lower() for d in needles)}
            record["status"] = {"by": "override", "unavailable": sorted(gone), "values": values}
            return gone
        names = {f"status_{i}": v for i, v in enumerate(values, 1)}
        answers = await self._ask(
            {"statuses": names},
            {name: {"type": "noul", "instructions": STATUS_QUESTION.format(status=name)}
             for name in names},
        )
        probabilities = {value: _noul(answers, name) for name, value in names.items()}
        gone = {v for v, p in probabilities.items() if p >= UNAVAILABLE_MIN}
        record["status"] = {"by": "jev", "unavailable": sorted(gone),
                            "values": {v: round(p, 3) for v, p in probabilities.items()}}
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
        if best_p >= NEXT_MIN:
            if chosen.get("url"):
                step = _Next("goto", chosen["url"])
            else:
                step = _Next("click", f'[data-cbs-next="{chosen["id"]}"]')
        decided: dict[str, Any] = {
            "by": "jev",
            "rule": "none" if step is None else step.target if step.kind == "goto" else "click",
            "probability": round(best_p, 3),
            "candidates": len(candidates),
        }
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
            filled["title"] = _fallback_title(card.raw)
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
            excerpt=str(card.raw.get("excerpt") or card.raw.get("text") or ""),
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
        plus any status values later pages judged. Parts nothing was decided
        about are left out, so pasting it pins exactly what was seen.
        """
        out: dict[str, Any] = {"match": self.site}
        first = next((d for d in self.decisions if d.get("listing_links")), None)
        if first is None:
            return out
        patterns = first["listing_links"].get("patterns") or []
        if patterns:
            out["listing_links"] = list(patterns)
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


def _candidates(groups: list[dict]) -> list[dict]:
    """The groups worth offering as the page's list, biggest first."""
    usable = [g for g in groups
              if int(g.get("links") or 0) >= MIN_GROUP_LINKS
              and float(g.get("chrome") or 0.0) < MAX_CHROME
              and g.get("cards")]
    usable.sort(key=lambda g: -int(g.get("links") or 0) * int(g.get("text_chars") or 0))
    return usable[:MAX_CANDIDATES]


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
    "contact" link in the same tile); it is one listing, under the shorter
    link. Its address prefers a link in the same card whose path is a prefix of
    the group link's (`…/listing/5` over `…/listing/5/contact`), when that link
    belongs to this card alone.
    """
    everywhere: dict[str, int] = {}
    for g in groups:
        for c in g.get("cards") or []:
            for h in set(c.get("hrefs") or []):
                everywhere[h] = everywhere.get(h, 0) + 1

    by_card: dict[str, _Card] = {}
    order: list[str] = []
    for g in groups:
        keep = list(g.get("varying_keys") or []) if not g.get("paths_unique", True) else []
        for c in g.get("cards") or []:
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


def _field_keys(cards: list[_Card]) -> list[tuple[str, bool]]:
    """The fields common enough to ask about: (key, is it a label), card order."""
    counts: dict[str, int] = {}
    labeled: dict[str, bool] = {}
    for card in cards:
        for key in (card.raw.get("labeled") or {}):
            labeled.setdefault(key, True)
        for key in (card.raw.get("slots") or {}):
            labeled.setdefault(key, False)
        for key, value in card.values.items():
            counts[key] = counts.get(key, 0) + 1
    need = FIELD_PRESENCE * len(cards)
    common = [k for k in labeled if counts.get(k, 0) >= need and counts.get(k, 0) > 0]
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


def _page_template(url: str, page: int) -> str | None:
    """`url` with its page number replaced by {page}, when exactly one is there."""
    hits = list(re.finditer(rf"(?<!\d){page}(?!\d)", url))
    if len(hits) != 1:
        return None
    m = hits[0]
    return url[: m.start()] + "{page}" + url[m.end():]
