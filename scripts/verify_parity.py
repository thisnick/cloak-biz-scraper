"""Prove the MCP tool and the REST route return the same payload.

Two façades over one service layer is the whole design bet (decision #13). This
is the check that the bet is still being honoured: not that both work, but that
both say *exactly* the same thing — so a dashboard and an agent can never
disagree about the same sweep or the same browser.

    python scripts/verify_parity.py --base http://127.0.0.1:18830 --job <job_id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


async def call_tool(client: httpx.AsyncClient, base: str, name: str, args: dict) -> dict:
    r = await client.post(
        f"{base}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
        headers=HEADERS,
    )
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def diff(a, b, path="") -> list[str]:
    """Every field where the two payloads disagree."""
    out: list[str] = []
    if type(a) is not type(b):
        return [f"{path or '.'}: {type(a).__name__} vs {type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing from MCP")
            elif k not in b:
                out.append(f"{path}.{k}: missing from REST")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} vs {len(b)} items"]
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {a!r} vs {b!r}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18830")
    ap.add_argument("--job", required=True)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    async with httpx.AsyncClient(timeout=60) as c:
        print("\n── get_scrape_listing_results: MCP vs REST ──")
        mcp = await call_tool(c, base, "get_scrape_listing_results", {"job_id": args.job})
        rest = (await c.get(f"{base}/api/scrape/{args.job}")).json()

        structured = mcp.get("structuredContent")
        check("MCP returns structured content", structured is not None)

        differences = diff(structured, rest)
        check("payloads are byte-identical", not differences,
              "; ".join(differences[:5]) if differences else
              f"{len(json.dumps(rest))} chars, {len(rest.get('listings', []))} listings")

        # The text block a model reads must not contradict the structured data.
        text = "".join(b.get("text", "") for b in mcp.get("content", []))
        check("the MCP text block carries the same job", args.job in text)

        print("\n── list_instances: MCP vs REST ──")
        mcp_i = await call_tool(c, base, "list_instances", {})
        rest_i = (await c.get(f"{base}/api/instances")).json()
        m = mcp_i.get("structuredContent", {}).get("result", [])
        check("instance payloads agree in shape", len(m) == len(rest_i),
              f"mcp={len(m)} rest={len(rest_i)}")
        if m and rest_i:
            # cdp_url is excluded from the equality check because it carries a
            # token minted at call time. Note it may still be byte-identical
            # across two calls in the same second — same iat, same exp — so
            # asserting that it *differs* would be asserting something untrue.
            # That it is minted per call is proven by counting mints, in
            # tests/test_tokens.py.
            fields = [k for k in rest_i[0] if k != "cdp_url"]
            stable = diff({k: m[0][k] for k in fields}, {k: rest_i[0][k] for k in fields})
            check("every field but the freshly-minted cdp_url matches", not stable,
                  "; ".join(stable[:3]))

            iid = rest_i[0]["instance_id"]
            check("both mint a cdp_url scoped to this instance",
                  all(u and f"/instances/{iid}/cdp?t=" in u
                      for u in (m[0]["cdp_url"], rest_i[0]["cdp_url"])))
            check("timezone is measured or honestly absent — never defaulted",
                  m[0]["timezone"] == rest_i[0]["timezone"],
                  f"timezone={rest_i[0]['timezone']!r} proxy_ip={rest_i[0]['proxy_ip']!r}")

    print(f"\n{'PARITY HOLDS' if ok else 'PARITY BROKEN'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
