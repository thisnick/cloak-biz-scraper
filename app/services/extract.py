"""Rendered DOM → markdown → Notion blocks.

Ported from browserd (app/tasks/archive.py), whose pipeline was validated
against bizbuysell / sellerforce / quietlight / fcbb. Every non-obvious line
here is a bug someone already paid for; the comments say which, because each one
reads like something to simplify away and none of them are.

One deliberate change from the port. browserd configured Turndown inside its
extraction function, so the SERP-card path could not have used the same rules
without copying them. Here the rules are built once and exported to the page as
`window.__cbsMarkdown`, and both callers — archiving a detail page, and lifting a
listing card's excerpt — go through it. The h4–h6 rule below is the reason that
matters: a second, subtly different copy of these rules is exactly how it would
come back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("cloakbiz.extract")

_VENDOR = Path(__file__).resolve().parent.parent / "sources" / "vendor"
# Installed into the image next to the script (see Dockerfile). Overridable so
# the test suite and local runs can point at a checkout's own node_modules.
_MD2BLOCKS = Path(os.environ.get("MD2BLOCKS_PATH", "/opt/md2blocks/md2blocks.mjs"))

# Node's default stack is fine; the timeout is not about size but about never
# letting a wedged child process hold a sweep open forever.
_NODE_TIMEOUT_SEC = 120


# ── Built-in site recipes (host-suffix keyed) ────────────────────────────────
# The "adapter" mechanism is just data: `exclude_selectors` prunes junk before
# Readability runs, `extra_selectors` re-attaches the facts/financials boxes
# Readability drops. All four sets of selectors were validated against live pages.
RECIPES: dict[str, dict] = {
    # facts: financial stats rows + detailed-information dl
    "bizbuysell.com": {"extra_selectors": [".financials", "#dlDetailedInformation"]},
    # facts bar (asking/sales/profit) + year-established block (elementor template ids)
    "sellerforce.com": {
        "extra_selectors": [".elementor-element-69369e8", ".elementor-element-f8a9dc9"]
    },
    # advisor bio, sell-your-business CTA, related listings, info form = junk;
    # revenue/income/multiple stats live outside what Readability keeps
    "quietlight.com": {
        "exclude_selectors": [
            "section.meet_advice", "section.think_Sell", "section.recent_buy",
            "#information-form",
        ],
        "extra_selectors": [".inform_revenue"],
    },
    # facts rows (Duda template ids — fragile; body alone is fine if they rot)
    "sfbay.fcbb.com": {
        "extra_selectors": [
            ".u_1114425441", ".u_1283286437", ".u_1412846958", ".u_1552251775",
        ]
    },
}


def recipe_for(host: str) -> dict:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for suffix, recipe in RECIPES.items():
        if host == suffix or host.endswith("." + suffix):
            return recipe
    return {}


def recipe_for_url(url: str) -> dict:
    return recipe_for(urlparse(url).hostname or "")


# ── In-page libraries ────────────────────────────────────────────────────────
_LIBS_CACHE: str | None = None


def libs_js() -> str:
    """Readability + Turndown (+GFM) + our shared markdown rules, as one payload.

    Two things here are load-bearing and easy to undo:

    * **Everything is exported to `window` explicitly.** Playwright wraps
      evaluated source in its own scope, so the libraries' top-level `const`
      declarations are invisible to the next `page.evaluate` without this.
    * **This is injected with `page.evaluate`, never `add_script_tag`.** Script
      tags are subject to the page's CSP, and the sites we read have one.
    """
    global _LIBS_CACHE
    if _LIBS_CACHE is None:
        libs = "\n;\n".join(
            (_VENDOR / f).read_text()
            for f in ("readability.js", "turndown.js", "turndown-plugin-gfm.js")
        )
        _LIBS_CACHE = "() => { if (window.__cbsMarkdown) return; " + libs + _RULES_JS + " }"
    return _LIBS_CACHE


# The single definition of "how this project turns HTML into markdown". Both the
# archive path and the SERP-card excerpt call window.__cbsMarkdown(html, excludes).
_RULES_JS = r"""
;window.Readability = Readability;
window.TurndownService = TurndownService;
window.turndownPluginGfm = turndownPluginGfm;

window.__cbsTurndown = () => {
  const td = new TurndownService({
    headingStyle: "atx", codeBlockStyle: "fenced", bulletListMarker: "-",
  });
  turndownPluginGfm.gfm(td);
  // No-images decision: drop them entirely.
  td.addRule("dropImg", { filter: "img", replacement: () => "" });
  // Definition lists (BizBuySell's facts table) → "**term** value" lines.
  td.addRule("dt", { filter: "dt", replacement: (c) => `\n**${c.trim()}** ` });
  td.addRule("dd", { filter: "dd", replacement: (c) => `${c.trim()}\n` });
  // Notion has only h1-h3, and martian SILENTLY DROPS h4-h6 written as markdown
  // headings — especially nested in list items. That is what lost QuietLight's
  // REVENUE / INCOME / MULTIPLE stat cards: extracted correctly, converted
  // correctly, and then discarded on the way to Notion with no error anywhere.
  // Emitting them as bold text keeps the text and loses only the heading level.
  td.addRule("smallHeadings", {
    filter: ["h4", "h5", "h6"],
    replacement: (c) => (c.trim() ? `\n\n**${c.trim()}**\n\n` : ""),
  });
  return td;
};

