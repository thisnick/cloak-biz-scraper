"""Exercise NotionStore against a real Notion workspace, then clean up after itself.

The unit tests fake the API, which proves we send the right bodies but not that
Notion accepts them. This proves the latter — and the properties that only mean
something against a database a human owns: that a column someone added survives a
sync, that money lands sortable, that "Not Disclosed" lands empty rather than zero.

  python scripts/verify_notion.py --parent <page-id> [--readonly-db <db-id>]
                                  [--clone-shape-from <db-id>] [--keep]

Everything is created under one clearly-named scratch page and archived at the
end, so a run against a real workspace leaves nothing behind. The two flags that
touch an existing database only ever **read** it:

  --readonly-db        report on its schema; never written to.
  --clone-shape-from   copy its column names and types (NOT its rows) into a
                       scratch database, and run the destructive checks against
                       the copy. This is the realistic case and the reason the
                       flag exists: anyone who already keeps a listings database
                       built it by hand, with text prices and thirty columns we
                       have never heard of. Testing only against a database we
                       created ourselves would prove nothing about theirs.

Reads NOTION_API_TOKEN from the environment or ./.env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Listing  # noqa: E402
from app.stores.money import parse_money  # noqa: E402
from app.stores.notion import PROPS_BY_NAME, NotionStore  # noqa: E402

# A user's own columns, added after we created the database. Nothing in the app
# knows these names — which is the point.
USER_COLUMN = "Key Risks / Notes"
USER_RATING = "My Rating"

# Every object this script creates carries this prefix, so anything it leaves
# behind after a crash is unambiguously its own and safe to delete.
SCRATCH = "cloak-biz-scraper TEST"

# Types Notion will let us create from scratch. A relation points at another
# database, a rollup and a formula read other columns, and a unique_id is
# generated — none can be cloned into an empty database, and trying turns a
# schema check into a debugging session about Notion's property model.
CLONEABLE = {
    "title", "rich_text", "number", "select", "multi_select", "date", "url",
    "email", "phone_number", "checkbox", "people", "files",
}


def clone_schema(props: dict) -> tuple[dict, list[str]]:
    """Rebuild someone's column layout, without their data."""
    out: dict = {}
    uncloneable: list[str] = []
    for name, spec in props.items():
        kind = spec["type"]
        if kind not in CLONEABLE:
            uncloneable.append(f"{name} ({kind})")
            continue
        if kind in ("select", "multi_select"):
            options = [
                {"name": o["name"], "color": o["color"]} for o in spec[kind].get("options", [])
            ]
            out[name] = {kind: {"options": options}}
        elif kind == "number":
            out[name] = {"number": {"format": spec["number"].get("format", "number")}}
        else:
            out[name] = {kind: {}}
    return out, uncloneable


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def sample_listings() -> list[Listing]:
    return [
        Listing(
            listing_id="2485121",
            url="https://www.bizbuysell.com/business-opportunity/high-margin-digital/2485121/",
            normalized_url="bizbuysell.com/business-opportunity/high-margin-digital/2485121",
            title="High Margin Digital Education and Licensing Business",
            location="San Francisco, CA",
            asking_price=parse_money("$1,258,000"),
            revenue=parse_money("$3,000,000"),
            cashflow=parse_money("$500,000"),
            ebitda=parse_money("Not Disclosed"),      # → must land empty
            source="bizbuysell_serp",
        ),
        Listing(
            listing_id="2321702",
            url="https://www.bizbuysell.com/business-opportunity/underground-utility/2321702/",
            normalized_url="bizbuysell.com/business-opportunity/underground-utility/2321702",
            title="Underground Utility, Earthwork and Concrete Contractor",
            location="Sacramento, CA",
            asking_price=parse_money("$81,000 + Inventory"),  # → must land empty
            revenue=parse_money("7350000"),
            source="bizbuysell_serp",
        ),
    ]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", required=True, help="page id to create the scratch page under")
    ap.add_argument("--readonly-db", default="", help="an existing db to report on; never written")
    ap.add_argument("--clone-shape-from", default="",
                    help="copy an existing db's COLUMNS (not rows) into a scratch db and test that")
    ap.add_argument("--keep", action="store_true", help="skip cleanup (leaves a scratch page)")
    args = ap.parse_args()

    load_env()
    token = os.environ.get("NOTION_API_TOKEN", "")
    store = NotionStore(token)
    client = store._client  # noqa: SLF001 — this script is a harness, not a caller
    out: dict = {}

    out["integration"] = await store.whoami()

    # ── an existing database, read only ─────────────────────────────────────
    if args.readonly_db:
        report = await store.verify_schema(args.readonly_db)
        out["existing_database"] = {
            "title": report.title,
            "usable": report.usable,
            "complete": report.complete,
            "missing_required": [i.describe() for i in report.missing_required],
            "mismatched_required": [i.describe() for i in report.mismatched_required],
            "missing_recommended": [i.describe() for i in report.missing_recommended],
            "mismatched_recommended": [i.describe() for i in report.mismatched_recommended],
            "columns_we_never_touch": report.untouched,
        }

    # ── scratch page ────────────────────────────────────────────────────────
    scratch = await client.request(
        "POST",
        "/pages",
        json={
            "parent": {"page_id": args.parent},
            "properties": {"title": [{"text": {"content": f"{SCRATCH} — verification run"}}]},
        },
    )
    out["scratch_page"] = scratch["id"]

    try:
        # ── create ──────────────────────────────────────────────────────────
        created = await store.create_database(scratch["id"], "Verification Listings")
        report = await store.verify_schema(created.id)
        out["created_database"] = {
            "id": created.id,
            "title": created.title,
            "schema_complete": report.complete,
            "problems": [i.describe() for i in report.problems],
        }

        # ── insert ──────────────────────────────────────────────────────────
        listings = sample_listings()
        first = await store.upsert_new(created.id, listings)
        out["first_sync"] = {"new": first.new, "existing": first.existing}

        rows = await client.request("POST", f"/databases/{created.id}/query", json={})
        out["rows_after_insert"] = [_summarize(r) for r in rows["results"]]

        # ── the user makes it their own ─────────────────────────────────────
        await client.request(
            "PATCH",
            f"/databases/{created.id}",
            json={"properties": {USER_COLUMN: {"rich_text": {}},
                                 USER_RATING: {"select": {"options": [{"name": "A", "color": "green"}]}}}},
        )
        target = rows["results"][0]["id"]
        await client.request(
            "PATCH",
            f"/pages/{target}",
            json={"properties": {
                USER_COLUMN: {"rich_text": [{"text": {"content": "Owner retiring; check lease."}}]},
                USER_RATING: {"select": {"name": "A"}},
                "Status": {"select": {"name": "Review"}},  # a column we own, that they moved
            }},
        )

        # ── re-sync the same listings ───────────────────────────────────────
        second = await store.upsert_new(created.id, listings)
        out["second_sync"] = {"new": second.new, "existing": second.existing}

        after = await client.request("GET", f"/pages/{target}")
        props = after["properties"]
        out["user_data_after_resync"] = {
            "their_note": _plain(props.get(USER_COLUMN, {})),
            "their_rating": (props.get(USER_RATING, {}).get("select") or {}).get("name"),
            "status_they_moved": (props.get("Status", {}).get("select") or {}).get("name"),
            "first_seen_at": (props.get("First Seen At", {}).get("date") or {}).get("start"),
            "last_synced_at": (props.get("Last Synced At", {}).get("date") or {}).get("start"),
        }

        # ── a database with only the required four ──────────────────────────
        minimal = await client.request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": scratch["id"]},
                "title": [{"type": "text", "text": {"content": "Minimal (required only)"}}],
                "properties": {
                    "Listing Title": {"title": {}},
                    "URL": {"url": {}},
                    "Normalized URL": {"rich_text": {}},
                    "Listing ID": {"rich_text": {}},
                },
            },
        )
        minimal_report = await store.verify_schema(minimal["id"])
        minimal_sync = await store.upsert_new(minimal["id"], listings)
        out["minimal_database"] = {
            "usable": minimal_report.usable,
            "complete": minimal_report.complete,
            "missing_recommended": [i.name for i in minimal_report.missing_recommended],
            "sync": {"new": minimal_sync.new, "existing": minimal_sync.existing},
        }

        # ── a database missing a required column ────────────────────────────
        broken = await client.request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": scratch["id"]},
                "title": [{"type": "text", "text": {"content": "Broken (no dedupe keys)"}}],
                "properties": {"Listing Title": {"title": {}}, "URL": {"url": {}}},
            },
        )
        broken_report = await store.verify_schema(broken["id"])
        out["broken_database"] = {
            "usable": broken_report.usable,
            "missing_required": [i.describe() for i in broken_report.missing_required],
        }
        try:
            await store.upsert_new(broken["id"], listings)
            out["broken_database"]["refused_to_sync"] = False
        except Exception as exc:
            out["broken_database"]["refused_to_sync"] = True
            out["broken_database"]["error"] = str(exc)

        # ── a clone of somebody's real, hand-built database ──────────────────
        if args.clone_shape_from:
            out["cloned_shape"] = await check_cloned_shape(
                store, client, scratch["id"], args.clone_shape_from, listings
            )

    finally:
        if not args.keep:
            # Archive is Notion's trash, not a purge: recoverable by the owner.
            await client.request("PATCH", f"/pages/{scratch['id']}", json={"archived": True})
            out["cleanup"] = "scratch page archived"
        else:
            out["cleanup"] = "kept (--keep)"

    print(json.dumps(out, indent=2))
    return 0


