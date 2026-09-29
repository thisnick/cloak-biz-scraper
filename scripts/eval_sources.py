"""Read listing pages with the generic reader, and show every decision it made.

The unit tests drive `GenericSource` with canned probe output and a fake
classifier, which pins the contract but not the thing that matters: whether, on
real pages it was never tuned to, the TypeSafe Classifier (e.g. Jev) picks the
list of businesses (and "none" where there is none), names the fields, and finds
the next page. This script answers that, page by page, and prints what a site
override pinning those decisions would look like. It is not run in CI: it needs
a browser, the live sites and an OpenRouter key.

    set -a; source .env; set +a          # OPENROUTER_API_KEY — never printed
    python scripts/eval_sources.py https://www.websiteclosers.com/businesses-for-sale/ --pages 2

    # the gate: every labelled page, scored (scripts/eval_sources_pages.json
    # explains the labels in its "_about")
    python scripts/eval_sources.py --truth scripts/eval_sources_pages.json --pages 1 --cdp "$CDP"
    python scripts/eval_sources.py --truth scripts/eval_sources_pages.json --pages 2 \\
        --paged-only --cdp "$CDP"

**Use a cloaked browser for live sites** (`--cdp`, the `cdp_url` that
`create_instance` returns): listing sites block a plain Chromium, and block one
IP that keeps asking. Attached over CDP, the script reuses that browser's own
context (its cookies and fingerprint), opens one tab per URL, closes it after,
and waits `--pace` seconds between sites. Without `--cdp` it launches a local
Chromium (`--executable` for a system Chrome); `--fixtures DIR` serves the saved
pages in tests/fixtures/generic instead of the network, for checking this script
itself offline.

A page that comes back as an anti-bot page is reported as blocked, not failed:
it says nothing about the reader.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ``python scripts/...`` puts scripts/, not the repo root, first on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import legibility  # noqa: E402
from app.services.blocker import text_contains_blocker  # noqa: E402
from app.services.typesafe import DEFAULT_MODEL, TypeSafeClient  # noqa: E402
from app.sources import owner_of  # noqa: E402
from app.sources.generic import GenericSource  # noqa: E402
from app.sources.overrides import override_for, parse_overrides  # noqa: E402

_VIEWPORT = {"width": 1440, "height": 900}


# ── reading one URL ──────────────────────────────────────────────────────────


async def evaluate(context, url: str, *, classifier, pages: int, wait_ms: int,
                   overrides=None, fixtures: dict[str, str] | None = None) -> dict[str, Any]:
    """Read up to `pages` pages of `url` the way a sweep does, in a new tab.

    Returns {url, pages: [{n, url, seconds, record, listings, legibility, fresh,
    blocked, error}], error, suggested_override}. Never raises: one site failing
    must not end an evaluation of twenty.
    """
    out: dict[str, Any] = {"url": url, "pages": [], "error": None, "suggested_override": None}
    page = await context.new_page()
    try:
        if fixtures is not None:
            await _serve_fixtures(page, fixtures)
        source = GenericSource(url, classifier, override_for(url, overrides or []))
        source.begin()
        seen: set[str] = set()
        for n in range(1, pages + 1):
            started = time.monotonic()
            if n == 1:
                await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            elif not await source.advance(page, n):
                break
            await page.wait_for_timeout(wait_ms)
            result = await source.cards(page)
            entry: dict[str, Any] = {
                "n": n, "url": page.url, "record": source.decisions[-1],
                "listings": [_listing_row(item) for item in result.listings],
                "legibility": None, "fresh": 0, "blocked": False, "error": result.error or None,
            }
            out["pages"].append(entry)
            if result.blocked or text_contains_blocker(result.title):
                entry["blocked"] = True
            elif not result.error and result.listings:
                verdict = await legibility.check(result.listings, page=n, classifier=classifier)
                entry["legibility"] = verdict.record(n)
            urls = [item.url for item in result.listings] + list(result.seen_urls or ())
            fresh = [u for u in dict.fromkeys(urls) if u not in seen]
            seen.update(urls)
            entry["fresh"] = len(fresh)
            entry["seconds"] = round(time.monotonic() - started, 1)
            if entry["blocked"] or entry["error"]:
                break
            if entry["legibility"] and not entry["legibility"]["ok"]:
                break
            if n > 1 and not fresh:
                break
        out["suggested_override"] = source.suggested_override()
    except Exception as exc:  # noqa: BLE001 — reported, and the next site still runs
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await page.close()
    return out


def _listing_row(item) -> dict[str, str]:
    return {"title": item.title, "location": item.location, "asking_price": item.asking_price,
            "cashflow": item.cashflow, "revenue": item.revenue, "ebitda": item.ebitda,
            "url": item.url}


async def _serve_fixtures(page, fixtures: dict[str, str]) -> None:
    """Answer a saved page's own URL with its HTML and refuse every other request."""
    async def handle(route):
        url = route.request.url.split("#", 1)[0]
        html = fixtures.get(url)
        if html is not None and route.request.resource_type == "document":
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)


