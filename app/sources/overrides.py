"""Site overrides: a person pinning part of how a generic listing page is read.

The generic reader (`sources/generic.py`) decides everything about a page
fresh, every time: which links are the listings, what each field on a card
holds, which link is the next page, which statuses mean "gone". Nothing is
remembered between sweeps, because a site that changes its layout would
otherwise be read with last month's answers. An override is the one exception,
and it is a person's, not the reader's: when a decision keeps coming out wrong
for one site, whoever runs the scraper pins that part and the reader stops
asking about it. Every part is optional — anything left out is still decided
fresh — so an override can be as small as the one thing that was wrong.

The values are copied from what the reader reports it decided (the link
pattern string, a field's label, a next-page URL), which is why the pattern
format and field keys are human-readable and stable: an override is meant to be
pasted, not written from scratch.

This module is the shape, the matching, and reading the document a person
wrote. Where that document is stored and edited belongs to Settings
(`site_overrides_json`, kept as the raw text so their formatting survives).
"""
from __future__ import annotations

import json
from typing import Any, Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

# What a field on a listing card can hold. The descriptions are the option
# wording the classifier is asked with, so they are written for it, and they are
# the wording that was measured (94% of fields named correctly on 13 sites; 69/70
# at confidence ≥ 0.8) — rewording them is a change to re-measure.
Role = Literal[
    "title", "location", "asking_price", "cash_flow_sde", "ebitda", "revenue",
    "status", "category", "description", "listing_id", "other",
]
FieldRule = Literal[
    "title", "location", "asking_price", "cash_flow_sde", "ebitda", "revenue",
    "status", "category", "description", "listing_id", "other", "ignore",
]

ROLES: dict[str, str] = {
    "title": "The listing's headline or business name",
    "location": "Where the business is located: city, county, state or country",
    "asking_price": "The price the seller is asking for the business",
    "cash_flow_sde": ("Owner earnings: cash flow, SDE, seller's discretionary earnings, "
                      "net profit or adjusted earnings"),
    "ebitda": "EBITDA specifically",
    "revenue": "Revenue, gross sales or turnover",
    "status": ("The listing's status or badge: new, active, pending, sold, under contract, "
               "coming soon, featured, sponsored"),
    "category": "The industry, category or type of business",
    "description": "A sentence or paragraph describing the business",
    "listing_id": "A listing number or reference code",
    "other": ("Something else: button or link text, a field label on its own, icon names, "
              "multiples, business age"),
}
assert tuple(ROLES) == get_args(Role)

MONEY_ROLES: tuple[str, ...] = ("asking_price", "cash_flow_sde", "ebitda", "revenue")

NEXT_PAGE_HELP = (
    'next_page must be a URL containing {page} (e.g. "https://example.com/listings?page={page}"), '
    '"click:<css selector>" for a button with no address, or "none"'
)


