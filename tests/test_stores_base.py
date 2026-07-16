"""The store contract: dedupe order, and the boundary that keeps Notion out of
the scrape path."""
from __future__ import annotations

from app.models import Listing
from app.stores.base import DedupeIndex, PropIssue, SchemaReport


def listing(**kw) -> Listing:
    return Listing(**{"listing_id": "2485121", "normalized_url": "bizbuysell.com/a", **kw})


class TestDedupeOrder:
    def test_known_listing_id_is_existing(self):
        assert DedupeIndex(listing_ids={"2485121"}).contains(listing())

    def test_known_url_is_existing(self):
        assert DedupeIndex(normalized_urls={"bizbuysell.com/a"}).contains(listing())

    def test_unknown_is_new(self):
        assert not DedupeIndex(listing_ids={"other"}, normalized_urls={"other"}).contains(listing())

    def test_listing_id_matches_even_when_the_url_moved(self):
        # The reason listing_id comes first: it survives a re-listing under a new
        # slug, where the URL alone would insert a duplicate.
        index = DedupeIndex(listing_ids={"2485121"}, normalized_urls={"bizbuysell.com/old-slug"})
        assert index.contains(listing(normalized_url="bizbuysell.com/new-slug"))

    def test_url_matches_even_when_the_id_changed(self):
        # And the reason it is not the only key: a duplicate row is worse than a
        # redundant check, and the same URL is the same listing.
        index = DedupeIndex(listing_ids={"old-id"}, normalized_urls={"bizbuysell.com/a"})
        assert index.contains(listing(listing_id="new-id"))

    def test_a_listing_with_no_keys_is_never_matched(self):
        # Blank must not collide with the blanks already in the index, or one
        # id-less row would suppress every later id-less listing.
        index = DedupeIndex(listing_ids={""}, normalized_urls={""})
        assert not index.contains(Listing())

    def test_empty_index_matches_nothing(self):
        assert not DedupeIndex().contains(listing())


class TestSchemaReport:
    def test_usable_turns_only_on_the_required_four(self):
        report = SchemaReport(
            db_id="d",
            missing_recommended=[PropIssue("EBITDA", "number", None, False)],
        )
        assert report.usable and not report.complete

    def test_a_missing_required_prop_blocks(self):
        report = SchemaReport(
            db_id="d", missing_required=[PropIssue("Listing ID", "rich_text", None, True)]
        )
        assert not report.usable

    def test_a_mismatched_required_prop_blocks(self):
        report = SchemaReport(
            db_id="d", mismatched_required=[PropIssue("URL", "url", "rich_text", True)]
        )
        assert not report.usable

    def test_clean_report_is_complete(self):
        assert SchemaReport(db_id="d").complete


def test_notion_store_satisfies_the_protocol():
    from app.stores.base import ListingStore
    from app.stores.notion import NotionStore

    assert isinstance(NotionStore("ntn_x"), ListingStore)


def test_the_store_contract_does_not_depend_on_notion():
    """base.py is what scrape logic imports. A Notion import or identifier here
    would make the 'pluggable' claim false on the spot, and the next store would
    inherit Notion's vocabulary along with it.

    Checks imports and identifiers rather than the file's text: the docstring
    names Notion on purpose, to say it is one implementation and not the
    interface. Prose about the boundary is not a breach of it.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "stores" / "base.py").read_text())

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [node.module or ""] + [a.name for a in node.names]
    assert not [name for name in imported if "notion" in name.lower()]

    identifiers = [
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    ] + [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert not [name for name in identifiers if "notion" in name.lower()]
