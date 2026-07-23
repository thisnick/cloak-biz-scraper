"""The pluggable listing store.

Scrape logic talks to this protocol and never imports Notion. That is not
speculative generality — it is what keeps the opinionated part of this project
(how listings are extracted, normalized, and deduped) separable from the part
every user will want to swap (where the rows land). Airtable, Postgres, or a CSV
should be a new module here and nothing else.

So nothing in this file may mention a Notion type, property, or error. If a
concept cannot be expressed for a CSV, it belongs in notion.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models import Listing


@dataclass(frozen=True)
class PropIssue:
    """One thing wrong with a store's schema, in the user's vocabulary.

    `found=None` means absent. Anything else means present but the wrong type,
    which is a different fix: add a column versus change one, and changing one
    may mean converting data the user typed.

    `expected` and `found` are the type names the user sees in their own tool,
    not our API's names — someone looking at a column labelled "Text" cannot act
    on the word "rich_text". `consequence` says what this costs them, because
    "type mismatch" is a fact about our code, not a reason for them to care.
    """

    name: str
    expected: str
    found: str | None = None
    required: bool = True
    consequence: str = ""

    def describe(self) -> str:
        if self.found is None:
            head = f"'{self.name}' is missing — add it as a {self.expected} column."
        else:
            head = (
                f"'{self.name}' is a {self.found} column, but this app writes "
                f"{self.expected} values."
            )
        return f"{head} {self.consequence}".strip()

    def fix(self) -> str:
        """The action to take, in one short imperative — add a column vs change
        an existing one are genuinely different fixes."""
        if self.found is None:
            return f"add a {self.expected} column"
        return f"change it from {self.found} to {self.expected}"


@dataclass(frozen=True)
class SchemaReport:
    """The result of inspecting a store's schema. Never mutates anything."""

    db_id: str
    title: str = ""
    missing_required: list[PropIssue] = field(default_factory=list)
    mismatched_required: list[PropIssue] = field(default_factory=list)
    missing_recommended: list[PropIssue] = field(default_factory=list)
    mismatched_recommended: list[PropIssue] = field(default_factory=list)
    # Columns the user added. Listed so the UI can show that we can see them and
    # still will not touch them.
    untouched: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether syncing can work at all. Only the required four decide this;
        a missing 'EBITDA' costs you a column of triage data, not the sync."""
        return not self.missing_required and not self.mismatched_required

    @property
    def complete(self) -> bool:
        return self.usable and not self.missing_recommended and not self.mismatched_recommended

    @property
    def problems(self) -> list[PropIssue]:
        return [
            *self.missing_required,
            *self.mismatched_required,
            *self.missing_recommended,
            *self.mismatched_recommended,
        ]


@dataclass
class DedupeIndex:
    """The keys already present in the store, for deciding what is new.

    Two keys, because neither is sufficient alone. A source's listing id is
    stable across a re-listing or a URL change, so it is authoritative when
    present — but not every source exposes one. The normalized URL always exists
    and catches the rest.
    """

    listing_ids: set[str] = field(default_factory=set)
    normalized_urls: set[str] = field(default_factory=set)

    def contains(self, listing: Listing) -> bool:
        """True when this listing is already stored.

        Checks the listing id first — it is the key that survives a URL change —
        then the normalized URL. Either match counts: a row whose URL we already
        hold is the same listing even if its id changed underneath us, and a
        duplicate row is worse than a redundant check.
        """
        if listing.listing_id and listing.listing_id in self.listing_ids:
            return True
        return bool(listing.normalized_url and listing.normalized_url in self.normalized_urls)

    def __len__(self) -> int:
        return len(self.listing_ids | self.normalized_urls)


@dataclass(frozen=True)
class UpsertResult:
    """What a sync did — including what it could not do.

    `skipped` is not decoration. A user whose 'Asking Price' column is text gets
    a sync that works and rows that are quietly missing their prices; without
    this they would have to notice the empty column themselves and guess why.
    It is the difference between degrading and degrading *silently*.

    `new_listings` is the listings actually INSERTED this sweep — the `new` count
    made concrete — each carrying the store row id it was written to on its
    `page_id` field. Already-present listings are counted in `existing` but never
    appear here, so a caller can hand these straight on to whatever files the row
    (see the sweep service) without re-reading the store. What a row id IS stays
    the store's business: this protocol only knows it lands on `Listing.page_id`.
    """

    new: int = 0
    existing: int = 0
    db_id: str = ""
    skipped: list[PropIssue] = field(default_factory=list)
    new_listings: list[Listing] = field(default_factory=list)

    @property
    def skipped_names(self) -> list[str]:
        return [issue.name for issue in self.skipped]


@runtime_checkable
class ListingStore(Protocol):
    """Where listings land.

    Async because every implementation worth having is network-bound and this
    app serves an event loop; a blocking store would stall the whole process
    mid-sweep.
    """

    async def verify_schema(
        self, db_id: str, column_map: "dict[str, str | None] | None" = None
    ) -> SchemaReport:
        """Report what is missing or mismatched. Must never mutate the store.

        `column_map` is an optional {field-key -> user's column name, or None}
        override; a store with no notion of columns may ignore it."""
        ...

    async def index(
        self, db_id: str, column_map: "dict[str, str | None] | None" = None
    ) -> DedupeIndex:
        """The dedupe keys already stored, read from the mapped columns."""
        ...

    async def upsert_new(
        self, db_id: str, listings: list[Listing],
        column_map: "dict[str, str | None] | None" = None,
    ) -> UpsertResult:
        """Insert listings that are not already stored. Must never overwrite a
        column the user added, and must only write columns named in the map."""
        ...
