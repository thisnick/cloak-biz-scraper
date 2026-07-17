"""NotionStore against a faked Notion API.

Fakes rather than mocks the transport, so these exercise the real request bodies
and response shapes. The live-workspace checks are in scripts/verify_notion.py;
these are the ones that can assert what we send, which is where "never clobber a
user's column" is actually decided.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.models import Listing
from app.stores import notion as notion_module
from app.stores.notion import (
    API,
    NotionAuthError,
    NotionNotFound,
    NotionStore,
    SchemaInvalid,
)

DB = "db-1234"
TOKEN = "ntn_faketoken"


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Notion's 3 req/s pacing is real and wanted; waiting for it in tests is not."""
    monkeypatch.setattr(notion_module, "_MIN_REQUEST_INTERVAL_SEC", 0)


def prop(type_: str, pid: str = "x") -> dict:
    return {"id": pid, "type": type_, type_: {}}


FULL_SCHEMA = {
    "Listing Title": prop("title", "title"),
    "URL": prop("url", "u1"),
    "Normalized URL": prop("rich_text", "n1"),
    "Listing ID": prop("rich_text", "i1"),
    "Source": prop("select", "s1"),
    "Location": prop("rich_text", "l1"),
    "Asking Price": prop("number", "a1"),
    "Revenue": prop("number", "r1"),
    "SDE / Cash Flow": prop("number", "c1"),
    "EBITDA": prop("number", "e1"),
    "Status": prop("select", "st"),
    "First Seen At": prop("date", "f1"),
    "Last Synced At": prop("date", "ls"),
}

MINIMAL_SCHEMA = {k: FULL_SCHEMA[k] for k in ("Listing Title", "URL", "Normalized URL", "Listing ID")}


def db_body(props: dict, title: str = "Listings") -> dict:
    return {"id": DB, "title": [{"plain_text": title}], "properties": props}


def row(page_id: str, listing_id: str = "", normalized_url: str = "") -> dict:
    return {
        "id": page_id,
        "properties": {
            "Listing ID": {"rich_text": [{"plain_text": listing_id}] if listing_id else []},
            "Normalized URL": {
                "rich_text": [{"plain_text": normalized_url}] if normalized_url else []
            },
        },
    }


def listing(**kw) -> Listing:
    return Listing(
        **{
            "listing_id": "2485121",
            "url": "https://www.bizbuysell.com/business-opportunity/foo/2485121/",
            "normalized_url": "bizbuysell.com/business-opportunity/foo/2485121",
            "title": "A Business",
            "source": "bizbuysell_serp",
            **kw,
        }
    )


def mock_db(props: dict) -> None:
    respx.get(f"{API}/databases/{DB}").mock(return_value=httpx.Response(200, json=db_body(props)))


def mock_query(rows: list[dict]) -> None:
    respx.post(url__startswith=f"{API}/databases/{DB}/query").mock(
        return_value=httpx.Response(200, json={"results": rows, "has_more": False})
    )


# ── verify_schema ───────────────────────────────────────────────────────────


