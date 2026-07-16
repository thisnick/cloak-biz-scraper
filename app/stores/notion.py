"""Notion implementation of ListingStore.

Three rules shape everything here, and all three come from the same place: the
database belongs to the user, not to us.

1. **Never auto-create.** A database appears only when someone clicks "Create".
   Surprise databases in someone's workspace are hostile, and a tool that
   creates one on first sync trains people not to trust it with their workspace.
2. **Never clobber a column we do not own.** We write only the properties in
   KNOWN_PROPS, and only where the user's database already has that name at that
   type. Everything else is invisible to us. That is precisely what makes
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..models import Listing
from .base import DedupeIndex, PropIssue, SchemaReport, UpsertResult

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
    # Written when the row is created and never touched again, so that a user's
    # own edits (a Status they changed, a First Seen At) are not reset by a
    # later sweep.
    insert_only: bool = False


def _plain(prop: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in prop.get("rich_text") or [])


def _plain_title(prop: dict[str, Any]) -> str:
    return "".join(part.get("plain_text", "") for part in prop.get("title") or [])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


KNOWN_PROPS: tuple[NotionProp, ...] = (
    # The four the machine cannot work without.
    NotionProp(
        "Listing Title", "title", True, {"title": {}}, source="title",
        render=lambda v: {"title": _text_chunk(v)} if v else None,
        extract=_plain_title,
        consequence="Every Notion database needs exactly one Title column; syncing is "
                    "blocked without it.",
    ),
    NotionProp(
        "URL", "url", True, {"url": {}}, source="url",
        render=lambda v: {"url": v} if v else None,
        consequence="Without it there is no link back to the original listing, so "
                    "syncing is blocked.",
    ),
    NotionProp(
        "Normalized URL", "rich_text", True, {"rich_text": {}}, source="normalized_url",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        extract=_plain, consequence=_DEDUPE_CONSEQUENCE,
    ),
    NotionProp(
        "Listing ID", "rich_text", True, {"rich_text": {}}, source="listing_id",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        extract=_plain, consequence=_DEDUPE_CONSEQUENCE,
    ),
    # Recommended: what turns a list of rows into a triage tool.
    NotionProp(
        "Source", "select", False, {"select": {}}, source="source",
        render=lambda v: {"select": {"name": v}} if v else None,
        consequence="Which site a listing came from will not be recorded. Everything "
                    "else still syncs.",
    ),
    NotionProp(
        "Location", "rich_text", False, {"rich_text": {}}, source="location",
        render=lambda v: {"rich_text": _text_chunk(v)} if v else None,
        consequence="Locations will not be recorded. Everything else still syncs.",
    ),
    NotionProp(
        "Asking Price", "number", False, {"number": {"format": "dollar"}}, source="asking_price",
        render=lambda v: {"number": v} if v is not None else None,
        consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "Revenue", "number", False, {"number": {"format": "dollar"}}, source="revenue",
        render=lambda v: {"number": v} if v is not None else None,
        consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "SDE / Cash Flow", "number", False, {"number": {"format": "dollar"}}, source="cashflow",
        render=lambda v: {"number": v} if v is not None else None,
        consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "EBITDA", "number", False, {"number": {"format": "dollar"}}, source="ebitda",
        render=lambda v: {"number": v} if v is not None else None,
        consequence=_MONEY_CONSEQUENCE,
    ),
    NotionProp(
        "Status", "select", False,
        {"select": {"options": [
            {"name": "New", "color": "blue"},
            {"name": "Review", "color": "yellow"},
            {"name": "Rejected", "color": "red"},
        ]}},
        render=lambda v: {"select": {"name": "New"}}, insert_only=True,
        consequence="New listings will not be marked 'New', so you lose the triage "
                    "workflow but not the listings.",
    ),
    NotionProp(
        "First Seen At", "date", False, {"date": {}},
        render=lambda v: {"date": {"start": _now_iso()}}, insert_only=True,
        consequence="You will not see when a listing first appeared.",
    ),
    NotionProp(
        "Last Synced At", "date", False, {"date": {}},
        render=lambda v: {"date": {"start": _now_iso()}},
        consequence="You will not be able to tell a listing that is still live from one "
                    "that has come off the market.",
    ),
)

PROPS_BY_NAME = {p.name: p for p in KNOWN_PROPS}
REQUIRED_PROPS = tuple(p for p in KNOWN_PROPS if p.required)


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
        """
        data = await self._client.request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": {p.name: p.create for p in KNOWN_PROPS},
            },
        )
        return DatabaseRef(
            id=data["id"],
            title="".join(t.get("plain_text", "") for t in data.get("title", [])) or title,
            url=data.get("url", ""),
        )

    # ── ListingStore ────────────────────────────────────────────────────────
    async def verify_schema(self, db_id: str) -> SchemaReport:
        """Inspect and report. Reads only — never repairs what it finds."""
        return self._report_from(db_id, await self._client.request("GET", f"/databases/{db_id}"))

    def _report_from(self, db_id: str, data: dict[str, Any]) -> SchemaReport:
        actual = data.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in data.get("title", [])) or "(untitled)"

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

        return SchemaReport(
            db_id=db_id,
            title=title,
            missing_required=missing_required,
            mismatched_required=mismatched_required,
            missing_recommended=missing_recommended,
            mismatched_recommended=mismatched_recommended,
            untouched=sorted(n for n in actual if n not in PROPS_BY_NAME),
        )

    async def _scan(self, db_id: str, actual: dict[str, Any]) -> list[_Row]:
        """Every row's dedupe keys and page id.

        Asks Notion for only the two key properties. On a database with forty
        columns and a thousand rows that is the difference between a few hundred
        KB and tens of MB per sweep.
        """
        wanted = [PROPS_BY_NAME["Listing ID"], PROPS_BY_NAME["Normalized URL"]]
        params = [("filter_properties", actual[p.name]["id"]) for p in wanted if p.name in actual]

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
                        listing_id=_plain(props.get("Listing ID", {})),
                        normalized_url=_plain(props.get("Normalized URL", {})),
                    )
                )
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    @staticmethod
    def _index_of(rows: list[_Row]) -> DedupeIndex:
        return DedupeIndex(
            listing_ids={r.listing_id for r in rows if r.listing_id},
            normalized_urls={r.normalized_url for r in rows if r.normalized_url},
        )

    async def index(self, db_id: str) -> DedupeIndex:
        data = await self._client.request("GET", f"/databases/{db_id}")
        return self._index_of(await self._scan(db_id, data.get("properties", {})))

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
            if prop.insert_only and not insert:
                continue
            found = actual.get(prop.name)
            if found is None or found.get("type") != prop.type or prop.render is None:
                continue
            value = getattr(listing, prop.source) if prop.source else None
            rendered = prop.render(value)
            if rendered is not None:
                out[prop.name] = rendered
        return out

    async def upsert_new(self, db_id: str, listings: list[Listing]) -> UpsertResult:
        """Insert listings that are not already there; touch nothing else.

        Existing rows get exactly one property written — `Last Synced At`, which
        the schema defines as "set on every sync" and which is the only thing
        making a stale listing distinguishable from a live one. Every other
        column on an existing row, ours or the user's, is left alone: a Status
        moved to 'Review' or a note typed into a column we have never heard of
        survives every sweep.

        A column at a type we do not write is skipped, not fought over, and the
        skip is reported. This is the common case, not an exotic one: anyone who
        already keeps a listings database built it by hand with text prices, and
        Notion rejects the *entire page* if one property's type is wrong — so
        without the skip, the single most likely real-world database would fail
        every row rather than lose one column.
        """
        data = await self._client.request("GET", f"/databases/{db_id}")
        schema = self._report_from(db_id, data)
        if not schema.usable:
            raise SchemaInvalid(schema)

        actual = data.get("properties", {})
        rows = await self._scan(db_id, actual)
        index = self._index_of(rows)
        by_listing_id = {r.listing_id: r for r in rows if r.listing_id}
        by_url = {r.normalized_url: r for r in rows if r.normalized_url}

        new = existing = 0
        touch = PROPS_BY_NAME["Last Synced At"]
        can_touch = (
            touch.name in actual and actual[touch.name].get("type") == touch.type
        )

        for listing in listings:
            if index.contains(listing):
                existing += 1
                if not can_touch:
                    continue
                row = by_listing_id.get(listing.listing_id) or by_url.get(listing.normalized_url)
                if row:
                    await self._client.request(
                        "PATCH",
                        f"/pages/{row.page_id}",
                        json={"properties": {touch.name: touch.render(None)}},
                    )
                continue

            await self._client.request(
                "POST",
                "/pages",
                json={
                    "parent": {"database_id": db_id},
                    "properties": self._properties_for(listing, actual, insert=True),
                },
            )
            new += 1
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
        return UpsertResult(new=new, existing=existing, db_id=db_id, skipped=skipped)