class SiteOverride(BaseModel):
    """The parts of reading one site that a person has pinned.

    `match` is a URL prefix ("https://www.bizquest.com/businesses-for-sale-in-")
    or a bare host ("bizquest.com", any page on it); `www.` never matters and
    the longest match wins. `listing_links` are link patterns as the reader
    reports them ("www.bizquest.com/business-for-sale/{*}/{*}"), where `{*}` is
    any one path segment and `{id}` one that is an id ("12899223-some-slug",
    "1012838.aspx"); several are read as one list, and one that is only an
    action on each card ("…/{*}/contact", "watch_item?…") is read through the
    detail links inside its cards. `fields` maps a card field —
    its label, or the slot key the reader reports for an unlabelled one — to
    what it holds, or to "ignore". `next_page` is a URL with `{page}` in it,
    "click:<css selector>", or "none". `drop_status` lists status texts
    (matched as case-insensitive substrings) that mean a listing is gone; an
    empty list drops nothing.
    """

    model_config = ConfigDict(extra="forbid")

    match: str
    listing_links: list[str] = []
    fields: dict[str, FieldRule] = {}
    next_page: str | None = None
    drop_status: list[str] | None = None

    @field_validator("match")
    @classmethod
    def _match(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("match must be a URL prefix or a host, e.g. \"bizquest.com\"")
        if "://" in value:
            p = urlparse(value)
            if p.scheme.lower() not in ("http", "https") or not p.hostname:
                raise ValueError("match must be an http(s) URL prefix or a bare host")
        elif _site_key(value) is None:
            raise ValueError("match must be a URL prefix or a host, e.g. \"bizquest.com\"")
        return value

    @field_validator("listing_links", "drop_status")
    @classmethod
    def _no_blanks(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [v.strip() for v in value]
        if any(not v for v in cleaned):
            raise ValueError("entries cannot be blank")
        return cleaned

    @field_validator("fields")
    @classmethod
    def _field_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not k.strip() for k in value):
            raise ValueError("a field name cannot be blank")
        return value

    @field_validator("next_page")
    @classmethod
    def _next_page(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value == "none":
            return value
        if value.startswith("click:"):
            if not value[len("click:"):].strip():
                raise ValueError(NEXT_PAGE_HELP)
            return value
        p = urlparse(value)
        if p.scheme.lower() in ("http", "https") and p.hostname and "{page}" in value:
            return value
        raise ValueError(NEXT_PAGE_HELP)


def _site_key(value: str) -> tuple[str, str] | None:
    """(host without www., lowercased path+query) — what matching compares."""
    raw = (value or "").strip()
    if not raw:
        return None
    p = urlparse(raw if "://" in raw else f"https://{raw}")
    try:
        host = (p.hostname or "").lower()
    except ValueError:
        return None
    if not host or " " in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    rest = (p.path or "") + (f"?{p.query}" if p.query else "")
    return host, rest.lower()


def override_for(url: str, overrides: list[SiteOverride] | None) -> SiteOverride | None:
    """The override that applies to `url`: the longest matching `match`, or None.

    A bare host matches every page on that host; a URL prefix matches pages
    whose address starts with it (scheme and `www.` ignored, so http/https and
    with/without www. are the same site). Hosts must be equal — "fcbb.com" does
    not cover "sfbay.fcbb.com", because two offices of one brand are two sites
    with their own layouts. On a tie, the first in the list wins.
    """
    target = _site_key(url)
    if target is None:
        return None
    best: SiteOverride | None = None
    best_len = -1
    for override in overrides or ():
        key = _site_key(override.match)
        if key is None or key[0] != target[0]:
            continue
        prefix = key[1] if "?" in key[1] else key[1].rstrip("/")
        if prefix and not target[1].startswith(prefix):
            continue
        if len(prefix) > best_len:
            best, best_len = override, len(prefix)
    return best


# ── the document ─────────────────────────────────────────────────────────────


class OverridesInvalid(ValueError):
    """The saved overrides document cannot be read; the message says where."""


_DOCUMENT = TypeAdapter(list[SiteOverride])

EXAMPLE = '[{"match": "bizquest.com", "next_page": "none"}]'


def parse_overrides(text: str | None) -> list[SiteOverride]:
    """The overrides in a document a person typed. Blank means none.

    Raises `OverridesInvalid` with the first problem and where it is: a line
    and column for broken JSON (which is where an editor puts the cursor), and
    the override and field for a value that is not allowed ("Override 2
    (bizquest.com) → next_page: …"). A person fixing a document in a text box
    needs to know which line to look at more than they need every problem at
    once, so only the first is spelled out and the rest are counted.
    """
    if not (text or "").strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OverridesInvalid(
            f"Line {exc.lineno}, column {exc.colno}: {exc.msg}. The overrides must be "
            f"JSON, e.g. {EXAMPLE}."
        ) from None
    try:
        return _DOCUMENT.validate_python(data)
    except ValidationError as exc:
        errors = exc.errors()
        first = _located(errors[0], data)
        more = len(errors) - 1
        if more:
            first += f" (and {more} more problem{'' if more == 1 else 's'})"
        raise OverridesInvalid(first) from None


def _located(error: dict[str, Any], data: Any) -> str:
    loc = tuple(error.get("loc") or ())
    msg = str(error.get("msg") or "is not valid").removeprefix("Value error, ")
    if not loc:
        return (f"The overrides must be a JSON list with one entry per site, "
                f"e.g. {EXAMPLE}.")
    index, path = loc[0], [str(part) for part in loc[1:]]
    where = f"Override {index + 1}" if isinstance(index, int) else str(index)
    if isinstance(index, int) and isinstance(data, list) and index < len(data):
        entry = data[index]
        if isinstance(entry, dict) and isinstance(entry.get("match"), str) and entry["match"]:
            where += f" ({entry['match']})"
    kind = error.get("type")
    if kind == "extra_forbidden":
        msg = ("is not something an override can set (it takes "
               + ", ".join(SiteOverride.model_fields) + ")")
    elif kind == "missing":
        msg = "is required"
    elif kind == "model_type":
        msg = 'must be an object like {"match": "example.com", …}'
    if path:
        return f"{where} → {' → '.join(path)}: {msg}"
    return f"{where}: {msg}"