def load_fixtures(directory: Path) -> dict[str, str]:
    """{captured URL: html} for every saved page (its first line names its URL)."""
    out: dict[str, str] = {}
    for path in sorted(directory.glob("*.html")):
        html = path.read_text()
        head = html.split("\n", 1)[0]
        if "Captured " in head:
            out[head.split("Captured ", 1)[1].split(" on ", 1)[0]] = html
    return out


# ── printing ─────────────────────────────────────────────────────────────────


def print_result(name: str, res: dict[str, Any]) -> None:
    print(f"\n── {name} ── {res['url']}")
    if res["error"]:
        print(f"  ERROR  {res['error']}")
    for entry in res["pages"]:
        _print_page(entry)
    if res["suggested_override"]:
        print("  suggested override:")
        text = json.dumps(res["suggested_override"], indent=2, ensure_ascii=False)
        print("    " + text.replace("\n", "\n    "))


def _print_page(entry: dict[str, Any]) -> None:
    rec = entry["record"]
    print(f"  page {entry['n']} · {entry['url']} · {entry.get('seconds', '?')} s")
    if entry["blocked"]:
        print("    BLOCKED  the site served an anti-bot page")
        return
    links = rec.get("listing_links") or {}
    patterns = ", ".join(links.get("patterns") or []) or "(none)"
    who = links.get("by", "?")
    conf = f" {links['confidence']:.2f}" if isinstance(links.get("confidence"), float) else ""
    cands = f" ({links['candidates']} candidates)" if "candidates" in links else ""
    print(f"    list      {patterns}  ← {who}{conf}{cands}")
    if entry["error"]:
        print(f"    error     {entry['error']}")
        return
    print(f"    cards     {rec.get('cards', 0)} on the list · {rec.get('kept', 0)} kept · "
          f"{rec.get('dropped_unavailable', 0)} dropped as unavailable · {entry['fresh']} new")
    for i, field in enumerate(rec.get("fields") or []):
        label = "fields" if i == 0 else ""
        conf = f" {field['confidence']:.2f}" if "confidence" in field else ""
        used = "" if field.get("by") == "override" else (" used" if field.get("used") else
                                                         " not used")
        print(f"    {label:<9} {field['key'][:48]:<48} → {field['role']:<14} "
              f"{field['by']}{conf}{used}")
    status = rec.get("status")
    if status:
        print(f"    status    {status['by']}: unavailable = {status['unavailable']} "
              f"of {status['values']}")
    nxt = rec.get("next_page") or {}
    if nxt:
        prob = f" p={nxt['probability']:.2f}" if "probability" in nxt else ""
        extra = ""
        if nxt.get("appears_as"):
            extra += f"  [{'; '.join(nxt['appears_as'])}]"
        if nxt.get("selector"):
            extra += f"  pin: click:{nxt['selector']}"
        print(f"    next      {nxt.get('rule')}  ← {nxt.get('by')}{prob}{extra}")
    leg = entry["legibility"]
    if leg:
        judged = ""
        if leg.get("classifier_asked"):
            judged = (f" · classifier dropped {leg['classifier_dropped']} of "
                      f"{leg['classifier_asked']} (mean {leg.get('classifier_mean', 0):.2f})")
        state = "ok" if leg["ok"] else f"FAILED: {leg.get('reason', '')}"
        print(f"    legible   {state} · kept {leg['kept']} · dropped {leg['dropped']}{judged}")
        for reject in leg.get("classifier_rejected") or []:
            print(f"              not a listing: {reject['title'] or '(no title)'} "
                  f"p={reject['p']:.2f}")
        if leg.get("classifier_error"):
            print(f"              classifier skipped: {leg['classifier_error']}")
    for row in entry["listings"][:2]:
        print("    sample    " + " | ".join(
            row[k] or "–" for k in ("title", "location", "asking_price", "cashflow")))