class TestVerifySchema:
    @respx.mock
    @pytest.mark.asyncio
    async def test_full_schema_is_complete(self):
        mock_db(FULL_SCHEMA)
        report = await NotionStore(TOKEN).verify_schema(DB)
        assert report.usable and report.complete
        assert report.problems == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_names_exactly_which_required_props_are_missing(self):
        mock_db({"Listing Title": prop("title", "title"), "URL": prop("url", "u1")})
        report = await NotionStore(TOKEN).verify_schema(DB)

        assert not report.usable
        assert {i.name for i in report.missing_required} == {"Normalized URL", "Listing ID"}
        described = " ".join(i.describe() for i in report.missing_required)
        assert "'Normalized URL' is missing" in described
        # "Text", not "rich_text": the user is looking at Notion's column types,
        # where our API's name for it appears nowhere.
        assert "add it as a Text column" in described
        assert "rich_text" not in described
        # And say what it costs them, not just that a type differs.
        assert "cannot tell a listing it has already saved from a new one" in described

    @respx.mock
    @pytest.mark.asyncio
    async def test_distinguishes_wrong_type_from_absent(self):
        # Different fixes: add a column vs change one that may already hold data.
        mock_db({**MINIMAL_SCHEMA, "Listing ID": prop("number", "i1")})
        report = await NotionStore(TOKEN).verify_schema(DB)

        assert not report.usable
        assert report.missing_required == []
        assert [i.name for i in report.mismatched_required] == ["Listing ID"]
        issue = report.mismatched_required[0]
        assert issue.found == "Number" and issue.expected == "Text"
        assert "is a Number column, but this app writes Text values" in issue.describe()

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_four_required_are_enough_to_sync(self):
        mock_db(MINIMAL_SCHEMA)
        report = await NotionStore(TOKEN).verify_schema(DB)
        assert report.usable, "a missing EBITDA costs a column of triage data, not the sync"
        assert not report.complete
        assert "EBITDA" in {i.name for i in report.missing_recommended}

    @respx.mock
    @pytest.mark.asyncio
    async def test_money_stored_as_text_is_a_recommended_mismatch(self):
        # The real shape of Nick's live DB, and the case §4 is opinionated about.
        schema = {**FULL_SCHEMA, "Asking Price": prop("rich_text", "a1")}
        mock_db(schema)
        report = await NotionStore(TOKEN).verify_schema(DB)
        assert report.usable, "text money is a downgrade, not a blocker"
        assert [i.name for i in report.mismatched_recommended] == ["Asking Price"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_two_grades_are_not_the_same_verdict(self):
        """The distinction the whole report exists to make.

        A missing dedupe key means nothing syncs. A text price means everything
        syncs except prices. Both are "a schema problem"; only one is a blocker,
        and telling them apart is the difference between "fix this now" and
        "fix this when you want to sort by price".
        """
        mock_db({**MINIMAL_SCHEMA, "Asking Price": prop("rich_text", "a1")})
        degraded = await NotionStore(TOKEN).verify_schema(DB)
        assert degraded.usable and not degraded.complete

        respx.get(f"{API}/databases/{DB}").mock(
            return_value=httpx.Response(
                200, json=db_body({k: v for k, v in MINIMAL_SCHEMA.items() if k != "Listing ID"})
            )
        )
        blocked = await NotionStore(TOKEN).verify_schema(DB)
        assert not blocked.usable

    @respx.mock
    @pytest.mark.asyncio
    async def test_money_mismatch_explains_the_payoff_not_the_type_system(self):
        mock_db({**FULL_SCHEMA, "Asking Price": prop("rich_text", "a1")})
        report = await NotionStore(TOKEN).verify_schema(DB)
        described = report.mismatched_recommended[0].describe()
        # "'Asking Price' is a Text column" — the words on their screen.
        assert "is a Text column, but this app writes Number values" in described
        # And the reason to care: the triage question §4 is opinionated about.
        assert "sort and filter" in described
        assert "$1–7M with SDE over $500k" in described
        assert "rich_text" not in described

    @respx.mock
    @pytest.mark.asyncio
    async def test_lists_user_columns_it_will_never_touch(self):
        schema = {**FULL_SCHEMA, "Key Risks / Notes": prop("rich_text", "k1"),
                  "Bot Triage": prop("select", "b1")}
        mock_db(schema)
        report = await NotionStore(TOKEN).verify_schema(DB)
        assert report.untouched == ["Bot Triage", "Key Risks / Notes"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_verify_never_mutates(self):
        mock_db(MINIMAL_SCHEMA)
        await NotionStore(TOKEN).verify_schema(DB)
        methods = [call.request.method for call in respx.calls]
        assert methods == ["GET"], "verify_schema must report, never repair"


# ── dedupe ──────────────────────────────────────────────────────────────────


class TestIndex:
    @respx.mock
    @pytest.mark.asyncio
    async def test_collects_both_keys(self):
        mock_db(FULL_SCHEMA)
        mock_query([row("p1", "2485121", "bizbuysell.com/a"), row("p2", "", "bizbuysell.com/b")])
        index = await NotionStore(TOKEN).index(DB)
        assert index.listing_ids == {"2485121"}
        assert index.normalized_urls == {"bizbuysell.com/a", "bizbuysell.com/b"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_follows_pagination(self):
        mock_db(FULL_SCHEMA)
        pages = [
            httpx.Response(200, json={"results": [row("p1", "1", "u/1")], "has_more": True,
                                      "next_cursor": "c2"}),
            httpx.Response(200, json={"results": [row("p2", "2", "u/2")], "has_more": False}),
        ]
        respx.post(url__startswith=f"{API}/databases/{DB}/query").mock(side_effect=pages)
        index = await NotionStore(TOKEN).index(DB)
        assert index.listing_ids == {"1", "2"}, "a second page of rows is not new listings"

    @respx.mock
    @pytest.mark.asyncio
    async def test_asks_only_for_the_dedupe_columns(self):
        mock_db(FULL_SCHEMA)
        mock_query([])
        await NotionStore(TOKEN).index(DB)
        query = [c for c in respx.calls if "query" in c.request.url.path][0]
        assert sorted(query.request.url.params.get_list("filter_properties")) == ["i1", "n1"]


# ── upsert ──────────────────────────────────────────────────────────────────


class TestUpsertNew:
    @respx.mock
    @pytest.mark.asyncio
    async def test_inserts_only_what_is_not_there(self):
        mock_db(FULL_SCHEMA)
        mock_query([row("p1", "2485121", "bizbuysell.com/business-opportunity/foo/2485121")])
        respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "new"}))
        respx.patch(url__startswith=f"{API}/pages/").mock(
            return_value=httpx.Response(200, json={"id": "p1"})
        )

        result = await NotionStore(TOKEN).upsert_new(
            DB, [listing(), listing(listing_id="9999", normalized_url="bizbuysell.com/new")]
        )
        assert (result.new, result.existing) == (1, 1)
        inserts = [c for c in respx.calls if c.request.method == "POST" and c.request.url.path.endswith("/pages")]
        assert len(inserts) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_deduplicates_within_one_sweep(self):
        # Paged SERPs overlap; the same card can arrive twice in one call.
        mock_db(FULL_SCHEMA)
        mock_query([])
        respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "new"}))
        result = await NotionStore(TOKEN).upsert_new(DB, [listing(), listing()])
        assert (result.new, result.existing) == (1, 1)

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_known_url_with_a_new_id_is_not_new(self):
        mock_db(FULL_SCHEMA)
        mock_query([row("p1", "old-id", "bizbuysell.com/business-opportunity/foo/2485121")])
        respx.patch(url__startswith=f"{API}/pages/").mock(
            return_value=httpx.Response(200, json={"id": "p1"})
        )
        result = await NotionStore(TOKEN).upsert_new(DB, [listing()])
        assert (result.new, result.existing) == (0, 1), "same URL, same listing"

    @respx.mock
    @pytest.mark.asyncio
    async def test_money_lands_as_numbers(self):
        # The listing carries what the card said; turning that into a number is
        # this store's job, so a verbatim string is the input under test.
        mock_db(FULL_SCHEMA)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(
            DB,
            [listing(asking_price="$1,258,000", revenue="$3,000,000", cashflow="$500,000")],
        )
        props = route.calls[0].request.read().decode()
        import json

        sent = json.loads(props)["properties"]
        assert sent["Asking Price"] == {"number": 1258000.0}
        assert sent["Revenue"] == {"number": 3000000.0}
        assert sent["SDE / Cash Flow"] == {"number": 500000.0}

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("verbatim", ["Not Disclosed", "", "$81,000 + Inventory"])
    async def test_money_we_cannot_be_sure_of_is_left_empty(self, verbatim):
        import json

        mock_db(FULL_SCHEMA)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(DB, [listing(asking_price=verbatim)])
        sent = json.loads(route.calls[0].request.read())["properties"]
        # Absent, not zero and not the raw text. An empty cell is visibly
        # missing, whereas a 0 would quietly join every "under $1M" filter — and
        # "$81,000 + Inventory" written as 81000 would understate the price by an
        # unknown amount while looking perfectly precise.
        assert "Asking Price" not in sent

    @respx.mock
    @pytest.mark.asyncio
    async def test_never_writes_a_column_the_user_added(self):
        import json

        schema = {**FULL_SCHEMA, "Key Risks / Notes": prop("rich_text", "k1")}
        mock_db(schema)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(DB, [listing()])
        sent = json.loads(route.calls[0].request.read())["properties"]
        assert "Key Risks / Notes" not in sent
        assert set(sent) <= set(notion_module.PROPS_BY_NAME)

    @respx.mock
    @pytest.mark.asyncio
    async def test_skips_properties_the_database_does_not_have(self):
        import json

        # A database with only the required four must sync, not 400 on EBITDA.
        mock_db(MINIMAL_SCHEMA)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(DB, [listing(asking_price="$1,000")])
        sent = json.loads(route.calls[0].request.read())["properties"]
        assert set(sent) == {"Listing Title", "URL", "Normalized URL", "Listing ID"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_skips_a_known_property_held_at_the_wrong_type(self):
        import json

        # The user's money column is text. Writing a number would 400; converting
        # it would destroy what they typed. Skip it and say so in the report.
        schema = {**FULL_SCHEMA, "Asking Price": prop("rich_text", "a1")}
        mock_db(schema)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        result = await NotionStore(TOKEN).upsert_new(DB, [listing(asking_price="$1,258,000")])
        sent = json.loads(route.calls[0].request.read())["properties"]
        assert "Asking Price" not in sent
        # The rest of the row still writes — one bad column is not a failed sync.
        assert sent["Listing Title"] == {"title": [{"type": "text", "text": {"content": "A Business"}}]}
        assert result.new == 1
        # And it must not be silent about it. Verified live: Notion rejects the
        # WHOLE page if one property's type is wrong ("Asking Price is expected
        # to be rich_text"), so this skip is what makes a hand-built database
        # work at all — which is exactly why the user has to be told it happened.
        assert "Asking Price" in result.skipped_names

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_degraded_sync_reports_every_column_it_could_not_fill(self):
        # Nick's real shape: the required four are fine, the money is text.
        mock_db({
            **FULL_SCHEMA,
            "Asking Price": prop("rich_text", "a1"),
            "Revenue": prop("rich_text", "r1"),
            "SDE / Cash Flow": prop("rich_text", "c1"),
            "EBITDA": prop("rich_text", "e1"),
        })
        mock_query([])
        respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        result = await NotionStore(TOKEN).upsert_new(DB, [listing(asking_price="$1,258,000")])

        assert result.new == 1, "a database full of text prices must still sync"
        assert sorted(result.skipped_names) == ["Asking Price", "EBITDA", "Revenue", "SDE / Cash Flow"]
        assert "sort and filter" in result.skipped[0].describe()

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_clean_sync_skips_nothing(self):
        mock_db(FULL_SCHEMA)
        mock_query([])
        respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        result = await NotionStore(TOKEN).upsert_new(DB, [listing()])
        assert result.skipped == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_new_rows_get_status_and_first_seen(self):
        import json

        mock_db(FULL_SCHEMA)
        mock_query([])
        route = respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(DB, [listing()])
        sent = json.loads(route.calls[0].request.read())["properties"]
        assert sent["Status"] == {"select": {"name": "New"}}
        assert "start" in sent["First Seen At"]["date"]
        assert "start" in sent["Last Synced At"]["date"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_existing_rows_get_only_last_synced_at(self):
        import json

        mock_db(FULL_SCHEMA)
        mock_query([row("p1", "2485121", "bizbuysell.com/business-opportunity/foo/2485121")])
        patch = respx.patch(f"{API}/pages/p1").mock(
            return_value=httpx.Response(200, json={"id": "p1"})
        )
        await NotionStore(TOKEN).upsert_new(DB, [listing()])

        sent = json.loads(patch.calls[0].request.read())["properties"]
        # A Status the user moved to 'Review', a First Seen At, a note in their
        # own column: none of it may be reset by a later sweep.
        assert list(sent) == ["Last Synced At"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refuses_a_database_it_cannot_dedupe(self):
        mock_db({"Listing Title": prop("title", "title")})
        with pytest.raises(SchemaInvalid) as exc:
            await NotionStore(TOKEN).upsert_new(DB, [listing()])
        assert "Listing ID" in str(exc.value)
        assert not [c for c in respx.calls if c.request.method == "POST"], "nothing written"

    @respx.mock
    @pytest.mark.asyncio
    async def test_never_creates_a_database(self):
        mock_db(FULL_SCHEMA)
        mock_query([])
        respx.post(f"{API}/pages").mock(return_value=httpx.Response(200, json={"id": "n"}))
        await NotionStore(TOKEN).upsert_new(DB, [listing()])
        assert not [c for c in respx.calls if c.request.url.path == "/v1/databases"], (
            "decision #5: a database appears only from an explicit click"
        )


# ── create ──────────────────────────────────────────────────────────────────


class TestCreateDatabase:
    @respx.mock
    @pytest.mark.asyncio
    async def test_creates_the_whole_schema(self):
        import json

        route = respx.post(f"{API}/databases").mock(
            return_value=httpx.Response(
                200, json={"id": "new-db", "title": [{"plain_text": "Business Listings"}]}
            )
        )
        created = await NotionStore(TOKEN).create_database("page-1", "Business Listings")
        assert created.id == "new-db"

        body = json.loads(route.calls[0].request.read())
        assert body["parent"] == {"type": "page_id", "page_id": "page-1"}
        assert set(body["properties"]) == set(notion_module.PROPS_BY_NAME)
        assert body["properties"]["Listing Title"] == {"title": {}}
        assert body["properties"]["Asking Price"] == {"number": {"format": "dollar"}}
        assert body["properties"]["Normalized URL"] == {"rich_text": {}}

    @respx.mock
    @pytest.mark.asyncio
    async def test_created_database_verifies_clean(self):
        """What we create must be what we check for — the two would drift apart
        silently if they were not the same table."""
        import json

        respx.post(f"{API}/databases").mock(
            return_value=httpx.Response(200, json={"id": DB, "title": [{"plain_text": "X"}]})
        )
        created = await NotionStore(TOKEN).create_database("page-1")
        body = json.loads(respx.calls[0].request.read())

        # Feed the created schema back as Notion would report it.
        mock_db({name: {"id": name, "type": next(iter(spec)), next(iter(spec)): {}}
                 for name, spec in body["properties"].items()})
        report = await NotionStore(TOKEN).verify_schema(created.id)
        assert report.complete, f"created schema fails our own check: {report.problems}"


# ── errors a non-technical user has to act on ───────────────────────────────


class TestErrors:
    @respx.mock
    @pytest.mark.asyncio
    async def test_bad_token(self):
        respx.get(f"{API}/databases/{DB}").mock(
            return_value=httpx.Response(401, json={"message": "API token is invalid."})
        )
        with pytest.raises(NotionAuthError, match="rejected the API token"):
            await NotionStore(TOKEN).verify_schema(DB)

    @respx.mock
    @pytest.mark.asyncio
    async def test_unshared_database_explains_sharing(self):
        # Notion reports an unshared database as 404, which reads as "wrong id"
        # and sends the user hunting for a typo instead of clicking Share.
        respx.get(f"{API}/databases/{DB}").mock(
            return_value=httpx.Response(404, json={"message": "Could not find database"})
        )
        with pytest.raises(NotionNotFound, match="shared with your integration"):
            await NotionStore(TOKEN).verify_schema(DB)

    @respx.mock
    @pytest.mark.asyncio
    async def test_403_explains_sharing_too(self):
        respx.get(f"{API}/databases/{DB}").mock(
            return_value=httpx.Response(403, json={"message": "restricted"})
        )
        with pytest.raises(NotionAuthError, match="Share it with your integration"):
            await NotionStore(TOKEN).verify_schema(DB)

    def test_no_token_at_all(self):
        with pytest.raises(NotionAuthError, match="my-integrations"):
            NotionStore("")

    @respx.mock
    @pytest.mark.asyncio
    async def test_rate_limit_is_retried_not_surfaced(self):
        respx.get(f"{API}/databases/{DB}").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json=db_body(FULL_SCHEMA)),
            ]
        )
        report = await NotionStore(TOKEN).verify_schema(DB)
        assert report.usable, "a 429 mid-sweep is Notion pacing us, not a failure"
