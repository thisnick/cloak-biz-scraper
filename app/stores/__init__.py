"""Listing stores. `base` defines the contract; `notion` is the first (and so
far only) implementation. Import the protocol from here, never a concrete store:
scrape logic that reaches for NotionStore directly is the thing this package
exists to prevent."""
from .base import DedupeIndex, ListingStore, PropIssue, SchemaReport, UpsertResult

__all__ = ["DedupeIndex", "ListingStore", "PropIssue", "SchemaReport", "UpsertResult"]
