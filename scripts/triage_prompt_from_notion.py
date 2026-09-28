"""Print a Notion page's plain text, for use as a sweep's `triage_prompt`.

The daily sweep's criteria live on a Notion page a person edits; the scheduled
agent reads that page and passes its words to `scrape_listings(...,
triage_prompt=...)`. This does the same read from a terminal — headings, lists
and paragraphs flattened to plain text, nested items indented, the page title
left out — so you can see exactly what the classifier will be given, save it for
scripts/eval_triage.py, or pass it from a script of your own.

The text goes to stdout; its Criteria Version (what each row it decides will
record) goes to stderr, so `> criteria.txt` captures only the text.

    set -a; source .env; set +a          # NOTION_API_TOKEN — never printed
    python scripts/triage_prompt_from_notion.py <page id or URL> > criteria.txt

Read-only. The page must be shared with the integration whose token you use.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.triage import criteria_version  # noqa: E402

# Blocks that hold no criteria text of their own.
_SKIP = {"divider", "child_page", "child_database", "image", "video", "file", "pdf",
         "bookmark", "embed", "link_preview", "table_of_contents", "breadcrumb",
         "unsupported"}


def notion_id(value: str) -> str:
    """The 32-hex id in a Notion id or URL, dashed the way the API prints it."""
    found = re.findall(r"[0-9a-fA-F]{32}", (value or "").replace("-", ""))
    if not found:
        raise ValueError(f"no Notion id in {value!r}")
    h = found[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def block_text(block: dict[str, Any]) -> str:
    """A block's own text: its rich text, joined, as plain text."""
    body = block.get(block.get("type") or "") or {}
    return "".join(part.get("plain_text") or (part.get("text") or {}).get("content", "")
                   for part in body.get("rich_text") or []).strip()


def flatten(blocks: list[dict[str, Any]], depth: int = 0) -> list[str]:
    """Lines of plain text for a tree of blocks (each block's `children` already
    fetched). Headings stand apart; list items keep their bullet or number;
    nested blocks are indented two spaces per level."""
    lines: list[str] = []
    indent = "  " * depth
    number = 0
    for block in blocks:
        kind = block.get("type") or ""
        number = number + 1 if kind == "numbered_list_item" else 0
        if kind in _SKIP:
            continue
        text = block_text(block)
        if kind.startswith("heading_"):
            if lines and lines[-1] != "":
                lines.append("")
            if text:
                lines.append(indent + text)
        elif kind == "bulleted_list_item":
            lines.append(f"{indent}- {text}")
        elif kind == "numbered_list_item":
            lines.append(f"{indent}{number}. {text}")
        elif kind == "to_do":
            lines.append(f"{indent}- {text}")
        elif kind == "table_row":
            cells = [" ".join(p.get("plain_text", "") for p in cell).strip()
                     for cell in (block.get("table_row") or {}).get("cells") or []]
            lines.append(indent + " | ".join(cells))
        elif text:
            lines.append(indent + text)
        elif kind == "paragraph" and lines and lines[-1] != "":
            lines.append("")
        children = block.get("children") or []
        if children:
            # A table's rows and a column's blocks are not nested content.
            flat = kind in ("table", "column_list", "column")
            lines.extend(flatten(children, depth if flat else depth + 1))
    return lines


def to_text(blocks: list[dict[str, Any]]) -> str:
    """The page's text: flattened, runs of blank lines collapsed, trimmed."""
    out: list[str] = []
    for text in flatten(blocks):
        if text.strip() == "" and (not out or out[-1] == ""):
            continue
        out.append(text.rstrip())
    return "\n".join(out).strip() + "\n"


async def children(client, block_id: str) -> list[dict[str, Any]]:
    """Every child block of `block_id`, each with its own children attached."""
    blocks: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = await client.request("GET", f"/blocks/{block_id}/children", params=params)
        blocks.extend(data.get("results", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    for block in blocks:
        if block.get("has_children") and block.get("type") not in ("child_page",
                                                                   "child_database"):
            block["children"] = await children(client, block["id"])
    return blocks


async def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    token = os.environ.get("NOTION_API_TOKEN", "")
    if not token:
        print("Set NOTION_API_TOKEN (e.g. set -a; source .env; set +a).", file=sys.stderr)
        return 2
    from app.stores.notion import NotionClient

    text = to_text(await children(NotionClient(token), notion_id(argv[0])))
    if not text.strip():
        print("The page has no text.", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    print(f"criteria_version: {criteria_version(text)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
