"""Drive a running server's MCP endpoint over real HTTP.

The transport rules are the ones the SDK does not enforce for us, so they are
worth checking against a real socket and not only through a test client: a
proxy, a server, or an SDK upgrade can each break them without a unit test
noticing.

    python scripts/verify_mcp.py --base http://127.0.0.1:18830
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

EXPECTED_TOOLS = {
    "scrape_listings", "get_scrape_listing_results", "archive_page",
    "create_instance", "close_instance", "list_instances", "get_instance",
}

ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


def rpc(method: str, params: dict | None = None, _id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}


INIT = rpc("initialize", {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "verify_mcp", "version": "1"},
})


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18830")
    args = ap.parse_args()
    url = args.base.rstrip("/") + "/mcp"

    async with httpx.AsyncClient(timeout=30) as c:
        print("\n── stateless ──")
        r = await c.post(url, json=INIT, headers=HEADERS)
        check("initialize returns 200", r.status_code == 200, f"got {r.status_code}")
        check("no Mcp-Session-Id header", "mcp-session-id" not in {k.lower() for k in r.headers},
              "a session id would pin the conversation to one container")
        if r.status_code == 200:
            info = r.json()["result"]["serverInfo"]
            check("serverInfo names this server", info["name"] == "cloak-biz-scraper", str(info))

        r = await c.post(url, json=rpc("tools/list"), headers=HEADERS)
        tools = {t["name"] for t in r.json()["result"]["tools"]} if r.status_code == 200 else set()
        check("tools/list without a handshake", r.status_code == 200)
        check("tools/list is exactly the plan's set", tools == EXPECTED_TOOLS,
              f"missing={EXPECTED_TOOLS - tools or '-'} extra={tools - EXPECTED_TOOLS or '-'}")

        print("\n── GET is refused ──")
        r = await c.get(url, headers=HEADERS)
        check("GET /mcp -> 405", r.status_code == 405, f"got {r.status_code}")
        check("405 carries Allow: POST", r.headers.get("allow") == "POST", r.headers.get("allow", "-"))

        print("\n── Origin is validated ──")
        r = await c.post(url, json=INIT, headers={**HEADERS, "Origin": "https://evil.example"})
        check("foreign Origin -> 403", r.status_code == 403, f"got {r.status_code}")
        r = await c.post(url, json=INIT, headers={**HEADERS, "Origin": "http://127.0.0.1:18830"})
        check("our own Origin is allowed", r.status_code == 200, f"got {r.status_code}")
        r = await c.post(url, json=INIT, headers=HEADERS)
        check("absent Origin is allowed (every server-side client)", r.status_code == 200)

        print("\n── unsupported URL ──")
        r = await c.post(url, json=rpc("tools/call", {
            "name": "scrape_listings", "arguments": {"url": "https://abc.xyz/investor/"},
        }), headers=HEADERS)
        body = r.json()
        text = json.dumps(body)
        check("scrape_listings on an unsupported URL is an error",
              body.get("result", {}).get("isError") is True or "error" in body)
        check("the error names the supported pattern", "businesses-for-sale" in text)
        check("the error points at archive_page for a single page", "archive_page" in text)
        print("    " + text[:300])

    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
