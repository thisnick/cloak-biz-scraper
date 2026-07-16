"""Parse listing money strings into numbers, or into nothing at all.

The opinionated call from the plan: money is a **number**, not text. Storing
"$1,258,000" as a string makes the one question this tool exists to answer —
*"listings $1–7M with SDE over $500k"* — unsortable and unfilterable in Notion.

The other half of that call matters just as much: **a value we cannot parse
exactly becomes empty, never a guess.** "$81,000 + Inventory" is not $81,000. It
is $81,000 plus an undisclosed amount, so recording 81000 would understate the
asking price by an unknown margin and silently corrupt exactly the filter the
number type was introduced to enable. An empty cell is visibly missing; a wrong
number is invisibly wrong. Nothing is lost either way — the verbatim text still
survives in the listing's excerpt, the archived page body, and the run evidence.

So this parser accepts only strings that are *entirely* a single amount, and
rejects anything with trailing qualifiers, ranges, or commentary.
"""
from __future__ import annotations

import re

# Anchored on both ends: the whole string must be the amount and nothing else.
# That is the point — trailing junk means we do not know the number.
_AMOUNT = re.compile(
    r"""^
    \$?\s*                              # optional currency marker
    (?P<num>
        \d{1,3}(?:,\d{3})+              # 1,258,000
      | \d+                             # 7350000
    )
    (?P<frac>\.\d+)?                    # 1.25
    \s*
    (?P<mult>MM|mm|[KkMmBb])?           # 1.2M / 500k / 3MM
    $""",
    re.VERBOSE,
)

_MULTIPLIER = {"k": 1_000, "m": 1_000_000, "mm": 1_000_000, "b": 1_000_000_000}

# Common ways a listing says "we are not telling you". Matched only to skip the
# regex, not to distinguish them — every one of them means "empty".
_NOT_A_NUMBER = {"", "n/a", "na", "none", "not disclosed", "undisclosed", "tbd", "-", "—"}


def parse_money(value: object) -> float | None:
    """A single exact amount as a float, or None when we cannot be sure.

    >>> parse_money("$1,258,000")
    1258000.0
    >>> parse_money("Not Disclosed") is None
    True
    >>> parse_money("$81,000 + Inventory") is None   # not $81,000
    True
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int; a True asking price is nonsense
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if text.lower() in _NOT_A_NUMBER:
        return None

    match = _AMOUNT.match(text)
    if not match:
        return None

    number = float(match.group("num").replace(",", "") + (match.group("frac") or ""))
    mult = match.group("mult")
    if mult:
        number *= _MULTIPLIER[mult.lower()]
    return number