# ── scoring against labelled pages ──────────────────────────────────────────


def score(truth: dict[str, Any], res: dict[str, Any] | None, pages: int) -> dict[str, Any]:
    """One labelled page's verdict: PASS, FAIL, BLOCKED, ERROR or SKIPPED.

    The list is right when a chosen pattern contains any of the labelled
    substrings (or, labelled null, when no list was chosen); the page must also
    have at least `min_cards` cards on its list and pass the legibility check.
    The next-page rule must match its label (`none`, `click`, or a URL
    containing the `url:` substring); with `pages` ≥ 2 a labelled next page
    must also have been reached and shown new listings.
    """
    kind = "soft" if truth.get("soft") else "hard"
    out: dict[str, Any] = {"name": truth["name"], "kind": kind, "verdict": "FAIL",
                           "list": "", "next": "", "cards": "", "notes": []}
    if res is None:
        out["verdict"] = "SKIPPED"
        return out
    first = res["pages"][0] if res["pages"] else None
    if first is None:
        out["verdict"] = "ERROR"
        out["notes"].append(res.get("error") or "nothing was read")
        return out
    if first["blocked"]:
        out["verdict"] = "BLOCKED"
        return out
    rec = first["record"]
    patterns = (rec.get("listing_links") or {}).get("patterns") or []
    want = truth.get("group")
    legible = first.get("legibility") or {}

    if want is None:
        list_ok = not patterns
        out["list"] = "none ✓" if list_ok else f"picked {patterns[0]} ✗"
        if not list_ok and legible and not legible.get("ok"):
            out["notes"].append("the legibility check still failed the page")
        cards_ok = legible_ok = True
    else:
        list_ok = any(sub in p for p in patterns for sub in want)
        if not patterns:
            out["list"] = "none ✗"
            out["notes"].append(first.get("error") or "no list chosen")
        else:
            out["list"] = f"{patterns[0]} {'✓' if list_ok else '✗'}"
        cards = int(rec.get("cards") or 0)
        cards_ok = cards >= int(truth.get("min_cards") or 1)
        out["cards"] = f"{cards}{'≥' if cards_ok else '<'}{truth.get('min_cards', 1)}"
        legible_ok = bool(legible.get("ok"))
        if list_ok and not legible_ok:
            out["notes"].append(legible.get("reason") or "no legibility verdict")

    # A label may list alternatives ("click|none"): a load-more page whose
    # infinite scroll sometimes loads every listing before the sweep reads it
    # is right either way.
    labels = (truth.get("next") or "none").split("|")
    rule = str(((rec.get("next_page") or {}).get("rule")) or "none")

    def matches(label: str) -> bool:
        if label in ("none", "click"):
            return rule == label
        return rule.startswith("http") and label.removeprefix("url:") in rule

    next_ok = any(matches(label) for label in labels)
    out["next"] = f"{rule if rule in ('none', 'click') else 'url'} {'✓' if next_ok else '✗'}"
    if not next_ok and rule not in ("none", "click"):
        out["notes"].append(f"next: {rule}")
    if next_ok and rule != "none" and pages >= 2:
        second = res["pages"][1] if len(res["pages"]) > 1 else None
        if second is None:
            next_ok = False
            out["notes"].append("page 2 was never reached")
        elif second["blocked"]:
            out["notes"].append("page 2 blocked")
        elif second["error"] or not second["fresh"]:
            next_ok = False
            out["notes"].append(f"page 2: {second['error'] or 'no new listings'}")
        else:
            out["next"] += f" (+{second['fresh']} on p2)"
    ok = list_ok and cards_ok and legible_ok and next_ok
    out["verdict"] = "PASS" if ok else "FAIL"
    return out


