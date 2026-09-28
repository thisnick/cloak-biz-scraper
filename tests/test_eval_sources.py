"""The live gate's scorer (scripts/eval_sources.py).

The script itself needs a browser, live sites and an OpenRouter key, so it is
not run here. Its verdicts are: a scorer that passed a wrong list, or failed a
page that was only blocked, would make the gate say the opposite of what
happened. So the one pure function that decides PASS/FAIL is pinned.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_sources.py"
_spec = importlib.util.spec_from_file_location("eval_sources", _SCRIPT)
eval_sources = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_sources)


def _page(n=1, *, patterns=("site.example/listing/{*}",), cards=20, rule="none", ok=True,
          blocked=False, error=None, fresh=20):
    return {
        "n": n, "url": f"https://site.example/list?page={n}", "blocked": blocked,
        "error": error, "fresh": fresh, "listings": [],
        "record": {"listing_links": {"by": "jev", "patterns": list(patterns)},
                   "cards": cards, "next_page": {"by": "jev", "rule": rule}},
        "legibility": {"page": n, "ok": ok, "kept": cards, "dropped": 0},
    }


LIST = {"name": "Site", "url": "https://site.example/list", "group": ["/listing/"],
        "min_cards": 10, "next": "url:?page=2"}
NONE = {"name": "404", "url": "https://site.example/gone", "group": None, "next": "none"}


class TestScore:
    def test_the_right_list_cards_legibility_and_next_page_pass(self):
        res = {"pages": [_page(rule="https://site.example/list?page=2")]}
        assert eval_sources.score(LIST, res, pages=1)["verdict"] == "PASS"

    @pytest.mark.parametrize("change,why", [
        ({"patterns": ("site.example/blog/{*}",)}, "wrong list"),
        ({"patterns": ()}, "no list"),
        ({"cards": 4}, "too few cards: a partial group"),
        ({"ok": False}, "cards that don't read as listings"),
        ({"rule": "none"}, "missed the next page"),
        ({"rule": "https://site.example/other"}, "followed the wrong link"),
    ])
    def test_each_way_of_being_wrong_fails(self, change, why):
        page = _page(**{"rule": "https://site.example/list?page=2", **change})
        assert eval_sources.score(LIST, {"pages": [page]}, pages=1)["verdict"] == "FAIL", why

    def test_no_list_is_right_where_there_is_none(self):
        assert eval_sources.score(NONE, {"pages": [_page(patterns=())]}, 1)["verdict"] == "PASS"
        picked = eval_sources.score(NONE, {"pages": [_page()]}, 1)
        assert picked["verdict"] == "FAIL" and "picked" in picked["list"]

    def test_a_blocked_page_is_blocked_not_failed(self):
        res = {"pages": [_page(blocked=True, patterns=())]}
        assert eval_sources.score(LIST, res, pages=1)["verdict"] == "BLOCKED"

    def test_nothing_read_is_an_error_and_no_run_is_skipped(self):
        assert eval_sources.score(LIST, {"pages": [], "error": "Timeout"}, 1)["verdict"] == "ERROR"
        assert eval_sources.score(LIST, None, 1)["verdict"] == "SKIPPED"

    def test_with_two_pages_the_next_page_must_add_listings(self):
        truth = {**LIST, "next": "click"}
        first = _page(rule="click")
        good = {"pages": [first, _page(2, rule="none", fresh=12)]}
        assert eval_sources.score(truth, good, pages=2)["verdict"] == "PASS"
        stale = {"pages": [first, _page(2, rule="none", fresh=0)]}
        assert eval_sources.score(truth, stale, pages=2)["verdict"] == "FAIL"
        never = {"pages": [first]}
        assert eval_sources.score(truth, never, pages=2)["verdict"] == "FAIL"


class TestScorecard:
    def test_the_gate_fails_on_a_failure_and_ignores_blocked_pages(self, capsys):
        rows = [{"name": "a", "kind": "hard", "verdict": "PASS", "list": "", "next": "",
                 "cards": "", "notes": []},
                {"name": "b", "kind": "hard", "verdict": "BLOCKED", "list": "", "next": "",
                 "cards": "", "notes": []},
                {"name": "c", "kind": "soft", "verdict": "FAIL", "list": "", "next": "",
                 "cards": "", "notes": []}]
        assert eval_sources.print_scorecard(rows) is True, "soft pages and blocks don't gate"
        assert "HARD GATE PASS — hard pages: 1/1 pass (1 blocked, not counted)" in (
            capsys.readouterr().out)
        rows[0]["verdict"] = "FAIL"
        assert eval_sources.print_scorecard(rows) is False


def test_the_labelled_pages_are_well_formed():
    """The truth file the gate reads: every page has a URL and a next-page label."""
    pages = json.loads((_SCRIPT.parent / "eval_sources_pages.json").read_text())["pages"]
    assert len(pages) >= 18
    for page in pages:
        assert page["url"].startswith("https://") and page["name"]
        nxt = page.get("next", "none")
        assert nxt in ("none", "click") or nxt.startswith("url:"), page["name"]
        assert page["group"] is None or (page["group"] and page.get("min_cards")), page["name"]
