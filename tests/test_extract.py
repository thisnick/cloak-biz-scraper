"""The markdown pipeline, and the two gotchas it exists to hold down.

The martian tests need node and @tryfabric/martian, which live in the image
(/opt/md2blocks). They skip when it is absent and run in the container — the
only place the real answer is available. Asserting martian's behaviour from
memory would defeat the point: the whole reason these exist is that what martian
*silently* does is not what anyone assumed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.services import extract
from app.services.extract import (
    _MD2BLOCKS, _md_to_blocks_sync, prelude, recipe_for, recipe_for_url,
)

martian = pytest.mark.skipif(
    not (_MD2BLOCKS.exists() and shutil.which("node")),
    reason="node + martian live in the image; run this in the container",
)


def texts(blocks: list[dict]) -> str:
    """Every string anywhere in the block tree, including nested children."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", {}).get("content", ""))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(blocks)
    return "\n".join(out)


class TestTheRulesAreShared:
    def test_one_definition_of_how_html_becomes_markdown(self):
        """Both the archive path and the SERP excerpt call __cbsMarkdown. A
        second copy of these rules is how the h4-h6 fix would come back."""
        libs = extract.libs_js()
        assert "window.__cbsMarkdown" in libs
        assert "smallHeadings" in libs
        assert extract.EXTRACT_JS.count("window.__cbsMarkdown") == 2  # body + extras

    def test_the_card_excerpt_uses_the_same_entry_point(self):
        from app.sources.bizbuysell import JS_CARDS

        assert "window.__cbsMarkdown" in JS_CARDS

    def test_libs_are_exported_to_window(self):
        """Playwright wraps evaluated source in its own scope, so a library's
        top-level const is invisible to the next evaluate without this."""
        libs = extract.libs_js()
        for name in ("window.Readability", "window.TurndownService", "window.turndownPluginGfm"):
            assert name in libs

    def test_excludes_are_pruned_before_readability_parses(self):
        """Readability strips class/id from its output, so a selector that would
        have matched cannot match afterwards. Order is the whole fix."""
        js = extract.EXTRACT_JS
        prune = js.index("excludeSelectors || []")
        parse = js.index("new Readability(clone)")
        assert prune < parse, "excludes must be pruned on the clone BEFORE parsing"


class TestRecipes:
    def test_quietlight_keeps_the_stats_and_drops_the_furniture(self):
        r = recipe_for("quietlight.com")
        assert ".inform_revenue" in r["extra_selectors"], "where REVENUE/INCOME/MULTIPLE live"
        assert "section.meet_advice" in r["exclude_selectors"]

    def test_www_and_subdomains_resolve(self):
        assert recipe_for("www.quietlight.com") == recipe_for("quietlight.com")
        assert recipe_for("sfbay.fcbb.com")["extra_selectors"]

    def test_an_unknown_host_gets_no_recipe_rather_than_an_error(self):
        assert recipe_for("example.com") == {}

    def test_a_websiteclosers_listing_resolves_with_and_without_www(self):
        """The financials and the WC reference live in the sidebar; a listing URL
        has to reach the recipe for either to survive."""
        want = [".sb-table", ".wysiwyg:not(.cfx)"]
        assert recipe_for_url(_WC_URL)["extra_selectors"] == want
        assert recipe_for_url(_WC_URL.replace("//www.", "//")) == recipe_for_url(_WC_URL)


_WC_FIXTURE = Path(__file__).parent / "fixtures" / "websiteclosers_listing.html"
_WC_URL = (
    "https://www.websiteclosers.com/businesses/sba-pre-qualified-ai-powered-medical-"
    "device-company-patented-systems-8-year-med-tech-firm-fda-class-i-device-110k-"
    "average-deal-size-22-granted-patent-claims/119217/"
)


