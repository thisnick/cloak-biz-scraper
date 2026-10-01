"""Measure a triage prompt against the Bot Triage decisions already in a Listings database.

Before a new criteria text goes into the daily sweep, this answers the one
question that matters about it: on the rows that have already been decided, how
often does the server's triage agree? It runs the prompt through exactly the
request a sweep makes for each listing (`app/services/legibility.ask`: one
request per card carrying the eligibility question and the triage question —
the detail stage needs a browser per row, so it is not measured here) over
every row whose Bot Triage is REVIEW or REJECT, and prints the agreement
overall, by the month each row was created, and since a date (the day the
current criteria took effect, so old rows judged by older criteria do not blur
the number). Also: how many REVIEWs it kept (a missed REVIEW is a listing
nobody sees), how many REJECTs it rejected, and how many of these real
listings the same request would have judged not eligible (not a business for
sale now) — a generic site's sweep drops those before they are ever saved.

**Read-only.** It reads the database schema and queries its rows; it never
writes to Notion. It is not run in CI: it needs a real database and a key.

    set -a; source .env; set +a      # NOTION_API_TOKEN and DECISION_API_KEY — never printed
    python scripts/eval_triage.py <listings db id or URL> --prompt criteria.txt --since 2026-08-30

    # a database whose columns have other names than the app's defaults
    python scripts/eval_triage.py <db> --prompt criteria.txt --column excerpt="Listing Excerpt"

Get the criteria text itself with scripts/triage_prompt_from_notion.py.
Column keys for --column: title, url, location, asking_price, cashflow, ebitda,
revenue, excerpt, bot_triage.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Listing  # noqa: E402
from app.services.legibility import MIN_ELIGIBLE, ask  # noqa: E402
from app.services.triage import REJECT, REVIEW, Triager  # noqa: E402
from app.services.typesafe import (  # noqa: E402
    DEFAULT_MODEL,
    TYPESAFE_PARALLEL,
    TypeSafeClient,
    TypeSafeError,
)

# Where each field is read from, by default: the app's own column names (what
# a database it created has), then the names an older hand-built database used.
COLUMNS: dict[str, tuple[str, ...]] = {
    "title": ("Listing Title", "Name", "Title"),
    "url": ("URL",),
    "location": ("Location",),
    "asking_price": ("Asking Price",),
    "cashflow": ("SDE / Cash Flow", "Cash Flow", "SDE"),
    "ebitda": ("EBITDA",),
    "revenue": ("Revenue",),
    "excerpt": ("Excerpt", "Listing Excerpt"),
    "bot_triage": ("Bot Triage",),
}


# ── pure parts (pinned by tests/test_eval_triage.py) ────────────────────────


def notion_id(value: str) -> str:
    """The 32-hex id in a Notion id or URL, dashed the way the API prints it."""
    found = re.findall(r"[0-9a-fA-F]{32}", (value or "").replace("-", ""))
    if not found:
        raise ValueError(f"no Notion id in {value!r}")
    h = found[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def resolve_columns(schema: dict[str, Any], overrides: dict[str, str]) -> dict[str, str]:
    """Field key -> the column it is read from, for the columns that exist."""
    out: dict[str, str] = {}
    for key, names in COLUMNS.items():
        for name in ((overrides[key],) if key in overrides else names):
            if name in schema:
                out[key] = name
                break
    return out


def _money_text(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def text_of(prop: dict[str, Any] | None) -> str:
    """A property's value as the card would have shown it. A Number column
    (the app stores money as numbers) reads back as dollars, so the classifier
    sees "$2,400,000" and the multiple can be computed from it."""
    if not prop:
        return ""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in value or []).strip()
    if kind in ("select", "status"):
        return ((value or {}).get("name") or "").strip()
    if kind == "number":
        return "" if value is None else _money_text(value)
    if kind == "url":
        return value or ""
    if kind == "formula":
        inner = (value or {}).get((value or {}).get("type") or "")
        return "" if inner is None else str(inner)
    return ""


def row_of(page: dict[str, Any], columns: dict[str, str]) -> dict[str, Any]:
    """One database row as {id, created, label, listing}."""
    props = page.get("properties", {})

    def get(key: str) -> str:
        col = columns.get(key)
        return text_of(props.get(col)) if col else ""

    listing = Listing(
        url=get("url"), title=get("title"), location=get("location"),
        asking_price=get("asking_price"), cashflow=get("cashflow"), ebitda=get("ebitda"),
        revenue=get("revenue"), excerpt=get("excerpt"),
    )
    return {"id": page.get("id", ""), "created": page.get("created_time", ""),
            "label": get("bot_triage").upper(), "listing": listing}


def agreement(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement of `jev` with `label` over results that got an answer."""
    counts = collections.Counter((r["label"], r["jev"]) for r in results)
    n = len(results)
    reviews = counts[(REVIEW, REVIEW)] + counts[(REVIEW, REJECT)]
    rejects = counts[(REJECT, REJECT)] + counts[(REJECT, REVIEW)]
    return {
        "n": n,
        "agree": (counts[(REVIEW, REVIEW)] + counts[(REJECT, REJECT)]) / n if n else 0.0,
        "review_kept": counts[(REVIEW, REVIEW)], "reviews": reviews,
        "reject_rejected": counts[(REJECT, REJECT)], "rejects": rejects,
    }