def print_scorecard(rows: list[dict[str, Any]]) -> bool:
    """The table, then one line per gate. True when the hard gate passed."""
    print("\n── scorecard ──")
    print(f"{'page':<38} {'kind':<5} {'verdict':<8} {'list':<52} {'next':<18} {'cards':<9} notes")
    for r in rows:
        print(f"{r['name'][:38]:<38} {r['kind']:<5} {r['verdict']:<8} {r['list'][:52]:<52} "
              f"{r['next'][:18]:<18} {r['cards']:<9} {'; '.join(r['notes'])[:120]}")
    passed = True
    for kind in ("hard", "soft"):
        mine = [r for r in rows if r["kind"] == kind]
        if not mine:
            continue
        counts = {v: sum(r["verdict"] == v for r in mine)
                  for v in ("PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED")}
        judged = counts["PASS"] + counts["FAIL"] + counts["ERROR"]
        line = f"{kind} pages: {counts['PASS']}/{judged} pass"
        extra = [f"{counts[v]} {v.lower()}" for v in ("BLOCKED", "SKIPPED") if counts[v]]
        if extra:
            line += " (" + ", ".join(extra) + ", not counted)"
        if kind == "hard":
            passed = counts["FAIL"] == 0 and counts["ERROR"] == 0
            line = f"HARD GATE {'PASS' if passed else 'FAIL'} — " + line
        print(line)
    return passed


# ── main ─────────────────────────────────────────────────────────────────────


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("urls", nargs="*", help="listing pages to read")
    ap.add_argument("--truth", type=Path, help="score against labelled pages (JSON)")
    ap.add_argument("--only", action="append", default=[],
                    help="with --truth: only pages whose name contains this (repeatable)")
    ap.add_argument("--paged-only", action="store_true",
                    help="with --truth: only pages labelled with a next page")
    ap.add_argument("--pages", type=int, default=1, help="pages to read per URL (default 1)")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="seconds on each page before reading it (default 10)")
    ap.add_argument("--pace", type=float, default=5.0,
                    help="seconds between sites (default 5)")
    ap.add_argument("--cdp", help="attach to a running (cloaked) browser at this CDP URL")
    ap.add_argument("--executable", help="launch this Chrome/Chromium instead of Playwright's")
    ap.add_argument("--headed", action="store_true", help="show the launched browser")
    ap.add_argument("--fixtures", type=Path,
                    help="serve saved pages from this directory; nothing touches the network "
                         "except the classifier")
    ap.add_argument("--overrides", type=Path, help="a site overrides document to apply (JSON)")
    ap.add_argument("--model", default=os.environ.get("TYPESAFE_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--json", type=Path, help="also write every result to this file")
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("Set OPENROUTER_API_KEY (the TypeSafe Classifier's OpenRouter key) first.",
              file=sys.stderr)
        return 2
    classifier = TypeSafeClient(key_getter=lambda: key, model_getter=lambda: args.model)
    check = await classifier.check()
    print(f"classifier: {check.message}")
    if not check.ok:
        return 2

    overrides = parse_overrides(args.overrides.read_text()) if args.overrides else []
    targets: list[tuple[str, str, dict | None]] = [(u, u, None) for u in args.urls]
    if args.truth:
        for item in json.loads(args.truth.read_text())["pages"]:
            if args.only and not any(o.lower() in item["name"].lower() for o in args.only):
                continue
            if args.paged_only and set((item.get("next") or "none").split("|")) == {"none"}:
                continue
            targets.append((item["name"], item["url"], item))
    if not targets:
        ap.error("give one or more URLs, or --truth")
    fixtures = load_fixtures(args.fixtures) if args.fixtures else None

    from playwright.async_api import async_playwright

    results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        launched = None
        if args.cdp:
            browser = await pw.chromium.connect_over_cdp(args.cdp)
            # The cloaked browser's own context: its cookies, its fingerprint.
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            launched = await pw.chromium.launch(headless=not args.headed,
                                                executable_path=args.executable or None)
            context = await launched.new_context(viewport=_VIEWPORT)
        try:
            for i, (name, url, truth) in enumerate(targets):
                if owner_of(url) is not None:
                    print(f"\n── {name} ── skipped: {url} is read by a site adapter, "
                          f"not the generic reader")
                    continue
                if fixtures is not None and url not in fixtures:
                    if truth is not None:
                        rows.append(score(truth, None, args.pages))
                    continue
                if i and fixtures is None:
                    await asyncio.sleep(args.pace)
                res = await evaluate(context, url, classifier=classifier, pages=args.pages,
                                     wait_ms=int(args.wait * 1000), overrides=overrides,
                                     fixtures=fixtures)
                res["name"] = name
                results.append(res)
                print_result(name, res)
                if truth is not None:
                    rows.append(score(truth, res, args.pages))
        finally:
            if launched is not None:
                await launched.close()

    if args.json:
        args.json.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    if rows:
        return 0 if print_scorecard(rows) else 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