window.__cbsMarkdown = (rawHtml, excludeSelectors) => {
  const box = document.createElement("div");
  box.innerHTML = rawHtml;
  const kill = ["script", "style", "noscript", "iframe", "svg", ...(excludeSelectors || [])];
  for (const sel of kill) {
    try { box.querySelectorAll(sel).forEach((n) => n.remove()); } catch (e) {}
  }
  return window.__cbsTurndown().turndown(box);
};
"""


EXTRACT_JS = """
(opts) => {
  const { mode, includeSelector, excludeSelectors, extraSelectors } = opts;
  let title = document.title, byline = null, usedPath = null, html = null;

  if (includeSelector) {
    const el = document.querySelector(includeSelector);
    if (!el) return { error: `include_selector not found: ${includeSelector}` };
    html = el.outerHTML; usedPath = "include_selector";
  } else if (mode === "full") {
    html = document.body.outerHTML; usedPath = "full";
  } else {
    // Readability mutates its input — clone the document. Excludes must be
    // pruned BEFORE parsing: Readability strips class/id from its output, so a
    // selector that would have matched cannot match afterwards.
    const clone = document.cloneNode(true);
    for (const sel of (excludeSelectors || [])) {
      try { clone.querySelectorAll(sel).forEach(n => n.remove()); } catch (e) {}
    }
    const article = new Readability(clone).parse();
    if (article && article.content && article.content.length > 200) {
      html = article.content; usedPath = "readability";
      title = article.title || title; byline = article.byline || null;
    } else {
      html = document.body.outerHTML; usedPath = "full_fallback";
    }
  }

  let markdown = window.__cbsMarkdown(html, excludeSelectors);
  // Extra sections (the facts/financials boxes Readability drops), appended
  // below the body. ALL matches per selector; missing ones skipped silently,
  // because a template id that rots should cost a section, not the archive.
  for (const sel of (extraSelectors || [])) {
    for (const el of document.querySelectorAll(sel)) {
      markdown += "\\n\\n---\\n\\n" + window.__cbsMarkdown(el.outerHTML, excludeSelectors);
    }
  }
  return { title, byline, usedPath, markdown, htmlChars: html.length };
}
"""


async def inject(page) -> None:
    """Make the extraction libraries available on the page."""
    await page.evaluate(libs_js())


async def extract(page, *, url: str, mode: str = "readability",
                  include_selector: str | None = None,
                  exclude_selectors: list[str] | None = None,
                  extra_selectors: list[str] | None = None) -> dict:
    """Readable content of the loaded page as markdown, using the host's recipe."""
    recipe = recipe_for_url(url)
    await inject(page)
    return await page.evaluate(
        EXTRACT_JS,
        {
            "mode": mode,
            "includeSelector": include_selector,
            "excludeSelectors": (
                recipe.get("exclude_selectors", []) if exclude_selectors is None
                else exclude_selectors
            ),
            "extraSelectors": (
                recipe.get("extra_selectors", []) if extra_selectors is None
                else extra_selectors
            ),
        },
    )


# ── markdown → Notion blocks ────────────────────────────────────────────────
_HR_SPLIT = re.compile(r"\n\s*\n---\n\s*\n|\n---\n")


class MarkdownConversionError(RuntimeError):
    """The markdown → Notion blocks step failed."""


def _md_to_blocks_sync(md: str, base_url: str) -> list[dict]:
    """Convert via martian (node).

    martian drops `---` thematic breaks, so the sections are split here and real
    divider blocks interleaved. `base_url` is passed to the script to resolve
    relative and fragment links, which Notion rejects outright.
    """
    if not _MD2BLOCKS.exists():
        raise MarkdownConversionError(
            f"The markdown converter is missing at {_MD2BLOCKS}. It is installed into the "
            f"image next to the app; set MD2BLOCKS_PATH to point at a checkout's copy when "
            f"running outside the container."
        )
    blocks: list[dict] = []
    for section in _HR_SPLIT.split(md):
        section = section.strip()
        if not section:
            continue
        if blocks:
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        proc = subprocess.run(
            ["node", str(_MD2BLOCKS), base_url], input=section, text=True,
            capture_output=True, timeout=_NODE_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            raise MarkdownConversionError(f"md2blocks failed: {proc.stderr.strip()[:400]}")
        blocks.extend(json.loads(proc.stdout))
    return blocks


async def md_to_blocks(md: str, base_url: str) -> list[dict]:
    return await asyncio.to_thread(_md_to_blocks_sync, md, base_url)


def prelude(url: str, heading: str) -> list[dict]:
    """The header we append above an archived page: what this is and where it came from."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    rich = [
        {"type": "text", "text": {"content": "Archived from "}},
        {"type": "text", "text": {"content": url, "link": {"url": url}}},
        {"type": "text", "text": {"content": f" · {ts}"}},
    ]
    return [
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_1",
         "heading_1": {"rich_text": [{"type": "text", "text": {"content": heading}}]}},
        {"object": "block", "type": "callout",
         "callout": {"icon": {"type": "emoji", "emoji": "📄"}, "rich_text": rich}},
    ]
