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
                    ebitda="", excerpt="20+ years.", source="bizbuysell_serp",
                    synced_row_id="notion-page-2453593"),
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


def _rest_info(client) -> dict:
    r = client.get("/api/server-info")
    assert r.status_code == 200, r.text
    return r.json()


def _mcp_info(client) -> dict:
    r = client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "server_info", "arguments": {}},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]


class TestServerInfoParity:
    """Same pin for server_info: it holds today because both façades call the one
    `views.server_info`, but nothing caught the drift if a field were added at one
    façade. This drives the two real serialisation paths against each other."""

    def test_the_two_facades_return_the_same_snapshot(self, client):
        rest = _rest_info(client)
        mcp = _mcp_info(client)
        # Control: a real snapshot with the four sections, not two empty/error bodies.
        assert set(rest) == {"proxy", "browser", "pool", "notion"} and rest["pool"]["max"] >= 1, rest
        assert rest == mcp, (
            "MCP and REST disagree about server_info. A field added at one façade "
            "instead of in views.server_info is how it shows up.\n"
            f"  only in REST: {set(rest) - set(mcp)}\n"
            f"  only in MCP : {set(mcp) - set(rest)}"
        )
        assert _sha(rest) == _sha(mcp)


class TestAnArchiveIdIsNotASweep:
    """The mistake one id space makes reachable, and how both façades answer it.

    `archive_page` now records a task, and its id looks exactly like a sweep's —
    so an agent will eventually poll one with `get_scrape_listing_results`. The
    answer must be a sentence saying it is the wrong kind of id, in both façades:
    a ScrapeResult with an empty `listings` would read as a sweep that found
    nothing, which is a wrong answer rather than a refused question.
    """

    def _archive_task(self):
        return app.state.jobs.create(
            kind="archive", url="https://www.bizbuysell.com/Business-Opportunity/x/1/",
            notion_page_id="page-1", status="completed", title="A Laundromat",
            blocks_appended=12, summary="Archived 'A Laundromat' into Notion (12 blocks).",
        )

    def test_rest_refuses_it_as_a_conflict_not_a_missing_run(self, client):
        task = self._archive_task()
        r = client.get(f"/api/scrape/{task.id}")
        # 409, not 404: the record exists, it just cannot answer this question.
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "archive task" in detail and "scrape_listings" in detail

    def test_mcp_refuses_it_with_the_same_sentence(self, client):
        task = self._archive_task()
        r = client.post("/mcp", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_scrape_listing_results", "arguments": {"job_id": task.id}},
        })
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert result["isError"] is True, result
        text = " ".join(c.get("text", "") for c in result["content"])
        assert "archive task" in text and "scrape_listings" in text


# ── create_upload_url: one ticket, two doors ─────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 60
NOT_AN_IMAGE = b"NOTION_API_TOKEN=secret_abcdef\n"


def _rest_ticket(client) -> dict:
    r = client.post("/api/uploads")
    assert r.status_code == 200, r.text
    return r.json()


def _mcp_ticket(client) -> dict:
    r = client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "create_upload_url", "arguments": {}},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["isError"] is False, body
    return body["result"]["structuredContent"]


@pytest.fixture
def uploads(tmp_path):
    """This test's own staging root, so a mint never writes to a shared volume."""
    from app.services.uploads import UploadService

    app.state.uploads = UploadService(tmp_path / "uploads")
    return app.state.uploads


