"""The triage scripts' pure parts (scripts/eval_triage.py, scripts/triage_prompt_from_notion.py).

Both scripts need a real Notion database and a key, so they are not run here.
What they compute is: an agreement figure that miscounted, or a row read with
its money in the wrong shape, would send a criteria change to production on a
wrong number; and a criteria page flattened badly would hand the classifier
something other than what the person wrote.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_triage = _load("eval_triage")
from_notion = _load("triage_prompt_from_notion")


def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def _page(page_id: str, created: str, **props) -> dict:
    return {"id": page_id, "created_time": created, "properties": props}


class TestReadingRows:
    SCHEMA = {"Listing Title": {}, "URL": {}, "Asking Price": {}, "SDE / Cash Flow": {},
              "Listing Excerpt": {}, "Bot Triage": {}, "Location": {}}

    def test_ids_come_out_of_urls_and_dashes(self):
        raw = "0123456789abcdef0123456789ABCDEF"
        want = "01234567-89ab-cdef-0123-456789abcdef"
        assert eval_triage.notion_id(raw) == want
        assert eval_triage.notion_id(f"https://www.notion.so/Listings-{raw}?v=1") == want
        assert from_notion.notion_id(want) == want
        with pytest.raises(ValueError):
            eval_triage.notion_id("listings")

    def test_columns_resolve_by_default_names_then_aliases_then_overrides(self):
        cols = eval_triage.resolve_columns(self.SCHEMA, {})
        assert cols["title"] == "Listing Title" and cols["excerpt"] == "Listing Excerpt"
        assert "ebitda" not in cols, "absent columns are left out, not guessed"
        cols = eval_triage.resolve_columns(self.SCHEMA, {"excerpt": "Nope"})
        assert "excerpt" not in cols

    def test_a_row_reads_as_the_card_did_money_included(self):
        cols = eval_triage.resolve_columns(self.SCHEMA, {})
        row = eval_triage.row_of(_page(
            "p1", "2026-09-01T10:00:00.000Z",
            **{"Listing Title": {"type": "title", "title": _rt("HVAC Services")},
               "URL": {"type": "url", "url": "https://x/1"},
               "Asking Price": {"type": "number", "number": 2400000},
               "SDE / Cash Flow": {"type": "rich_text", "rich_text": _rt("$600,000")},
               "Listing Excerpt": {"type": "rich_text", "rich_text": _rt("Contracts.")},
               "Bot Triage": {"type": "select", "select": {"name": "review"}},
               "Location": {"type": "rich_text", "rich_text": []}}), cols)
        listing = row["listing"]
        assert row["label"] == "REVIEW" and row["created"].startswith("2026-09-01")
        assert listing.asking_price == "$2,400,000", "a Number column reads back as dollars"
        assert listing.cashflow == "$600,000" and listing.location == ""
        from app.services.triage import card_state

        assert card_state(listing)["price_to_earnings_multiple"] == "4.00x"

    def test_values_of_every_kind(self):
        assert eval_triage.text_of({"type": "number", "number": 1250.5}) == "$1,250.50"
        assert eval_triage.text_of({"type": "number", "number": None}) == ""
        assert eval_triage.text_of({"type": "status", "status": {"name": "REJECT"}}) == "REJECT"
        assert eval_triage.text_of({"type": "select", "select": None}) == ""
        assert eval_triage.text_of(
            {"type": "formula", "formula": {"type": "string", "string": "x"}}) == "x"
        assert eval_triage.text_of(None) == ""


class TestAgreement:
    def _r(self, label, jev, created="2026-09-02"):
        return {"label": label, "jev": jev, "created": created}

    def test_counts(self):
        results = [self._r("REVIEW", "REVIEW"), self._r("REVIEW", "REJECT"),
                   self._r("REJECT", "REJECT"), self._r("REJECT", "REJECT")]
        stats = eval_triage.agreement(results)
        assert stats["n"] == 4 and stats["agree"] == 0.75
        assert (stats["review_kept"], stats["reviews"]) == (1, 2)
        assert (stats["reject_rejected"], stats["rejects"]) == (2, 2)
        assert eval_triage.agreement([])["agree"] == 0.0

    def test_the_report_splits_by_month_and_since(self):
        results = [self._r("REJECT", "REJECT", "2026-08-15T00:00"),
                   self._r("REJECT", "REVIEW", "2026-08-31T00:00"),
                   self._r("REVIEW", "REVIEW", "2026-09-02T00:00")]
        lines = eval_triage.report(results, "2026-08-30")
        assert lines[0].startswith("all") and "n=    3" in lines[0]
        assert lines[1].startswith("since 2026-08-30") and "n=    2" in lines[1]
        assert "agree= 50.0%" in lines[1]
        assert [l.split()[0] for l in lines[2:]] == ["2026-08", "2026-09"]


class TestCriteriaPageText:
    def _b(self, kind, text="", children=None):
        block = {"type": kind, kind: {"rich_text": _rt(text) if text else []}}
        if children:
            block["children"] = children
        return block

    def test_headings_lists_and_paragraphs_flatten_to_plain_text(self):
        blocks = [
            self._b("heading_1", "Listing Triage Criteria"),
            self._b("paragraph", "Reject only on clear evidence."),
            self._b("heading_2", "1. Location"),
            self._b("bulleted_list_item", "Reject if outside the Bay Area.", children=[
                self._b("bulleted_list_item", "Remote businesses continue."),
            ]),
            self._b("paragraph"),
            self._b("numbered_list_item", "Restaurants"),
            self._b("numbered_list_item", "Retail"),
            self._b("divider"),
            self._b("paragraph", "Price / SDE above 6.0 is a reject."),
        ]
        assert from_notion.to_text(blocks) == (
            "Listing Triage Criteria\n"
            "Reject only on clear evidence.\n"
            "\n"
            "1. Location\n"
            "- Reject if outside the Bay Area.\n"
            "  - Remote businesses continue.\n"
            "\n"
            "1. Restaurants\n"
            "2. Retail\n"
            "Price / SDE above 6.0 is a reject.\n"
        )

    def test_a_page_with_nothing_to_read_is_empty(self):
        assert from_notion.to_text([self._b("divider"), self._b("paragraph")]).strip() == ""

    @pytest.mark.asyncio
    async def test_children_are_fetched_page_by_page_and_nested(self):
        calls = []

        class Client:
            async def request(self, method, path, **kw):
                calls.append((method, path, (kw.get("params") or {}).get("start_cursor")))
                if path == "/blocks/page/children" and not kw["params"].get("start_cursor"):
                    return {"results": [{"id": "a", "type": "paragraph", "has_children": True,
                                         "paragraph": {"rich_text": _rt("one")}}],
                            "has_more": True, "next_cursor": "c2"}
                if path == "/blocks/page/children":
                    return {"results": [{"id": "b", "type": "child_page", "has_children": True,
                                         "child_page": {"title": "Sub"}}], "has_more": False}
                return {"results": [{"id": "a1", "type": "paragraph",
                                     "paragraph": {"rich_text": _rt("nested")}}],
                        "has_more": False}

        blocks = await from_notion.children(Client(), "page")
        assert [c[1] for c in calls] == ["/blocks/page/children", "/blocks/page/children",
                                          "/blocks/a/children"], "a child page is not read"
        assert all(c[0] == "GET" for c in calls), "read-only"
        assert from_notion.to_text(blocks) == "one\n  nested\n"