def _chromium_available() -> bool:
    """Whether a Playwright chromium is installed — asked without launching one."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
        return bool(path) and Path(path).exists()
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(), reason="the real JS extractor needs a Playwright chromium"
)


@needs_chromium
class TestWebsiteClosersKeepsTheFinancialsAndTheReference:
    """The recipe against the real markup, in a real browser.

    A matched pair, like the martian tests above: the first case runs the same
    extractor with no extra selectors — which is exactly what an unrecognised
    host gets — and shows the page's only financials going missing. The rest run
    the host's own recipe and show them landing. Asserting this from remembered
    structure would defeat the point; the fixture is sliced from listing 119217's
    live DOM and the figures are that listing's own.
    """

    async def _extract(self, extra_selectors=None):
        from playwright.async_api import async_playwright

        html = _WC_FIXTURE.read_text()
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html)
                # extra_selectors=None means "use the host's recipe"; [] is the
                # pre-recipe behaviour, reproduced rather than described.
                return await extract.extract(
                    page, url=_WC_URL, extra_selectors=extra_selectors
                )
            finally:
                await browser.close()

    @pytest.mark.asyncio
    async def test_readability_alone_drops_every_figure_on_the_page(self):
        """The bug. Readability keeps the body copy and discards the sidebar, so
        an archive of this page states no price, no cash flow, and no broker
        reference at all."""
        res = await self._extract(extra_selectors=[])
        md = res["markdown"]
        assert res["usedPath"] == "readability", "not the path the live page takes"
        assert "robotic hair restoration systems" in md, "body copy should survive"
        for gone in ("Asking Price", "$ 4,900,000", "Cash Flow", "$ 1,371,348",
                     "Gross Income", "$ 1,958,786", "Year Established",
                     "Represented by", "WC 4068"):
            assert gone not in md, f"{gone} unexpectedly survived without the recipe"

    @pytest.mark.asyncio
    async def test_the_recipe_puts_the_four_figures_back(self):
        md = (await self._extract())["markdown"]
        for label, figure in (("Asking Price", "$ 4,900,000"),
                              ("Cash Flow", "$ 1,371,348"),
                              ("Gross Income", "$ 1,958,786"),
                              ("Year Established", "2018")):
            assert label in md, f"{label} row lost"
            assert figure in md, f"{label} figure lost"

    @pytest.mark.asyncio
    async def test_the_recipe_keeps_who_represents_the_listing_and_its_reference(self):
        """The last line of that block is the WC number, which is how a listing
        is referred to in conversation with the broker."""
        md = (await self._extract())["markdown"]
        assert "This Med-Tech Company is Represented by:" in md
        assert "WebsiteClosers.com" in md
        assert "Tech Business Brokers" in md
        assert "WC 4068" in md

    @pytest.mark.asyncio
    async def test_the_body_copy_is_not_pulled_in_twice(self):
        """`.wysiwyg:not(.cfx)` is the represented-by block precisely because the
        body copy carries `.cfx`. Drop the `:not(...)` and the whole description
        is appended a second time — this is the case that would catch it."""
        md = (await self._extract())["markdown"]
        assert md.count("robotic hair restoration systems") == 1

    @pytest.mark.asyncio
    async def test_the_extras_arrive_as_their_own_sections(self):
        """Appended below the body behind dividers, not spliced into it."""
        res = await self._extract()
        body, *extras = res["markdown"].split("\n\n---\n\n")
        assert len(extras) == 2, "one section per extra selector"
        assert "Asking Price" not in body


@martian
class TestMartianDropsSmallHeadingsInsideLists:
    """The bug that cost QuietLight's financials, pinned to its real trigger.

    A matched pair: the first proves the hazard exists, the second proves the
    Turndown rule answers it. Measured against real martian rather than
    remembered — and the memory was wrong in an instructive way. At top level an
    h4 is fine (martian maps it to heading_3). **Nested in a list item it is
    not**, and the failure is far worse than losing a heading: martian returns
    an EMPTY block list and drops the entire list, figures included, with no
    error. That is exactly the shape QuietLight's stat cards have, and exactly
    why REVENUE / INCOME / MULTIPLE vanished.

    If a future martian fixes this, the first test fails — and that is the
    signal that the smallHeadings rule could go.
    """

    def test_at_top_level_an_h4_is_harmless(self):
        blocks = _md_to_blocks_sync("#### REVENUE\n\n$1,746,364\n", "https://x.example/")
        assert [b["type"] for b in blocks] == ["heading_3", "paragraph"]
        assert "REVENUE" in texts(blocks)

    def test_an_h4_inside_a_list_item_silently_destroys_the_whole_list(self):
        """The real QuietLight shape, and the actual disaster: not a lost
        heading — a lost list. Everything, including the figures."""
        md = "-   #### REVENUE\n    \n    $1,746,364\n    \n-   #### MULTIPLE\n    \n    4.64x\n"
        blocks = _md_to_blocks_sync(md, "https://quietlight.com/")
        assert blocks == [], (
            "martian returned blocks for an h4 nested in a list — if this now "
            "passes, martian changed and the smallHeadings rule may be removable"
        )

    def test_bold_instead_of_a_heading_keeps_the_labels_and_the_figures(self):
        """What our Turndown rule emits for the same cards. This is the fix."""
        md = "-   **REVENUE**\n    \n    $1,746,364\n    \n-   **MULTIPLE**\n    \n    4.64x\n"
        found = texts(_md_to_blocks_sync(md, "https://quietlight.com/"))
        for want in ("REVENUE", "$1,746,364", "MULTIPLE", "4.64x"):
            assert want in found, f"{want} was dropped on the way to Notion"


@martian
class TestDividersAndLinks:
    def test_thematic_breaks_become_real_dividers(self):
        """martian drops `---`, so the sections are split and dividers rebuilt."""
        blocks = _md_to_blocks_sync("first\n\n---\n\nsecond", "https://x.example/")
        assert [b["type"] for b in blocks] == ["paragraph", "divider", "paragraph"]

    def test_a_relative_link_is_resolved_against_the_page(self):
        """Notion rejects non-absolute link URLs outright, so Turndown's raw
        hrefs have to be resolved before they get there."""
        blocks = _md_to_blocks_sync("[more](/about)", "https://quietlight.com/listings/1/")
        assert "https://quietlight.com/about" in str(blocks)

    def test_a_fragment_link_is_resolved_rather_than_dropped(self):
        """QuietLight's "Request More Information" points at #information-form.
        Resolved against the page it becomes absolute, which Notion accepts — so
        the link survives instead of being thrown away."""
        blocks = _md_to_blocks_sync("[Request More Information](#information-form)",
                                    "https://quietlight.com/listings/1/")
        assert "Request More Information" in texts(blocks)
        assert "https://quietlight.com/listings/1/#information-form" in str(blocks)

    @pytest.mark.parametrize("href", ["mailto:a@b.com", "javascript:alert(1)"])
    def test_a_link_that_is_not_http_loses_its_href_but_keeps_its_text(self, href):
        """Nothing resolves these to http(s), so Notion would refuse the whole
        request. The text is content and survives; the href is not and does not."""
        blocks = _md_to_blocks_sync(f"[click]({href})", "https://quietlight.com/listings/1/")
        assert "click" in texts(blocks)
        assert href not in str(blocks)


class TestPrelude:
    def test_it_says_what_this_is_and_where_it_came_from(self):
        blocks = prelude("https://quietlight.com/listings/1/", "Source Content")
        assert [b["type"] for b in blocks] == ["divider", "heading_1", "callout"]
        assert "Source Content" in str(blocks)
        assert "quietlight.com/listings/1" in str(blocks)


class TestMissingConverter:
    def test_a_missing_converter_explains_itself(self, monkeypatch, tmp_path):
        monkeypatch.setattr(extract, "_MD2BLOCKS", tmp_path / "nope.mjs")
        with pytest.raises(extract.MarkdownConversionError) as exc:
            _md_to_blocks_sync("hello", "https://x.example/")
        assert "MD2BLOCKS_PATH" in str(exc.value)