class TestUploadTicketParity:
    """`create_upload_url` mints fresh randomness on every call, so a naive
    "call both, compare" would only ever prove that two random tickets differ.

    The service call is therefore frozen to ONE ticket and both façades are asked
    to describe it. What is left varying is exactly what parity is about — the
    view builder and the two serialisation paths, FastAPI's `response_model`
    against FastMCP's structured output. The live, unfrozen call is pinned
    separately below on the fields that are not random.
    """

    @pytest.fixture
    def frozen(self, uploads, monkeypatch):
        from app.services.uploads import Ticket

        ticket = Ticket(handle="upl_00112233445566aa", token="payload.signature",
                        expires_at=1_800_000_000.0)

        async def one_ticket(*, subject, secret, now=None):
            return ticket

        monkeypatch.setattr(app.state.uploads, "mint", one_ticket)
        return ticket

    def test_the_two_facades_describe_one_ticket_identically(self, client, frozen):
        rest = _rest_ticket(client)
        mcp = _mcp_ticket(client)
        # Control first: prove this ran on the real ticket, not on two error
        # bodies that would be trivially equal.
        assert rest["handle"] == frozen.handle and rest["token"] == frozen.token, rest
        assert rest == mcp, (
            "MCP and REST disagree about an upload ticket. A field built at a call "
            "site instead of in views.upload_ticket is how it shows up.\n"
            f"  only in REST: {set(rest) - set(mcp)}\n"
            f"  only in MCP : {set(mcp) - set(rest)}\n"
            f"  differing   : {[k for k in rest if k in mcp and rest[k] != mcp[k]]}"
        )
        assert _sha(rest) == _sha(mcp)

    def test_both_doors_hand_out_the_same_absolute_upload_url(self, client, frozen):
        """The URL is the whole point of the ticket, and it is derived from the
        request's own origin — the one field most likely to differ between an
        ASGI tool call and a FastAPI route."""
        rest, mcp = _rest_ticket(client), _mcp_ticket(client)
        assert rest["upload_url"] == f"https://testserver/uploads/{frozen.handle}"
        assert mcp["upload_url"] == rest["upload_url"]

    def test_a_live_mint_agrees_on_everything_that_is_not_random(self, client, uploads):
        """The unfrozen path, so the parity above cannot pass on a stub alone."""
        rest, mcp = _rest_ticket(client), _mcp_ticket(client)
        assert rest["handle"] != mcp["handle"], "each call really did mint its own"
        stable = ("expires_in", "max_files", "max_bytes_per_file", "accepts")
        assert {k: rest[k] for k in stable} == {k: mcp[k] for k in stable}
        for payload in (rest, mcp):
            assert payload["upload_url"].startswith("https://testserver/uploads/upl_")
            assert payload["curl"].endswith(payload["upload_url"])
            assert payload["token"] in payload["curl"]