def line(name: str, stats: dict[str, Any]) -> str:
    if not stats["n"]:
        return f"{name:28} n=    0"
    kept = stats["review_kept"] / stats["reviews"] if stats["reviews"] else 0.0
    rejected = stats["reject_rejected"] / stats["rejects"] if stats["rejects"] else 0.0
    return (f"{name:28} n={stats['n']:5}  agree={stats['agree']:6.1%}  "
            f"REVIEW kept {stats['review_kept']}/{stats['reviews']} ({kept:.0%})  "
            f"REJECT rejected {stats['reject_rejected']}/{stats['rejects']} ({rejected:.0%})")


def report(results: list[dict[str, Any]], since: str | None) -> list[str]:
    lines = [line("all", agreement(results))]
    if since:
        lines.append(line(f"since {since}", agreement(
            [r for r in results if r["created"][:10] >= since])))
    for month in sorted({r["created"][:7] for r in results if r["created"]}):
        lines.append(line(f"  {month}", agreement(
            [r for r in results if r["created"][:7] == month])))
    lines.extend(eligibility(results))
    return lines


def eligibility(results: list[dict[str, Any]], shown: int = 10) -> list[str]:
    """How many of these real, decided rows the same request judged not
    eligible (P < 0.5) — each one a listing a generic sweep would have dropped
    — with the lowest-scoring few named."""
    judged = [r for r in results if r.get("eligible") is not None]
    if not judged:
        return []
    low = sorted((r for r in judged if r["eligible"] < MIN_ELIGIBLE), key=lambda r: r["eligible"])
    scores = sorted(r["eligible"] for r in judged)
    median = scores[len(scores) // 2]
    out = [f"\nnot eligible (P < {MIN_ELIGIBLE}): {len(low)} of {len(judged)} "
           f"({len(low) / len(judged):.1%}); median P(eligible) {median:.2f}"]
    out += [f"  {r['eligible']:.2f}  {r['label'] or '?':6}  {(r.get('title') or '')[:70]}"
            for r in low[:shown]]
    return out


# ── the run ─────────────────────────────────────────────────────────────────


class MeteredClient(TypeSafeClient):
    """The app's client, counting requests and what OpenRouter says they cost."""

    requests = 0
    cost = 0.0

    async def _call(self, *args, **kwargs):
        reply = await super()._call(*args, **kwargs)
        self.requests += 1
        self.cost += reply.cost or 0.0
        return reply


async def dump(client, db_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The schema and every row. Reads only: a query is a read."""
    schema = (await client.request("GET", f"/databases/{db_id}")).get("properties", {})
    pages: list[dict[str, Any]] = []
    cursor = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await client.request("POST", f"/databases/{db_id}/query", json=body)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return schema, pages
        cursor = data.get("next_cursor")


async def judge(rows: list[dict[str, Any]], client, triager: Triager, concurrency: int,
                out: Path | None) -> tuple[list[dict[str, Any]], int]:
    """Each row's one request, as a sweep makes it: eligible and triage together."""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, Any]] = []
    failed = 0
    done = 0
    handle = out.open("a", encoding="utf-8") if out else None

    async def one(row: dict[str, Any]) -> None:
        nonlocal failed, done
        async with sem:
            try:
                answer = await ask(client, row["listing"], triager)
            except TypeSafeError as exc:
                failed += 1
                print(f"  no answer for {row['id']}: {exc}", file=sys.stderr)
                return
        decision = answer.triage
        result = {"id": row["id"], "created": row["created"], "label": row["label"],
                  "title": row["listing"].title,
                  "jev": decision.decision, "p_review": round(decision.p_review, 4),
                  "eligible": round(answer.eligible, 4), "model": decision.model}
        results.append(result)
        if handle:
            handle.write(json.dumps(result) + "\n")
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(rows)}", file=sys.stderr)

    try:
        await asyncio.gather(*(one(r) for r in rows))
    finally:
        if handle:
            handle.close()
    return results, failed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("database", help="the Listings database's id or URL")
    ap.add_argument("--prompt", required=True, type=Path, help="a file holding the criteria text")
    ap.add_argument("--since", help="also report rows created on or after YYYY-MM-DD")
    ap.add_argument("--limit", type=int, help="judge at most this many rows")
    ap.add_argument("--sample", type=int, help="judge a random sample of this many rows")
    ap.add_argument("--concurrency", type=int, default=TYPESAFE_PARALLEL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--column", action="append", default=[], metavar="KEY=NAME",
                    help="read a field from a differently named column")
    ap.add_argument("--out", type=Path, help="append each row's result to this JSONL file")
    a = ap.parse_args()

    token = os.environ.get("NOTION_API_TOKEN", "")
    key = os.environ.get("DECISION_API_KEY", "")
    if not token or not key:
        print("Set NOTION_API_TOKEN and DECISION_API_KEY (e.g. set -a; source .env; set +a).",
              file=sys.stderr)
        return 2
    overrides = dict(c.split("=", 1) for c in a.column)
    unknown = set(overrides) - set(COLUMNS)
    if unknown:
        print(f"Unknown --column key(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    prompt = a.prompt.read_text(encoding="utf-8")

    from app.stores.notion import NotionClient

    db_id = notion_id(a.database)
    schema, pages = await dump(NotionClient(token), db_id)
    columns = resolve_columns(schema, overrides)
    missing = [k for k in COLUMNS if k not in columns]
    print(f"{len(pages)} rows; reading {json.dumps(columns)}")
    if "bot_triage" not in columns:
        print("No Bot Triage column to compare against (use --column bot_triage=NAME).",
              file=sys.stderr)
        return 2
    if missing:
        print(f"Not found, left blank: {', '.join(missing)}")

    rows = [r for r in (row_of(p, columns) for p in pages) if r["label"] in (REVIEW, REJECT)]
    if a.sample:
        random.Random(7).shuffle(rows)
        rows = rows[: a.sample]
    if a.limit:
        rows = rows[: a.limit]

    client = MeteredClient(lambda: key, lambda: a.model)
    triager = Triager(client, prompt)
    print(f"judging {len(rows)} decided rows with criteria version {triager.version} "
          f"({a.model})")
    results, failed = await judge(rows, client, triager, a.concurrency, a.out)

    print()
    for text in report(results, a.since):
        print(text)
    if failed:
        print(f"\n{failed} row(s) got no answer from the classifier (see above).")
    print(f"\ncost ${client.cost:.4f} over {client.requests} request(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