async def check_cloned_shape(store, client, scratch_page: str, source_db: str, listings) -> dict:
    """The realistic case: a database a human built, cloned and then written to.

    Three things have to hold at once, and only a real database proves them:
      * the required four are fine, so it syncs (a warning, not a blocker);
      * its text money columns are skipped rather than fatal — Notion rejects the
        entire page if one property type is wrong, so without the skip every row
        would fail;
      * its thirty columns we know nothing about are still untouched afterwards.
    """
    source = await client.request("GET", f"/databases/{source_db}")  # READ ONLY
    properties, uncloneable = clone_schema(source.get("properties", {}))
    result: dict = {
        "source_db": source_db,
        "source_title": "".join(t.get("plain_text", "") for t in source.get("title", [])),
        "columns_cloned": len(properties),
        "columns_too_exotic_to_clone": uncloneable,
    }

    clone = await client.request(
        "POST",
        "/databases",
        json={
            "parent": {"type": "page_id", "page_id": scratch_page},
            "title": [{"type": "text", "text": {"content": f"{SCRATCH} — clone of real shape"}}],
            "properties": properties,
        },
    )
    report = await store.verify_schema(clone["id"])
    result["verify"] = {
        "usable": report.usable,
        "complete": report.complete,
        "missing_required": [i.describe() for i in report.missing_required],
        "mismatched_required": [i.describe() for i in report.mismatched_required],
        "degraded": [i.describe() for i in
                     [*report.missing_recommended, *report.mismatched_recommended]],
        "columns_we_never_touch": len(report.untouched),
    }

    # The write that would fail without the type check.
    synced = await store.upsert_new(clone["id"], listings)
    result["sync"] = {
        "new": synced.new,
        "existing": synced.existing,
        "skipped_and_reported": synced.skipped_names,
    }

    # Now be the user: type into the columns that are yours, not ours.
    rows = await client.request("POST", f"/databases/{clone['id']}/query", json={})
    target = rows["results"][0]["id"]
    theirs = {
        name: spec
        for name, spec in properties.items()
        if name not in PROPS_BY_NAME and spec.get("rich_text") is not None
    }
    mine = list(theirs)[:3]
    await client.request(
        "PATCH",
        f"/pages/{target}",
        json={"properties": {
            name: {"rich_text": [{"text": {"content": f"my own note in {name}"}}]}
            for name in mine
        }},
    )

    await store.upsert_new(clone["id"], listings)  # re-sync over their edits
    after = await client.request("GET", f"/pages/{target}")
    result["their_columns_after_resync"] = {
        name: _plain(after["properties"].get(name, {})) for name in mine
    }
    result["their_columns_survived"] = all(
        after["properties"][name]["rich_text"] for name in mine
    )

    # And the blocking grade, on the same real shape minus one dedupe key.
    without_key = {k: v for k, v in properties.items() if k != "Listing ID"}
    broken = await client.request(
        "POST",
        "/databases",
        json={
            "parent": {"type": "page_id", "page_id": scratch_page},
            "title": [{"type": "text", "text": {"content": f"{SCRATCH} — real shape, no Listing ID"}}],
            "properties": without_key,
        },
    )
    broken_report = await store.verify_schema(broken["id"])
    result["same_shape_missing_a_required_column"] = {
        "usable": broken_report.usable,
        "missing_required": [i.describe() for i in broken_report.missing_required],
    }
    try:
        await store.upsert_new(broken["id"], listings)
        result["same_shape_missing_a_required_column"]["refused_to_sync"] = False
    except Exception as exc:
        result["same_shape_missing_a_required_column"]["refused_to_sync"] = True
        result["same_shape_missing_a_required_column"]["error"] = str(exc)[:160]
    return result


def _plain(prop: dict) -> str:
    return "".join(p.get("plain_text", "") for p in prop.get("rich_text") or [])


def _summarize(row: dict) -> dict:
    p = row["properties"]

    def number(name):
        return p.get(name, {}).get("number")

    return {
        "title": "".join(t.get("plain_text", "") for t in p.get("Listing Title", {}).get("title", [])),
        "listing_id": _plain(p.get("Listing ID", {})),
        "asking_price": number("Asking Price"),
        "revenue": number("Revenue"),
        "cashflow": number("SDE / Cash Flow"),
        "ebitda": number("EBITDA"),
        "status": (p.get("Status", {}).get("select") or {}).get("name"),
        "types": {
            name: p[name]["type"]
            for name in ("Asking Price", "Revenue", "First Seen At")
            if name in p
        },
    }


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
