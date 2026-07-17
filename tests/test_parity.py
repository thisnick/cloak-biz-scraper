"""MCP and REST return the SAME bytes for the same job.

Decision #13: both façades are thin skins over one service layer, so an agent
polling `/mcp` and a dashboard polling `/api/*` must never see different answers.
Step 3 proved this byte-identical — but only ever by a reviewer running it by
hand; nothing in the suite pinned it, so it held on trust. It holds today because
both façades return `ScrapeResult.of(job)` from the one constructor. A field added
at a call site instead of in `of()` would diverge silently, and `evidence_dir`
was just such a field.

This drives the two real serialisation paths against each other — FastAPI's
`response_model` and FastMCP's structured output — not `of()` compared to itself,
which would prove only that equality is reflexive.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Listing, SyncResult

from conftest import mint_access

HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream",
           # A real MCP client sends an Origin, and /mcp validates it. Sending our
           # own keeps this call representative of the real one and immune to the
           # rebinding guard rejecting an absent Origin for the wrong-reason later.
           "Origin": "https://testserver"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", "test-secret-value-long-enough")
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        c.headers["Authorization"] = f"Bearer {mint_access(app)}"
        yield c


def _rich_job():
    """Every field populated, so a divergence anywhere is caught — not just the
    scalars a sparse job would exercise."""
    return app.state.jobs.create(
        url="https://www.bizbuysell.com/california/businesses-for-sale/",
        source="bizbuysell_serp", status="completed", max_pages=3, sync=True,
        db_id="db-123", summary="Found 2 listings across 3 pages.", pages_crawled=3,
        error=None,
        synced=SyncResult(new=2, existing=1, db_id="db-123", skipped=["EBITDA"]),
        listings=[
            Listing(listing_id="2453593", url="https://x/1", normalized_url="x/1",
                    title="Remodeling Contractor", location="San Francisco, CA",
                    asking_price="$965,000", revenue="", cashflow="$210,000",
                    ebitda="", excerpt="20+ years.", source="bizbuysell_serp"),
            Listing(listing_id="2461001", url="https://x/2", normalized_url="x/2",
                    title="Coffee Roaster", location="Oakland, CA",
                    asking_price="Not Disclosed", revenue="$1,200,000", cashflow="",
                    ebitda="$300,000", excerpt="Wholesale accounts.", source="bizbuysell_serp"),
        ],
    )


def _rest_payload(client, job_id: str) -> dict:
    r = client.get(f"/api/scrape/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _mcp_payload(client, job_id: str) -> dict:
    r = client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_scrape_listing_results", "arguments": {"job_id": job_id}},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]


def _sha(payload: dict) -> str:
    # Canonicalise before hashing: the two frameworks are free to choose key order
    # or whitespace, and neither is the thing under test — the values are.
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class TestScrapeResultParity:
    def test_the_two_facades_return_the_same_payload(self, client):
        job = _rich_job()
        rest = _rest_payload(client, job.id)
        mcp = _mcp_payload(client, job.id)
        # Control first: prove the comparison is running on the real job, not on
        # two empty or error payloads that would be trivially equal. A pass below
        # means nothing unless this holds.
        assert rest["job_id"] == job.id and rest["pages_crawled"] == 3, rest
        assert rest == mcp, (
            "MCP and REST disagree about a completed sweep. If a field was added to "
            "ScrapeResult at a call site instead of in ScrapeResult.of(), this is how "
            "it shows up.\n"
            f"  only in REST: {set(rest) - set(mcp)}\n"
            f"  only in MCP : {set(mcp) - set(rest)}\n"
            f"  differing   : {[k for k in rest if k in mcp and rest[k] != mcp[k]]}"
        )
        assert _sha(rest) == _sha(mcp)

    def test_evidence_dir_crosses_both_facades(self, client):
        """The field whose addition motivated this test. Present, equal, non-empty
        in both — the specific regression #13 is guarding against."""
        job = _rich_job()
        rest = _rest_payload(client, job.id)
        mcp = _mcp_payload(client, job.id)
        assert rest["evidence_dir"] == mcp["evidence_dir"]
        assert rest["evidence_dir"].endswith(job.id)

    def test_parity_holds_for_a_bare_working_job(self, client):
        """The other end of the range: a just-started sweep, most fields empty.
        Empty and null serialise differently if the two paths ever diverge on
        defaults, so pin that too."""
        job = app.state.jobs.create(url="https://www.bizbuysell.com/x",
                                    source="bizbuysell_serp")
        assert _rest_payload(client, job.id) == _mcp_payload(client, job.id)
