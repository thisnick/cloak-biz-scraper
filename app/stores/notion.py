"""Notion implementation of ListingStore.

Three rules shape everything here, and all three come from the same place: the
database belongs to the user, not to us.

1. **Never auto-create.** A database appears only when someone clicks "Create".
   Surprise databases in someone's workspace are hostile, and a tool that
   creates one on first sync trains people not to trust it with their workspace.
2. **Never clobber a column we do not own.** We write only the properties in
   KNOWN_PROPS, and only where the user's database already has that name at that
   type — plus the TRIAGE_PROPS columns, and those only when a sweep was asked to
   triage. Everything else is invisible to us. That is precisely what makes
   "add your own columns and they will survive" a promise rather than a hope.
3. **Never mutate while verifying.** `verify_schema` reports; the user decides.

The API version is pinned to 2022-06-28 rather than tracking latest. Notion's
2025-09-03 revision introduces data sources and re-parents properties beneath
them, so "latest" is not a compatible superset — an unpinned client would
rewrite the meaning of every call here on Notion's schedule, not ours.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..models import Listing
from .base import (
    DedupeIndex,
    PropIssue,
    SchemaReport,
    TriageTarget,
    TriageUnavailable,
    UpsertResult,
)
from .money import parse_money

logger = logging.getLogger("cloakbiz.notion")

API = "https://api.notion.com/v1"
API_VERSION = "2022-06-28"

# Notion's documented average. Exceeding it earns a 429 mid-sweep, which is a
# worse outcome than being deliberately unhurried.
_MIN_REQUEST_INTERVAL_SEC = 1 / 3
_MAX_RETRIES = 4
# Notion truncates rich_text/title at 2000 chars per text object and 400s past
# it. Listing titles from a SERP card are nowhere near, but an excerpt could be.
_TEXT_LIMIT = 2000


class NotionError(RuntimeError):
    """Anything the Notion API refused, phrased for someone with no terminal."""


class NotionAuthError(NotionError):
    """The token is missing, wrong, or lacks access."""


class NotionNotFound(NotionError):
    """The database or page does not exist, or is not shared with the integration."""


class SchemaInvalid(NotionError):
    """The database cannot hold listings until its schema is fixed."""

    def __init__(self, report: SchemaReport) -> None:
        self.report = report
        problems = "; ".join(i.describe() for i in [*report.missing_required, *report.mismatched_required])
        super().__init__(
            f"This database is missing what the sync needs: {problems}. "
            f"Fix it in Notion, or create a new database from Settings."
        )


# ── the schema, as one table ────────────────────────────────────────────────
# Single source of truth for verify_schema, create_database, and upsert. Three
# copies of this list would drift, and the failure would be silent: a property
# we create but never verify, or verify but never write.


def _text_chunk(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value[:_TEXT_LIMIT]}}]


# Notion's API type names are not the words on the user's screen. Someone whose
# column header says "Text" cannot act on advice about "rich_text".
_DISPLAY_TYPE = {
    "title": "Title", "rich_text": "Text", "number": "Number", "select": "Select",
    "multi_select": "Multi-select", "date": "Date", "url": "URL", "email": "Email",
    "phone_number": "Phone", "checkbox": "Checkbox", "people": "Person",
    "files": "Files & media", "relation": "Relation", "rollup": "Rollup",
    "formula": "Formula", "status": "Status", "unique_id": "ID",
    "created_time": "Created time", "last_edited_time": "Last edited time",
    "created_by": "Created by", "last_edited_by": "Last edited by",
}


def _display(notion_type: str | None) -> str | None:
    if notion_type is None:
        return None
    return _DISPLAY_TYPE.get(notion_type, notion_type)


# Why a number matters here, in the user's terms rather than ours. This is the
# whole reason §4 is opinionated about money being numeric: the core triage
# question is "$1–7M with SDE over $500k", and a text column cannot answer it —
# "$1,258,000" sorts next to "$999" as a string.
_MONEY_CONSEQUENCE = (
    "Amounts are skipped unless this is a Number column. Number is also what lets you "
    "sort and filter — asking \"which listings are $1–7M with SDE over $500k?\" only "
    "works on numbers."
)
_DEDUPE_CONSEQUENCE = (
    "Without it this app cannot tell a listing it has already saved from a new one, so "
    "syncing is blocked until you add it."
)


@dataclass(frozen=True)
class NotionProp:
    # Stable machine key for this field, independent of the display name. The
    # column MAPPING is keyed by this, so renaming the default column header
    # ("SDE / Cash Flow" -> whatever the user calls it) never breaks a stored map.
    key: str
    name: str
    type: str
    required: bool
    # What POST /v1/databases needs to create it.
    create: dict[str, Any]
    # What the user loses if this column is missing or the wrong type. Lives in
    # the same table as everything else so the explanation cannot drift from the
    # behaviour it explains.
    consequence: str = ""
    # Listing attribute this reads from; None for values we compute (timestamps,
    # the initial Status).
    source: str | None = None
    render: Callable[[Any], dict[str, Any] | None] | None = None
    # Read back out of a page — only needed for the dedupe keys.
    extract: Callable[[dict[str, Any]], str] | None = None
    # Rewritten on a row that is already stored, each time a sweep sees the
    # listing again. Everything else is written when the row is created and
    # never touched again, so that a user's own edits (a Status they changed, a
    # First Seen At) are not reset by a later sweep.
    refresh: bool = False


def _plain(prop: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in prop.get("rich_text") or [])


def _plain_title(prop: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in prop.get("title") or [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: object) -> dict[str, Any] | None:
    """A verbatim listing amount rendered into a Notion Number, or nothing.

    This is where the plan's split lands: the scraper hands us "$1,258,000" or
    "$81,000 + Inventory" exactly as the card said it, and the decision to make
    that a number belongs here, because being a number is a fact about this
    column rather than about the listing. A store writing to a text column would
    keep the string; this one parses.

    Returning None leaves the cell **empty**, which is the deliberate half of the
    call: "$81,000 + Inventory" is not $81,000, so writing 81000 would silently
    understate a price by an unknown amount and corrupt the very filter the
    Number type exists to enable. Nothing is lost — the verbatim text survives on
    the Listing itself, in the excerpt, and in the archived page.
    """
    amount = parse_money(value)
    return {"number": amount} if amount is not None else None


KNOWN_PROPS: tuple[NotionProp, ...] = (
    # The four the machine cannot work without.
    NotionProp(
        "listing_title", "Listing Title", "title", True, {"title": {}}, source="title",
        render=lambda v: {"title": _text_chunk(v)} if v else None,
        extract=_plain_title,
        consequence="Every Notion database needs exactly one Title column; syncing is "
                    "blocked without it.",
    ),
    NotionProp(
        "url", "URL", "url", True, {"url": {}}, source="url",
        render=lambda v: {"url": v} if v else None,
        consequence="Without it there is no link back to the original listing, so "
                    "syncing is blocked.",
    ),
    NotionProp(
        "normalized_url", "Normalized URL", "rich_text", True, {"rich_text": {}},
        source="normalized_url",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        extract=_plain, consequence=_DEDUPE_CONSEQUENCE,
    ),
    NotionProp(
        "listing_id", "Listing ID", "rich_text", True, {"rich_text": {}}, source="listing_id",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        extract=_plain, consequence=_DEDUPE_CONSEQUENCE,
    ),
    # Recommended: what turns a list of rows into a triage tool.
    NotionProp(
        "source", "Source", "select", False, {"select": {}}, source="source",
        render=lambda v: {"select": {"name": v}} if v else None,
        consequence="Which site a listing came from will not be recorded. Everything "
                    "else still syncs.",
    ),
    NotionProp(
        "location", "Location", "rich_text", False, {"rich_text": {}}, source="location",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        consequence="Locations will not be recorded. Everything else still syncs.",
    ),
    NotionProp(
        "asking_price", "Asking Price", "number", False, {"number": {"format": "dollar"}},
        source="asking_price", render=_money, consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "revenue", "Revenue", "number", False, {"number": {"format": "dollar"}},
        source="revenue", render=_money, consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "sde_cashflow", "SDE / Cash Flow", "number", False, {"number": {"format": "dollar"}},
        source="cashflow", render=_money, consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "ebitda", "EBITDA", "number", False, {"number": {"format": "dollar"}},
        source="ebitda", render=_money, consequence=_MONEY_CONSEQUENCE,
    ),
    # Refreshed, unlike the other listing fields: it is the sweep's own reading
    # of the live card rather than anything a user types, so a known row should
    # say what the card says now — a rewritten description, a price cut spelled
    # out in the text. An empty snippet renders to None and never blanks one.
    NotionProp(
        "excerpt", "Excerpt", "rich_text", False, {"rich_text": {}}, source="excerpt",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None, refresh=True,
        consequence="The card's own summary text — extracted on every sweep but only "
                    "saved when this is mapped to a column, where each sweep refreshes "
                    "it from the live card. Everything else still syncs.",
    ),
    NotionProp(
        "status", "Status", "select", False,
        {"select": {"options": [
            {"name": "New", "color": "blue"},
            {"name": "Review", "color": "yellow"},
            {"name": "Rejected", "color": "red"},
        ]}},
        render=lambda v: {"select": {"name": "New"}},
        consequence="New listings will not be marked 'New', so you lose the triage "
                    "workflow but not the listings.",
    ),
    NotionProp(
        "first_seen_at", "First Seen At", "date", False, {"date": {}},
        render=lambda v: {"date": {"start": _now_iso()}},
        consequence="You will not see when a listing first appeared.",
    ),
    NotionProp(
        "last_synced_at", "Last Synced At", "date", False, {"date": {}},
        render=lambda v: {"date": {"start": _now_iso()}}, refresh=True,
        consequence="You will not be able to tell a listing that is still live from one "
                    "that has come off the market.",
    ),
)

PROPS_BY_NAME = {p.name: p for p in KNOWN_PROPS}
PROPS_BY_KEY = {p.key: p for p in KNOWN_PROPS}
REQUIRED_PROPS = tuple(p for p in KNOWN_PROPS if p.required)


# ── the triage columns ──────────────────────────────────────────────────────
# Written by triage and by nothing else: a sweep given a triage prompt records
# its REVIEW/REJECT decision here after the rows are saved. They are a separate
# table rather than more KNOWN_PROPS on purpose, because everything that walks
# KNOWN_PROPS would otherwise pick them up for people who never triage: the
# insert and the refresh would write them (and the mapped insert renders ANY
# date field as "now", so every new row would be stamped Triaged At the moment
# it was created, before anything had judged it), and the schema report and
# `skipped` would nag about four missing columns nobody asked for.
#
# How a triage field finds its column is also different, and more forgiving:
# see _resolve_triage.

TRIAGE_PROPS: tuple[NotionProp, ...] = (
    NotionProp(
        "bot_triage", "Bot Triage", "select", False,
        {"select": {"options": [
            {"name": "REVIEW", "color": "yellow"},
            {"name": "REJECT", "color": "red"},
        ]}},
        consequence="A sweep asked to triage has nowhere to record REVIEW or REJECT, so "
                    "it cannot triage.",
    ),
    NotionProp(
        "triage_reason", "Triage Reason", "rich_text", False, {"rich_text": {}},
        consequence="Why a row was marked REVIEW or REJECT will not be recorded.",
    ),
    NotionProp(
        "triaged_at", "Triaged At", "date", False, {"date": {}},
        consequence="When a row was triaged will not be recorded.",
    ),
    NotionProp(
        "criteria_version", "Criteria Version", "rich_text", False, {"rich_text": {}},
        consequence="Which version of the criteria judged a row will not be recorded.",
    ),
)

TRIAGE_BY_KEY = {p.key: p for p in TRIAGE_PROPS}

# The column types each triage field can be written into. Narrower than what an
# upsert field adapts to, because these values have a shape: the decision is one
# word (a Select option or text), the time is a date (or its text), the reason is
# prose. A reason in a Select would mint a new option for every row. A Status
# column is deliberately absent: its options are fixed and the API cannot add
# one, so REVIEW written to a Status lacking it would fail the whole write.
_TRIAGE_WRITABLE: dict[str, tuple[str, ...]] = {
    "bot_triage": ("select", "rich_text"),
    "triage_reason": ("rich_text",),
    "triaged_at": ("date", "rich_text"),
    "criteria_version": ("rich_text", "select"),
}


# ── the column MAPPING ──────────────────────────────────────────────────────
# The map is {field-key -> the user's column NAME, or None ("don't sync")}. A
# missing key means "unmapped": harmless for an optional field, blocking for a
# required one. An EMPTY map is the back-compat sentinel — it means "no map
# stored", so every method below falls back to IDENTITY mapping (each field to a
# same-named column), which is exactly the behaviour before this feature existed.

ColumnMap = dict[str, "str | None"]


def default_column_map(column_names: set[str]) -> ColumnMap:
    """Build the default map for a database by IDENTITY.

    Auto-map each field to a same-named column when the database has one. Leave
    unmatched REQUIRED fields unmapped (absent) so the user is forced to choose a
    column for them; default unmatched OPTIONAL fields to None ("don't sync").
    The result is always non-empty, so it never collides with the empty-map
    sentinel and is always treated as an explicit map from here on.

    A triage field is mapped to its same-named column when there is one and
    otherwise left ABSENT rather than set to None: absent means "whichever column
    has its name" (see _resolve_triage), so a Bot Triage column added in Notion
    later is picked up without a trip back to Settings. None would switch it off.
    """
    out: ColumnMap = {}
    for prop in KNOWN_PROPS:
        if prop.name in column_names:
            out[prop.key] = prop.name
        elif not prop.required:
            out[prop.key] = None
        # required + unmatched -> left absent (unmapped), the user must set it.
    for prop in TRIAGE_PROPS:
        if prop.name in column_names:
            out[prop.key] = prop.name
    return out


def _resolve(column_map: ColumnMap | None, key: str) -> str | None:
    """The column a field points at: the map's value, or the identity name when
    no map is stored."""
    if not column_map:
        return PROPS_BY_KEY[key].name
    return column_map.get(key)


def _resolve_triage(column_map: ColumnMap | None, key: str, columns) -> str | None:
    """The column a triage field writes to, or None when it is not written.

    A key the map does not mention resolves to the same-named column when the
    database has one — which is every stored map made before triage existed, so
    a database that already has a "Bot Triage" column works with no visit to
    Settings. An explicit None is the person switching the field off, and wins.
    A column the database does not (or no longer) have never resolves: triage
    writes only into columns that exist, exactly like the upsert.
    """
    if column_map and key in column_map:
        col = column_map[key]
    else:
        col = TRIAGE_BY_KEY[key].name
    return col if col and col in columns else None


def _triage_columns(column_map: ColumnMap | None, columns) -> dict[str, str]:
    """Every triage field that resolves, as key -> column name."""
    out: dict[str, str] = {}
    for prop in TRIAGE_PROPS:
        col = _resolve_triage(column_map, prop.key, columns)
        if col:
            out[prop.key] = col
    return out


def _decision_column(column_map: ColumnMap | None, actual: dict[str, Any]) -> str | None:
    """The Bot Triage column, when it resolves AND is a type triage can write.

    Only such a column is worth reading during a sync: a row reported blank in a
    column triage cannot write would be judged every sweep and saved never."""
    col = _resolve_triage(column_map, "bot_triage", actual)
    if col and (actual[col] or {}).get("type") in _TRIAGE_WRITABLE["bot_triage"]:
        return col
    return None


def _required_compatible(expected: str, actual: str | None) -> bool:
    """Whether a REQUIRED field's target column can hold what we write.

    Title needs a title column and URL a url column, but a URL mapped onto a Text
    column is fine — we simply write the link as text. Text (rich_text) fields
    need a text column. Optional fields are never checked here: their value
    adapts to whatever the target column is."""
    if expected == "title":
        return actual == "title"
    if expected == "url":
        return actual in ("url", "rich_text")
    if expected == "rich_text":
        return actual == "rich_text"
    return expected == actual


def _format_for_type(actual_type: str, logical: str, *, timestamp: bool) -> dict[str, Any] | None:
    """Render one logical string value FOR the target column's actual type.

    This is the heart of target-sensitive writes: the same "$1,258,000" becomes a
    parsed Number in a number column and the verbatim string in a text column —
    the value adapts to the column, never the other way round. An unparseable
    money string in a number column, or an empty value, yields None (an empty
    cell). A target type we cannot form a value for is skipped, never guessed at,
    so a write can only ever succeed or leave a cell blank — it never 400s the row.
    """
    if actual_type == "number":
        amount = parse_money(logical)
        return {"number": amount} if amount is not None else None
    if not logical:
        return None
    if actual_type == "title":
        return {"title": _text_chunk(logical)}
    if actual_type == "rich_text":
        return {"rich_text": _text_chunk(logical)}
    if actual_type == "url":
        return {"url": logical}
    if actual_type == "select":
        # Notion creates a missing select option on write, so this is always safe.
        return {"select": {"name": logical}}
    if actual_type == "status":
        # A Notion "status" column is NOT a select: its options are fixed and the
        # API cannot create one. Writing an option the column lacks 400s the whole
        # page, so we never write a status column — better an empty cell than a
        # lost row. (Our own created databases use a select for Status, not this.)
        return None
    if actual_type == "date":
        # Only an actual timestamp field (First/Last Seen) can form a valid date;
        # a listing's text value in a date column cannot, so leave it empty.
        return {"date": {"start": logical}} if timestamp else None
    if actual_type == "email":
        return {"email": logical}
    if actual_type == "phone_number":
        return {"phone_number": logical}
    if actual_type == "checkbox":
        return {"checkbox": logical.strip().lower() in ("true", "yes", "1", "x", "✓")}
    return None


def _logical_value(prop: NotionProp, listing: Listing) -> str:
    """The field's value as a plain string, before it is shaped for a column.

    Status is always "New" on insert; the date fields are stamped now; everything
    else is read verbatim off the listing (money included — parsing is the
    column's business, decided in _format_for_type)."""
    if prop.key == "status":
        return "New"
    if prop.type == "date":
        return _now_iso()
    if prop.source:
        return getattr(listing, prop.source) or ""
    return ""


@dataclass(frozen=True)
class MapRow:
    """One row of the settings mapping table — plain data for the template."""

    key: str
    label: str
    required: bool
    selected: str      # the column currently mapped, "" when unmapped or "don't sync"
    dont_sync: bool     # True only when an optional field is explicitly set to None
    saved_type: str     # display type of the mapped column, "" when none/missing
    # A field written only by triage, shown under its own subheading so nobody
    # expects an ordinary sweep to fill it in.
    triage: bool = False


def build_map_rows(column_map: ColumnMap | None, columns: dict[str, str]) -> list[MapRow]:
    """The mapping table view: one row per field, its current selection, and the
    display type of the column it lands in. `columns` is name -> notion type.

    The listing fields come first, then the triage fields (`triage=True`), each
    showing the column it would really write to — for a triage field that
    includes a same-named column picked up without being mapped."""
    rows: list[MapRow] = []
    for prop in KNOWN_PROPS:
        target = _resolve(column_map, prop.key)
        selected = target if (target and target in columns) else ""
        rows.append(
            MapRow(
                key=prop.key,
                label=prop.name,
                required=prop.required,
                selected=selected,
                dont_sync=(bool(column_map) and prop.key in column_map and column_map[prop.key] is None),
                saved_type=_display(columns.get(selected)) or "" if selected else "",
            )
        )
    for prop in TRIAGE_PROPS:
        selected = _resolve_triage(column_map, prop.key, columns) or ""
        rows.append(
            MapRow(
                key=prop.key,
                label=prop.name,
                required=False,
                selected=selected,
                dont_sync=bool(column_map) and prop.key in column_map and column_map[prop.key] is None,
                saved_type=_display(columns.get(selected)) or "" if selected else "",
                triage=True,
            )
        )
    return rows


# ── transport ───────────────────────────────────────────────────────────────


class NotionClient:
    """Rate-limited httpx wrapper. Notion is the only thing this app talks to
    in bulk, and a 50-card sweep will hit its limits without help."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        if not token:
            raise NotionAuthError(
                "No Notion API token is configured. Add one under Settings — create an "
                "integration at notion.so/my-integrations, then share your database with it."
            )
        self._token = token
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": API_VERSION,
            "Content-Type": "application/json",
        }

    async def _throttle(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last_request
            if gap < _MIN_REQUEST_INTERVAL_SEC:
                await asyncio.sleep(_MIN_REQUEST_INTERVAL_SEC - gap)
            self._last_request = time.monotonic()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            await self._throttle()
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method, f"{API}{path}", headers=self._headers(), **kwargs
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(2**attempt * 0.5)
                continue

            if resp.status_code == 429:
                # Honour Notion's own backoff rather than guessing at it.
                delay = float(resp.headers.get("Retry-After", 2**attempt))
                logger.warning("notion rate limited; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(2**attempt * 0.5)
                last_exc = NotionError(f"Notion returned {resp.status_code}")
                continue
            return self._decode(resp)

        raise NotionError(
            f"Notion did not respond successfully after {_MAX_RETRIES} attempts: {last_exc}"
        )

    def _decode(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code == 200:
            return resp.json()

        try:
            body = resp.json()
            message = body.get("message", resp.text)
            code = body.get("code", "")
        except ValueError:
            message, code = resp.text, ""

        if resp.status_code == 401:
            raise NotionAuthError(
                f"Notion rejected the API token. Check it was copied whole from your "
                f"integration's page. ({message})"
            )
        if resp.status_code == 403:
            raise NotionAuthError(
                f"The token is valid but not allowed to do this. Most often the "
                f"integration has not been given access — open the page or database in "
                f"Notion, then Share it with your integration. ({message})"
            )
        if resp.status_code == 404:
            raise NotionNotFound(
                f"Notion could not find that page or database. Either the id is wrong or "
                f"it has not been shared with your integration — an unshared database is "
                f"invisible to the API, which reports it as missing. ({message})"
            )
        raise NotionError(f"Notion refused this request ({resp.status_code} {code}): {message}")


# ── the store ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Row:
    page_id: str
    listing_id: str
    normalized_url: str
    # The row's Bot Triage value: None when the column was not read (not
    # resolved, or a type triage cannot write), "" when it was read and is blank.
    # The difference is the whole point — only "" makes a row triage backlog.
    triage: str | None = None


@dataclass(frozen=True)
class NotionTriageTarget(TriageTarget):
    """Where triage writes in one Notion database, for one sweep.

    `types` is each resolved field's actual column type, read once so every
    write renders for the column it lands in. `client` is the ONE NotionClient
    the whole triage phase shares — the row writes and the archive appends alike.
    Each NotionClient paces itself separately, so a client per call would let a
    burst of writes run at several times Notion's rate and earn 429s mid-sweep.
    """

    types: dict[str, str] = field(default_factory=dict)
    client: "NotionClient | None" = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class DatabaseRef:
    id: str
    title: str
    url: str = ""


class NotionStore:
    """ListingStore over a Notion database."""

    def __init__(self, token: str) -> None:
        self._client = NotionClient(token)

    # ── UI-facing operations (not part of ListingStore) ─────────────────────
    async def whoami(self) -> str:
        """The integration's own name — the cheapest proof a token works."""
        me = await self._client.request("GET", "/users/me")
        return me.get("name") or me.get("bot", {}).get("workspace_name") or "Notion integration"

    async def list_databases(self) -> list[DatabaseRef]:
        """Every database shared with this integration.

        Deliberately a picker rather than a text box for a database id: a
        non-technical user should never have to know a Notion URL contains a
        32-hex id, and an empty list is itself the diagnosis (nothing shared yet).
        """
        found: list[DatabaseRef] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"value": "database", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            data = await self._client.request("POST", "/search", json=body)
            for res in data.get("results", []):
                found.append(
                    DatabaseRef(
                        id=res["id"],
                        title="".join(t.get("plain_text", "") for t in res.get("title", []))
                        or "(untitled)",
                        url=res.get("url", ""),
                    )
                )
            if not data.get("has_more"):
                return found
            cursor = data.get("next_cursor")

    async def list_parent_pages(self) -> list[DatabaseRef]:
        """Pages that could parent a new database."""
        found: list[DatabaseRef] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"value": "page", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            data = await self._client.request("POST", "/search", json=body)
            for res in data.get("results", []):
                # A page that is itself a database row cannot parent a database.
                if res.get("parent", {}).get("type") == "database_id":
                    continue
                title = ""
                for prop in res.get("properties", {}).values():
                    if prop.get("type") == "title":
                        title = _plain_title(prop)
                found.append(DatabaseRef(id=res["id"], title=title or "(untitled)",
                                         url=res.get("url", "")))
            if not data.get("has_more"):
                return found
            cursor = data.get("next_cursor")

    async def create_database(self, parent_page_id: str, title: str = "Business Listings") -> DatabaseRef:
        """Create a database with the full schema, under a page the user picked.

        Only ever called from an explicit click. Nothing in the sync path may
        call this — see rule 1 at the top of this module.

        The triage columns are created too, so a database made here can be
        triaged with no further setup. They stay empty until a sweep is asked to
        triage; nothing else ever writes them.
        """
        data = await self._client.request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": {p.name: p.create for p in (*KNOWN_PROPS, *TRIAGE_PROPS)},
            },
        )
        return DatabaseRef(
            id=data["id"],
            title="".join(t.get("plain_text", "") for t in data.get("title", [])) or title,
            url=data.get("url", ""),
        )

    # ── ListingStore ────────────────────────────────────────────────────────
    async def verify_schema(
        self, db_id: str, column_map: ColumnMap | None = None
    ) -> SchemaReport:
        """Inspect and report. Reads only — never repairs what it finds.

        With no `column_map` this is identity mapping: exactly the pre-mapping
        behaviour, which is what keeps a correctly-named database working with no
        stored map. With a map, each field is judged against the column it is
        mapped to (or found unmapped)."""
        data = await self._client.request("GET", f"/databases/{db_id}")
        return self._report_from(db_id, data, column_map)

    async def column_types(self, db_id: str) -> dict[str, str]:
        """The database's columns as name -> Notion type. Feeds the default map
        and the settings mapping table."""
        data = await self._client.request("GET", f"/databases/{db_id}")
        return {name: prop.get("type", "") for name, prop in data.get("properties", {}).items()}

    def _report_from(
        self, db_id: str, data: dict[str, Any], column_map: ColumnMap | None = None
    ) -> SchemaReport:
        actual = data.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in data.get("title", [])) or "(untitled)"

        if not column_map:
            return self._legacy_report(db_id, title, actual)

        missing_required: list[PropIssue] = []
        mismatched_required: list[PropIssue] = []
        missing_recommended: list[PropIssue] = []
        mismatched_recommended: list[PropIssue] = []
        mapped_targets: set[str] = set()

        for prop in KNOWN_PROPS:
            col = column_map.get(prop.key)
            if col:
                mapped_targets.add(col)
            found = actual.get(col) if col else None

            if prop.required:
                if not col or found is None:
                    # Unmapped, or mapped to a column the database no longer has:
                    # either way syncing is blocked until the user picks one.
                    missing_required.append(
                        PropIssue(prop.name, _display(prop.type), None, True, prop.consequence)
                    )
                elif not _required_compatible(prop.type, found.get("type")):
                    mismatched_required.append(
                        PropIssue(
                            prop.name, _display(prop.type), _display(found.get("type")),
                            True, prop.consequence,
                        )
                    )
            else:
                # Optional. "Don't sync" (col is None) is a fine, deliberate
                # choice. Mapped to an existing column is fine at ANY type — the
                # write adapts the value to it, so a Number field in a Text column
                # simply saves as text, with no nag. The only real problem is a map
                # that points at a column the database does not have.
                if col and found is None:
                    missing_recommended.append(
                        PropIssue(prop.name, _display(prop.type), None, False, prop.consequence)
                    )

        # A column triage writes is no longer one we never touch. The triage
        # fields are otherwise invisible to this report — they neither block nor
        # count against "complete" — because only a triaging sweep needs them,
        # and it checks them itself (prepare_triage).
        mapped_targets.update(_triage_columns(column_map, actual).values())

        return SchemaReport(
            db_id=db_id,
            title=title,
            missing_required=missing_required,
            mismatched_required=mismatched_required,
            missing_recommended=missing_recommended,
            mismatched_recommended=mismatched_recommended,
            untouched=sorted(n for n in actual if n not in mapped_targets),
        )

    def _legacy_report(self, db_id: str, title: str, actual: dict[str, Any]) -> SchemaReport:
        """Identity mapping: field name must match column name at the expected
        type. Unchanged from before the column map existed."""
        missing_required: list[PropIssue] = []
        mismatched_required: list[PropIssue] = []
        missing_recommended: list[PropIssue] = []
        mismatched_recommended: list[PropIssue] = []

        for prop in KNOWN_PROPS:
            found = actual.get(prop.name)
            if found is None:
                issue = PropIssue(
                    prop.name, _display(prop.type), None, prop.required, prop.consequence
                )
                (missing_required if prop.required else missing_recommended).append(issue)
            elif found.get("type") != prop.type:
                issue = PropIssue(
                    prop.name, _display(prop.type), _display(found.get("type")),
                    prop.required, prop.consequence,
                )
                (mismatched_required if prop.required else mismatched_recommended).append(issue)

        # Same-named triage columns are ours to write (only when triaging), so
        # they are not "untouched" either.
        triage_cols = set(_triage_columns(None, actual).values())
        return SchemaReport(
            db_id=db_id,
            title=title,
            missing_required=missing_required,
            mismatched_required=mismatched_required,
            missing_recommended=missing_recommended,
            mismatched_recommended=mismatched_recommended,
            untouched=sorted(n for n in actual if n not in PROPS_BY_NAME and n not in triage_cols),
        )

    async def _scan(
        self, db_id: str, actual: dict[str, Any], column_map: ColumnMap | None = None
    ) -> list[_Row]:
        """Every row's dedupe keys and page id.

        Reads the two dedupe keys from the columns they are MAPPED to (the user's
        real column names), so dedupe works under any mapping. Asks Notion for
        only those two properties. On a database with forty columns and a thousand
        rows that is the difference between a few hundred KB and tens of MB per
        sweep.

        When Bot Triage resolves to a column triage can write, that one column is
        asked for too, so the same pass says which known rows are still blank —
        the triage backlog — at the cost of one small property per row rather
        than a second scan. Otherwise it is not read, and every row's `triage`
        stays None ("not read"), never "" ("blank").
        """
        id_col = _resolve(column_map, "listing_id")
        url_col = _resolve(column_map, "normalized_url")
        triage_col = _decision_column(column_map, actual)
        wanted = list(dict.fromkeys(c for c in (id_col, url_col, triage_col) if c and c in actual))
        params = [("filter_properties", actual[c]["id"]) for c in wanted]

        rows: list[_Row] = []
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = await self._client.request(
                "POST", f"/databases/{db_id}/query", json=body, params=params
            )
            for page in data.get("results", []):
                props = page.get("properties", {})
                rows.append(
                    _Row(
                        page_id=page["id"],
                        listing_id=self._read_key(props, id_col, actual),
                        normalized_url=self._read_key(props, url_col, actual),
                        triage=self._read_decision(props, triage_col, actual),
                    )
                )
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    @staticmethod
    def _read_decision(props: dict[str, Any], col: str | None, actual: dict[str, Any]) -> str | None:
        """A row's Bot Triage as text: the Select option's name, or the Text.

        None when the column was not read, or when the page came back without it
        at all. That second case is deliberately not "blank": treating a value
        Notion simply did not return as empty would re-triage the row and write
        over a decision someone already made.
        """
        if not col or col not in props:
            return None
        prop = props[col] or {}
        if (actual.get(col) or {}).get("type") == "select":
            return ((prop.get("select") or {}).get("name") or "").strip()
        return _plain(prop).strip()

    @staticmethod
    def _read_key(props: dict[str, Any], col: str | None, actual: dict[str, Any]) -> str:
        """Read a dedupe key out of a page, honouring the mapped column's type so
        a listing id kept in a Title, or a URL in a url column, still reads back."""
        if not col:
            return ""
        col_type = (actual.get(col) or {}).get("type", "rich_text")
        prop = props.get(col, {})
        if col_type == "title":
            return _plain_title(prop)
        if col_type == "url":
            return prop.get("url") or ""
        return _plain(prop)

    @staticmethod
    def _index_of(rows: list[_Row]) -> DedupeIndex:
        index = DedupeIndex(
            listing_ids={r.listing_id for r in rows if r.listing_id},
            normalized_urls={r.normalized_url for r in rows if r.normalized_url},
        )
        # A key held by two rows means the row the upsert would find: the last.
        by_id = {r.listing_id: r for r in rows if r.listing_id}
        by_url = {r.normalized_url: r for r in rows if r.normalized_url}
        index.decisions_by_id = {k: r.triage for k, r in by_id.items() if r.triage is not None}
        index.decisions_by_url = {k: r.triage for k, r in by_url.items() if r.triage is not None}
        return index

    async def index(self, db_id: str, column_map: ColumnMap | None = None) -> DedupeIndex:
        data = await self._client.request("GET", f"/databases/{db_id}")
        return self._index_of(await self._scan(db_id, data.get("properties", {}), column_map))

    def _properties_for_mapped(
        self, listing: Listing, actual: dict[str, Any], column_map: ColumnMap, *, insert: bool
    ) -> dict[str, Any]:
        """Render a listing's properties under an explicit column map.

        Iterates the map, and for each mapped field reads the TARGET column's
        actual type and formats the value for that type. Two guarantees are
        load-bearing here: we ONLY ever write columns named in the map, and only
        when the database actually has them — a field mapped to a column that was
        since deleted is skipped, never re-created. Everything else in the user's
        database is invisible to this write.
        """
        out: dict[str, Any] = {}
        for prop in KNOWN_PROPS:
            if not (insert or prop.refresh):
                continue
            col = column_map.get(prop.key)
            if not col:
                continue  # unmapped or explicitly "don't sync"
            found = actual.get(col)
            if found is None:
                continue  # mapped to a column the database does not have — never create it
            rendered = _format_for_type(
                found.get("type", ""), _logical_value(prop, listing),
                timestamp=(prop.type == "date"),
            )
            if rendered is not None:
                out[col] = rendered
        return out

    def _properties_for(self, listing: Listing, actual: dict[str, Any], *, insert: bool) -> dict[str, Any]:
        """Render only properties we own AND the database actually has AND at the
        type we expect.

        The three conditions are each load-bearing. Owning it is rule 2. Having
        it lets a database with only the required four still sync instead of
        400ing on an EBITDA column that was never created. Matching the type
        means a user whose 'Asking Price' is text keeps their text — we skip the
        column and say so in the schema report, rather than failing the write or
        silently converting their data.
        """
        out: dict[str, Any] = {}
        for prop in KNOWN_PROPS:
            if not (insert or prop.refresh):
                continue
            found = actual.get(prop.name)
            if found is None or found.get("type") != prop.type or prop.render is None:
                continue
            value = getattr(listing, prop.source) if prop.source else None
            rendered = prop.render(value)
            if rendered is not None:
                out[prop.name] = rendered
        return out

    def _properties(
        self, listing: Listing, actual: dict[str, Any], column_map: ColumnMap | None,
        *, insert: bool,
    ) -> dict[str, Any]:
        """A listing's properties for a new row (`insert`) or a known one.

        A known row gets only the `refresh` columns. Both go through the same
        renderer, so a known row is written exactly where a new one would be:
        under a map the value adapts to whatever column it lands in; under
        identity the type must match, so a mistyped Last Synced At is skipped
        rather than written as text."""
        if column_map:
            return self._properties_for_mapped(listing, actual, column_map, insert=insert)
        return self._properties_for(listing, actual, insert=insert)

    async def upsert_new(
        self, db_id: str, listings: list[Listing], column_map: ColumnMap | None = None
    ) -> UpsertResult:
        """Insert listings that are not already there; on known rows, refresh only
        what the sweep owns outright.

        Existing rows get only the `refresh` columns written: `Last Synced At`,
        which the schema defines as "set on every sync" and which is the only
        thing making a stale listing distinguishable from a live one, and the
        `Excerpt`, so the row says what the card says now. Every other column on
        an existing row, ours or the user's, is left alone: a Status moved to
        'Review' or a note typed into a column we have never heard of survives
        every sweep. The refresh rides the one PATCH a known row already cost,
        so it adds no request unless Last Synced At is not being written.

        A column at a type we do not write is skipped, not fought over, and the
        skip is reported. This is the common case, not an exotic one: anyone who
        already keeps a listings database built it by hand with text prices, and
        Notion rejects the *entire page* if one property's type is wrong — so
        without the skip, the single most likely real-world database would fail
        every row rather than lose one column.
        """
        data = await self._client.request("GET", f"/databases/{db_id}")
        schema = self._report_from(db_id, data, column_map)
        if not schema.usable:
            raise SchemaInvalid(schema)

        actual = data.get("properties", {})
        rows = await self._scan(db_id, actual, column_map)
        index = self._index_of(rows)
        by_listing_id = {r.listing_id: r for r in rows if r.listing_id}
        by_url = {r.normalized_url: r for r in rows if r.normalized_url}

        new = existing = 0
        # The listings actually inserted, each stamped with the page id Notion
        # minted for it, so the caller can file the fresh rows without re-querying.
        new_listings: list[Listing] = []
        # Known rows whose Bot Triage was read and is blank — the backlog a
        # triaging sweep heals. Only ever "" qualifies: None means the column
        # was not read, and must never read as "every row is blank". Keyed by
        # page so a card seen twice in one sweep is one row of backlog.
        untriaged: dict[str, Listing] = {}
        for listing in listings:
            if index.contains(listing):
                existing += 1
                row = by_listing_id.get(listing.listing_id) or by_url.get(listing.normalized_url)
                if not row:
                    continue
                if row.triage == "" and row.page_id not in untriaged:
                    untriaged[row.page_id] = listing.model_copy(update={"synced_row_id": row.page_id})
                refreshed = self._properties(listing, actual, column_map, insert=False)
                if refreshed:
                    await self._client.request(
                        "PATCH", f"/pages/{row.page_id}", json={"properties": refreshed},
                    )
                continue

            props = self._properties(listing, actual, column_map, insert=True)
            created = await self._client.request(
                "POST",
                "/pages",
                json={"parent": {"database_id": db_id}, "properties": props},
            )
            new += 1
            # The POST response's `id` is the created page — carry it back on a
            # copy of the listing so nothing mutates the caller's object.
            new_listings.append(listing.model_copy(update={"synced_row_id": created.get("id", "")}))
            # Within one sweep the same listing can appear twice (paging overlap);
            # without this the second copy would be inserted again.
            if listing.listing_id:
                index.listing_ids.add(listing.listing_id)
            if listing.normalized_url:
                index.normalized_urls.add(listing.normalized_url)

        # Exactly the recommended columns this database cannot hold, from the
        # report we already built — so what we tell the user matches what the
        # write actually did, rather than being a second guess at it.
        skipped = [*schema.missing_recommended, *schema.mismatched_recommended]
        logger.info(
            "upsert into %s: %d new, %d existing, skipped %s",
            db_id, new, existing, [i.name for i in skipped] or "nothing",
        )
        return UpsertResult(
            new=new, existing=existing, db_id=db_id, skipped=skipped,
            new_listings=new_listings, untriaged=list(untriaged.values()),
        )

    # ── triage ──────────────────────────────────────────────────────────────
    async def prepare_triage(
        self, db_id: str, column_map: ColumnMap | None = None
    ) -> NotionTriageTarget:
        """Resolve the four triage columns and their types, once per sweep.

        Reads the database and nothing else. Bot Triage must resolve to a Select
        or Text column, or this raises TriageUnavailable saying which fix applies
        (add the column, map one, or change its type) — before anything is
        judged. The three optional columns are written when they resolve to a
        type that can hold them and quietly left out when they do not resolve;
        one that resolves to the wrong kind of column, or to a column a listing
        field already writes (triage would overwrite the listing's own data), is
        left out with a note saying so.
        """
        data = await self._client.request("GET", f"/databases/{db_id}")
        actual = data.get("properties", {})
        taken = self._listing_columns(column_map, actual)

        fields: dict[str, str] = {}
        types: dict[str, str] = {}
        notes: list[str] = []
        for prop in TRIAGE_PROPS:
            col = _resolve_triage(column_map, prop.key, actual)
            if col is None:
                if prop.key == "bot_triage":
                    raise TriageUnavailable(self._no_decision_column(column_map, actual))
                continue
            col_type = (actual[col] or {}).get("type", "")
            if col in taken:
                problem = (
                    f"'{col}' is where {taken[col]} is saved, so triage won't write "
                    f"{prop.name} there — it would overwrite the listing's own data. "
                    f"Pick another column for {prop.name} under Settings → Notion."
                )
            elif col_type not in _TRIAGE_WRITABLE[prop.key]:
                problem = self._wrong_triage_type(prop, col, col_type)
            else:
                fields[prop.key] = col
                types[prop.key] = col_type
                continue
            if prop.key == "bot_triage":
                raise TriageUnavailable(problem)
            notes.append(problem)

        return NotionTriageTarget(
            db_id=db_id, fields=fields, notes=tuple(notes), types=types, client=self._client,
        )

    async def write_triage(
        self, target: TriageTarget, row_id: str, decision: str, reason: str,
        triaged_at: datetime, criteria_version: str,
    ) -> None:
        """Write one row's decision into the columns `target` resolved — and no
        other column, the listing's included. One PATCH, on the target's shared
        client; any refusal from Notion propagates as a NotionError, because a
        decision that was not saved is a failure, not a skip."""
        if not isinstance(target, NotionTriageTarget):
            raise TypeError("write_triage needs the target prepare_triage returned")
        if not decision.strip():
            # Rendering a blank decision writes nothing to Bot Triage, leaving the
            # row looking untriaged while its reason and time say otherwise.
            raise ValueError("A triage decision is required.")
        when = triaged_at if triaged_at.tzinfo else triaged_at.replace(tzinfo=timezone.utc)
        values = {
            "bot_triage": decision.strip(),
            "triage_reason": reason,
            "triaged_at": when.isoformat(),
            "criteria_version": criteria_version,
        }
        properties: dict[str, Any] = {}
        for key, col in target.fields.items():
            rendered = _format_for_type(
                target.types.get(key, ""), values[key], timestamp=(key == "triaged_at"),
            )
            if rendered is not None:
                properties[col] = rendered
        client = target.client or self._client
        await client.request("PATCH", f"/pages/{row_id}", json={"properties": properties})

    @staticmethod
    def _listing_columns(column_map: ColumnMap | None, actual: dict[str, Any]) -> dict[str, str]:
        """The columns the upsert writes, as column -> the field's name. Triage
        stays out of these, so it never overwrites a listing's own data."""
        out: dict[str, str] = {}
        for prop in KNOWN_PROPS:
            col = _resolve(column_map, prop.key)
            if col and col in actual:
                out.setdefault(col, prop.name)
        return out

    @staticmethod
    def _no_decision_column(column_map: ColumnMap | None, actual: dict[str, Any]) -> str:
        """Why Bot Triage resolves to nothing, as the one fix that applies."""
        name = TRIAGE_BY_KEY["bot_triage"].name
        if column_map and "bot_triage" in column_map:
            chosen = column_map["bot_triage"]
            if chosen is None:
                return (
                    f"{name} is set to \"don't write\" in Settings → Notion, so there is "
                    f"nowhere to record triage decisions. Pick a column for it there."
                )
            return (
                f"{name} is mapped to '{chosen}', which this database no longer has. Pick "
                f"another column for it under Settings → Notion."
            )
        return (
            f"This database has no '{name}' column, so there is nowhere to record triage "
            f"decisions. Add a Select column named {name} in Notion, or map one under "
            f"Settings → Notion."
        )

    @staticmethod
    def _wrong_triage_type(prop: NotionProp, col: str, col_type: str) -> str:
        """Why `col` cannot hold this triage field, in the words on the screen."""
        allowed = " or ".join(_display(t) for t in _TRIAGE_WRITABLE[prop.key])
        if col_type == "status":
            return (
                f"'{col}' is a Status column. Notion doesn't let this app add options to "
                f"a Status column, so triage can't write {prop.name} there. Change it to "
                f"a {allowed} column in Notion, or pick another column under "
                f"Settings → Notion."
            )
        return (
            f"'{col}' is a {_display(col_type) or 'different kind of'} column, but "
            f"{prop.name} needs a {allowed} column. Change it in Notion, or pick another "
            f"column under Settings → Notion."
        )