class TestThePublishedUploadSurface:
    """What a model is TOLD must be what the server actually does. A tool
    description that overstates the caps is a bug with a very long feedback
    loop: the model believes it for the rest of the session and keeps posting
    files that keep being refused."""

    def test_exactly_one_new_tool_appears(self, client):
        r = client.post("/mcp", headers=HEADERS,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "create_upload_url" in names
        assert len(names) == 15, sorted(names)
        assert "stage_from_url" not in names and "release_upload" not in names

    def test_it_takes_no_arguments(self, client):
        """No arguments is the design: the caps are the server's, so there is
        nothing for a caller to size in advance."""
        r = client.post("/mcp", headers=HEADERS,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(t for t in r.json()["result"]["tools"] if t["name"] == "create_upload_url")
        assert tool["inputSchema"].get("properties", {}) == {}
        assert not tool["inputSchema"].get("required")

    def test_the_description_teaches_the_three_step_flow(self, client):
        r = client.post("/mcp", headers=HEADERS,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        doc = next(t for t in r.json()["result"]["tools"]
                   if t["name"] == "create_upload_url")["description"]
        assert "create_upload_url()" in doc and "agent_browser" in doc
        # The two sentences that stop a no-egress client burning turns, and stop
        # a model trying to paste a photo as ~a million tokens of base64.
        assert "say so and stop" in doc
        assert "pasting the file as text will not work" in doc
        assert "checked by content rather than by filename" in doc

    def test_the_caps_it_reports_are_the_caps_that_are_enforced(self, client, uploads,
                                                                monkeypatch):
        """Not "the number equals another literal" — the number is lowered, the
        ticket is asked what it allows, and the endpoint is made to prove it."""
        from app.services import uploads as store

        monkeypatch.setattr(store, "MAX_BYTES_PER_FILE", 200)
        ticket = _rest_ticket(client)
        assert ticket["max_bytes_per_file"] == 200

        auth = {"Authorization": f"Bearer {ticket['token']}"}
        ok = client.post(ticket["upload_url"], headers=auth,
                         files={"file": ("small.jpg", JPEG, "image/jpeg")})
        too_big = client.post(ticket["upload_url"], headers=auth,
                              files={"file": ("big.jpg", JPEG + b"\x00" * 5000,
                                              "image/jpeg")})
        assert ok.status_code == 200, ok.text
        assert too_big.status_code == 413, too_big.text

    def test_the_file_count_it_reports_is_the_count_that_is_enforced(self, client, uploads,
                                                                    monkeypatch):
        from app.services import uploads as store

        monkeypatch.setattr(store, "MAX_FILES_PER_TICKET", 2)
        ticket = _rest_ticket(client)
        assert ticket["max_files"] == 2

        r = client.post(ticket["upload_url"],
                        headers={"Authorization": f"Bearer {ticket['token']}"},
                        files=[("file", (f"p{n}.jpg", JPEG + bytes([n]), "image/jpeg"))
                               for n in range(3)])
        assert r.status_code == 409, r.text

    def test_what_it_says_it_accepts_is_what_it_accepts(self, client, uploads):
        ticket = _rest_ticket(client)
        assert ticket["accepts"] == ["image/jpeg", "image/png", "image/webp",
                                     "image/gif", "application/pdf"]

        auth = {"Authorization": f"Bearer {ticket['token']}"}
        refused = client.post(ticket["upload_url"], headers=auth,
                              files={"file": ("photo.jpg", NOT_AN_IMAGE, "image/jpeg")})
        assert refused.status_code == 415, refused.text
        for name, data, declared in (("a.jpg", JPEG, "image/jpeg"),
                                     ("b.png", PNG, "image/png")):
            r = client.post(ticket["upload_url"], headers=auth,
                            files={"file": (name, data, declared)})
            assert r.status_code == 200, r.text
            assert r.json()["content_type"] in ticket["accepts"]

    def test_the_expiry_it_reports_is_the_expiry_it_was_minted_with(self, client, uploads):
        import time

        from app.services import uploads as store

        ticket = _rest_ticket(client)
        assert ticket["expires_in"] == store.TTL_SEC
        assert abs(ticket["expires_at"] - (time.time() + ticket["expires_in"])) < 5

    def test_the_pre_baked_curl_is_a_command_that_actually_works(self, client, uploads):
        """Models follow a whole command far more reliably than they assemble one
        from parts, so the whole command has to be right — including the part
        that matters, the token in a HEADER rather than in the URL."""
        import shlex

        ticket = _rest_ticket(client)
        argv = shlex.split(ticket["curl"])
        assert argv[0] == "curl"
        name, _, value = argv[argv.index("-H") + 1].partition(": ")
        url = argv[-1]
        assert name == "Authorization" and value.startswith("Bearer ")
        assert url == ticket["upload_url"]
        assert ticket["token"] not in url, "the token must not ride in the URL"
        assert "-F" in argv and argv[argv.index("-F") + 1].startswith("file=@")

        r = client.post(url, headers={name: value},
                        files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 200, r.text
        assert r.json()["path"].endswith(f"/{ticket['handle']}/photo.jpg")

    def test_the_path_it_hands_back_is_one_agent_browser_will_accept(self, client, uploads):
        """The end of the chain the description promises: step 2's path is a path
        step 3 resolves. If these two ever disagreed, the tool would be telling a
        model to do something that cannot work."""
        import pathlib

        ticket = _rest_ticket(client)
        r = client.post(ticket["upload_url"],
                        headers={"Authorization": f"Bearer {ticket['token']}"},
                        files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        staged = r.json()["path"]

        assert app.state.uploads.resolve_for("owner", [staged]) == [
            pathlib.Path(staged).resolve()
        ]

    def test_minting_sweeps_expired_tickets_first(self, client, uploads):
        """The mint is the only thing that frees this space, so the sweep has to
        happen on the way through rather than on a timer that would keep a
        sleeping container awake."""
        import json
        import time

        dead = _rest_ticket(client)["handle"]
        manifest = uploads.root / dead / ".ticket.json"
        record = json.loads(manifest.read_text())
        record["expires"] = time.time() - 1
        manifest.write_text(json.dumps(record))

        _rest_ticket(client)

        assert not (uploads.root / dead).exists(), "the expired ticket survived a mint"


class TestTheTicketStaysOutOfTheLogs:
    """The token now travels in a tool RESULT, which is a place transcripts get
    kept. services/log_safety.py redacts query strings and userinfo — a bearer
    in a header is outside what it can see, so the only defence is that nothing
    logs it. This is what checks that."""

    def test_no_log_line_anywhere_carries_the_token(self, client, uploads, caplog):
        import logging

        with caplog.at_level(logging.DEBUG):
            ticket = _rest_ticket(client)
            r = client.post(ticket["upload_url"],
                            headers={"Authorization": f"Bearer {ticket['token']}"},
                            files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 200, r.text

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert ticket["token"] not in logged
        assert ticket["curl"] not in logged
        # The handle is deliberately NOT secret — it is in the URL path, it is
        # useless without the token, and a log that cannot name the ticket
        # cannot explain anything.
        assert ticket["handle"] in logged, "nothing was logged at all; the test proves nothing"
