"""Money parsing — the opinionated call that money is a number, and the harder
half of it: an amount we cannot be sure of becomes nothing at all."""
from __future__ import annotations

import pytest

from app.stores.money import parse_money


class TestParses:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("$1,258,000", 1_258_000.0),
            ("1,258,000", 1_258_000.0),
            ("7350000", 7_350_000.0),
            ("$7350000", 7_350_000.0),
            ("$ 1,250,000", 1_250_000.0),
            ("$1,250,000.50", 1_250_000.5),
            ("$500k", 500_000.0),
            ("$1.2M", 1_200_000.0),
            ("$3MM", 3_000_000.0),
            ("$2B", 2_000_000_000.0),
            ("0", 0.0),
        ],
    )
    def test_exact_amounts(self, text, expected):
        assert parse_money(text) == expected

    def test_numbers_pass_through(self):
        assert parse_money(1_258_000) == 1_258_000.0
        assert parse_money(1258000.5) == 1_258_000.5


class TestRefusesToGuess:
    @pytest.mark.parametrize(
        "text",
        [
            "Not Disclosed",
            "not disclosed",
            "Undisclosed",
            "N/A",
            "TBD",
            "",
            "   ",
            "-",
        ],
    )
    def test_the_ways_a_listing_says_nothing(self, text):
        assert parse_money(text) is None

    def test_qualified_amount_is_not_that_amount(self):
        # The case from the plan. 81000 would be actively wrong: the asking price
        # is 81000 PLUS an undisclosed amount of inventory, so recording the
        # number understates it by an unknown margin — and it would understate it
        # inside exactly the "$1-7M" filter the number type exists to enable.
        assert parse_money("$81,000 + Inventory") is None

    @pytest.mark.parametrize(
        "text",
        [
            "$1M - $2M",          # a range is not an amount
            "$1,258,000 (est.)",  # an estimate we would report as fact
            "Call for price",
            "$1,258,000 plus stock",
            "around 500000",
            "1,258,000 USD",      # trailing token we do not understand
            "$$100",
            "abc",
            "12,34",              # malformed grouping — not 1234, not 12.34
        ],
    )
    def test_anything_with_more_to_it_is_empty(self, text):
        assert parse_money(text) is None

    def test_a_boolean_is_not_a_price(self):
        # bool is an int in Python; True would otherwise become $1.
        assert parse_money(True) is None
        assert parse_money(False) is None

    def test_none_and_junk_types(self):
        assert parse_money(None) is None
        assert parse_money(["$5"]) is None
