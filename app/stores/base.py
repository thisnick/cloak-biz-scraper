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
from datetime import datetime
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
    # Each stored row's triage decision, under the key it is found by: "" when
    # it was read and is blank. A row whose decision was not read (the store
    # has nowhere to keep one) is absent here, never "": "never read" and
    # "blank" lead to opposite actions, exactly as for `UpsertResult.untriaged`.
    decisions_by_id: dict[str, str] = field(default_factory=dict)
    decisions_by_url: dict[str, str] = field(default_factory=dict)

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

    def decision(self, listing: Listing) -> str | None:
        """The stored row's triage decision ("" when read and blank), found the
        way `contains` finds the row — by listing id, then by normalized URL.
        None when the listing is not stored or its decision was not read."""
        if listing.listing_id and listing.listing_id in self.listing_ids:
            return self.decisions_by_id.get(listing.listing_id)
        if listing.normalized_url and listing.normalized_url in self.normalized_urls:
            return self.decisions_by_url.get(listing.normalized_url)
        return None

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
    `synced_row_id` field. Already-present listings are counted in `existing` but
    never appear here, so a caller can hand these straight on to whatever files
    the row (see the sweep service) without re-reading the store. What a row id IS
    stays the store's business: this protocol only knows it lands on
    `Listing.synced_row_id`.

    `untriaged` is the already-stored rows this sync saw whose triage decision is
    still blank, each carrying its row id on `synced_row_id` — the backlog a
    triaging sweep heals alongside its new rows. It is filled only when the store
    actually read the decision: a store with nowhere to keep one returns it empty
    rather than calling every row blank, because "never read" and "blank" lead to
    opposite actions (skip it, or judge it). A row holding any decision, a
    person's included, is never listed, so a decision is made once.
    """

    new: int = 0
    existing: int = 0
    db_id: str = ""
    skipped: list[PropIssue] = field(default_factory=list)
    new_listings: list[Listing] = field(default_factory=list)
    untriaged: list[Listing] = field(default_factory=list)

    @property
    def skipped_names(self) -> list[str]:
        return [issue.name for issue in self.skipped]


class TriageUnavailable(RuntimeError):
    """The store has nowhere to record a triage decision, said so the person can
    fix it (which place is missing, or why the one chosen cannot hold it).

    Raised by `prepare_triage`, before any listing is judged: a sweep that cannot
    save a decision should refuse triage for the whole job, not spend a
    classifier call per row on verdicts it would then have to drop.
    """


@dataclass(frozen=True)
class TriageTarget:
    """Where one store records triage decisions, worked out once per sweep.

    Resolved up front, not per row, so every row in a sweep is written to the
    same places and a store that cannot take a decision says so before anything
    is judged. `fields` maps each triage field that WILL be written — "bot_triage"
    (always), "triage_reason", "triaged_at", "criteria_version" — to the person's
    own name for where it lands; a field absent from it is simply not recorded.
    `notes` says, in the person's words, why a field they seem to want was left
    out (a place of the wrong kind, say), so a partial write is never silent.
    A store extends this with whatever it needs to write, such as a connection
    shared by every write of the sweep.
    """

    db_id: str
    fields: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


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
        """The dedupe keys already stored, read from the mapped columns, with
        each row's triage decision where the store keeps one. A synced sweep
        reads this once, before its first page, to tell new listings from known
        ones."""
        ...

    async def upsert_new(
        self, db_id: str, listings: list[Listing],
        column_map: "dict[str, str | None] | None" = None,
    ) -> UpsertResult:
        """Insert listings that are not already stored. Must never overwrite a
        column the user added, and must only write columns named in the map.
        Never writes a triage field: those belong to `write_triage` alone."""
        ...

    async def prepare_triage(
        self, db_id: str, column_map: "dict[str, str | None] | None" = None
    ) -> TriageTarget:
        """Resolve where triage decisions go, once per sweep. Reads only.

        Raises TriageUnavailable when the decision itself has nowhere to go (no
        place for it, one switched off, or one that cannot hold the words
        REVIEW/REJECT); the optional fields are left out of the target instead."""
        ...

    async def write_triage(
        self, target: TriageTarget, row_id: str, decision: str, reason: str,
        triaged_at: datetime, criteria_version: str,
    ) -> None:
        """Record one row's decision in the places `target` resolved, and nothing
        else. Raises when the write fails: a decision that was not saved is a
        failure to report, never a quiet skip."""
        ...
