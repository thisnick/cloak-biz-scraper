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

# Without an Origin — which is every server-side MCP client, and the only shape
# in which a forged Host reaches the tool at all: with one present, the Origin
# rule refuses the request first, because an Origin that disagrees with the Host
# is exactly what that rule is for.
NO_ORIGIN_HEADERS = {"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"}

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


class TestTheMcpDoorEndToEnd:
    """The claim the whole unit rests on, driven through the MCP door itself.

    Everything else here compares payloads. A payload comparison cannot see the
    two arguments the tool actually passes to the service — the subject it mints
    for and the secret it signs with — because neither appears in the answer.
    The frozen fixture this file used to rely on discarded both, and both could
    be replaced with a constant while the suite stayed green: one produced a
    ticket that accepted the file and then refused to resolve it, the other a
    ticket the endpoint rejected outright. Both are the promise step 3 of the
    tool's own description makes.

    So this test does the whole flow: mint over /mcp, POST real bytes to the URL
    it returned with the token it returned, and hand the path it answered with
    back to the thing that decides whether `agent_browser upload` will accept it.
    """

    def _mint_over_mcp(self, client) -> dict:
        return _mcp_ticket(client)

    def test_a_ticket_minted_over_mcp_stages_a_file_that_then_resolves(self, client,
                                                                       uploads):
        import pathlib as _pathlib

        from app.services.tokens import OWNER

        ticket = self._mint_over_mcp(client)

        posted = client.post(
            ticket["upload_url"],
            headers={"Authorization": f"Bearer {ticket['token']}"},
            files={"file": ("photo.jpg", JPEG, "image/jpeg")},
        )
        assert posted.status_code == 200, (
            f"the token this tool handed out did not open the URL it handed out: "
            f"{posted.status_code} {posted.text}"
        )

        staged = posted.json()["path"]
        assert app.state.uploads.resolve_for(OWNER, [staged]) == [
            _pathlib.Path(staged).resolve()
        ], "the file staged, and then the subject it was staged for could not use it"

    def test_the_same_flow_over_rest_agrees_step_for_step(self, client, uploads):
        """The control: the door that was already verified, driven identically,
        so a failure above is about the MCP door and not about the flow."""
        import pathlib as _pathlib

        from app.services.tokens import OWNER

        ticket = _rest_ticket(client)
        posted = client.post(ticket["upload_url"],
                             headers={"Authorization": f"Bearer {ticket['token']}"},
                             files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert posted.status_code == 200, posted.text
        staged = posted.json()["path"]
        assert app.state.uploads.resolve_for(OWNER, [staged]) == [
            _pathlib.Path(staged).resolve()
        ]

    def test_both_doors_mint_for_the_same_subject(self, client, uploads):
        """Directly, from the manifest rather than the payload: the subject a
        ticket was minted for is recorded on disk and appears in no response."""
        import json

        rest, mcp = _rest_ticket(client), _mcp_ticket(client)
        subjects = {
            json.loads((uploads.root / t["handle"] / ".ticket.json").read_text())["sub"]
            for t in (rest, mcp)
        }
        assert subjects == {"owner"}, subjects


class TestTheSubjectComesFromTheVerifiedToken:
    """`OWNER` is also what a hardcoded constant produces, so asserting
    `{"owner"}` cannot tell a façade that reads the token from one that ignores
    it — hardcoding `subject=OWNER` in either left the whole suite passing.

    Same defect as the resolved-path tests that normalised both sides: an
    expected value the bug itself can reach. These mint under a subject no
    constant would return.
    """

    @pytest.mark.parametrize("door", ["rest", "mcp"])
    def test_each_facade_mints_for_the_subject_in_the_token(self, client, uploads,
                                                            door):
        import json

        headers = {"Authorization": f"Bearer {mint_access(app, subject='somebody-else')}"}

        if door == "rest":
            r = client.post("/api/uploads", headers=headers)
            assert r.status_code == 200, r.text
            handle = r.json()["handle"]
        else:
            r = client.post("/mcp", headers={**HEADERS, **headers}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "create_upload_url", "arguments": {}}})
            body = r.json()["result"]
            assert body["isError"] is False, body
            handle = body["structuredContent"]["handle"]

        manifest = json.loads((uploads.root / handle / ".ticket.json").read_text())
        assert manifest["sub"] == "somebody-else", (
            "the ticket was minted for a subject the caller's token does not name"
        )


class TestBothDoorsUseTheOneViewBuilder:
    """"One implementation behind two doors" is the module docstring's claim, and
    comparing payloads does not check it — two implementations that happen to
    agree pass that test perfectly, and a REST route that rebuilt the ticket
    inline with every value correct went green. This watches the call happen."""

    def test_each_facade_actually_calls_views_upload_ticket(self, client, uploads,
                                                            monkeypatch):
        from app import mcp_server
        from app.routes import api
        from app.services import views

        calls = []
        real = views.upload_ticket

        def spy(ticket, *, base_url=""):
            calls.append(base_url)
            return real(ticket, base_url=base_url)

        # Both façades bind the name at import, so each binding is patched — and
        # to the SAME wrapper, which is what makes "both went through one
        # builder" the thing being observed rather than "each went through its
        # own".
        monkeypatch.setattr(mcp_server, "upload_ticket", spy)
        monkeypatch.setattr(api, "upload_ticket", spy)

        rest, mcp = _rest_ticket(client), _mcp_ticket(client)

        assert len(calls) == 2, (
            f"only {len(calls)} of the two doors went through views.upload_ticket"
        )
        assert calls == ["https://testserver", "https://testserver"]
        assert set(rest) == set(mcp)


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

    def test_the_accepted_list_is_exactly_what_the_sniffer_can_recognise(self, client,
                                                                         uploads):
        """Both directions, derived rather than listed.

        A hardcoded list can only re-confirm itself: a type added to the sniffer
        and forgotten in `ACCEPTS` would be silently accepted and never
        advertised, and one removed from the sniffer but left in `ACCEPTS` would
        be advertised and always refused. Neither is visible to a test that
        compares one literal against another — which is the same defect the
        agent_browser verb-block test was just fixed for.
        """
        from app.services import uploads as store

        recognisable = {content_type for _magic, content_type in store._MAGIC}
        recognisable.add(store.sniff(b"RIFF" + b"\x00" * 4 + b"WEBP"))
        assert set(_rest_ticket(client)["accepts"]) == recognisable

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


class TestMintingCanBeRefused:
    """Both refusals a mint can produce, over the door that has to turn them
    into a status code. Untested when Unit C shipped; the messages are the whole
    point of surfacing them, so they are worth a test rather than a reading."""

    def test_a_full_volume_is_a_507_that_names_where_to_free_space(
        self, client, uploads, monkeypatch
    ):
        from app.services import uploads as store

        monkeypatch.setattr(store, "UPLOADS_BUDGET_BYTES", 8192)
        first = _rest_ticket(client)
        (uploads.root / first["handle"] / "filler.bin").write_bytes(b"x" * 8192)

        r = client.post("/api/uploads")
        assert r.status_code == 507, r.text
        detail = r.json()["detail"]
        assert "Settings" in detail and "Disk space" in detail

    def test_the_tool_refuses_with_the_same_sentence(self, client, uploads, monkeypatch):
        from app.services import uploads as store

        monkeypatch.setattr(store, "UPLOADS_BUDGET_BYTES", 8192)
        first = _rest_ticket(client)
        (uploads.root / first["handle"] / "filler.bin").write_bytes(b"x" * 8192)

        r = client.post("/mcp", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_upload_url", "arguments": {}},
        })
        result = r.json()["result"]
        assert result["isError"] is True, result
        text = " ".join(c.get("text", "") for c in result["content"])
        assert "Settings" in text and "Disk space" in text

    def test_a_store_that_cannot_sign_is_a_503_rather_than_a_traceback(
        self, client, uploads, monkeypatch
    ):
        """The other refusal `mint` can raise, mapped rather than escaping as a 500.

        Its real cause — no APP_SECRET — cannot be reached through this door:
        the OAuth guard verifies the caller's token against the same secret, so
        a server without one answers 401 before the route runs (checked: it
        does). What is untested and testable is the MAPPING, so that is what
        this drives, with the service's own refusal.
        """
        from app.services.uploads import UploadsError

        async def cannot_sign(*, subject, secret, now=None):
            raise UploadsError(
                "this server has no APP_SECRET set, so it cannot mint an upload URL"
            )

        monkeypatch.setattr(app.state.uploads, "mint", cannot_sign)
        r = client.post("/api/uploads")
        assert r.status_code == 503, r.text
        assert "APP_SECRET" in r.json()["detail"]

    def test_the_service_really_does_refuse_without_a_secret(self, uploads):
        """The precondition the mapping above stands in for."""
        import asyncio

        from app.services.uploads import UploadsError

        with pytest.raises(UploadsError, match="APP_SECRET"):
            asyncio.run(uploads.mint(subject="owner", secret=None))


class TestTheAddressComesFromAHeader:
    """`upload_url` is built from the `Host` header, this deployment runs behind
    no TrustedHostMiddleware, and the tool's description tells a model to run the
    `curl` string AS-IS. That makes a hostile Host injection into an instruction
    we asked something to execute verbatim, so it is refused rather than escaped
    and shipped — and escaped as well, because one being right is not a reason
    for the other to be missing."""

    @pytest.mark.parametrize("host", [
        "",                       # no Host at all
        "evil.example$(id)",      # command substitution, survives shlex as one word
        "evil.example`id`",       # the older spelling of the same thing
        "evil.example;id",
        "has space.example",
        "o'quote.example",
        "evil.example\nX-Injected: 1",
    ])
    def test_a_host_that_is_not_a_hostname_is_refused(self, client, uploads, host):
        r = client.post("/api/uploads", headers={"Host": host})

        assert r.status_code != 200, (
            f"minted a ticket for Host {host!r}: {r.text}"
        )
        assert r.status_code == 400, r.text
        assert "could not work out its own address" in r.json()["detail"]

    @pytest.mark.parametrize("door", ["rest", "mcp"])
    def test_a_refused_mint_leaves_nothing_behind(self, client, uploads, door):
        """A refusal must cost nothing.

        Before the address was validated ahead of the mint, each refused call
        left a directory, a manifest and a reservation with nothing to clean
        them up until the TTL — and every later mint re-walked the pile, taking
        the median mint from 0.7 ms to 84.6 ms after 2,000 refused calls.
        """
        from app.services import reclaim

        before = sorted(p.name for p in reclaim.children(uploads.root))

        if door == "rest":
            r = client.post("/api/uploads", headers={"Host": "evil.example$(id)"})
            assert r.status_code == 400, r.text
        else:
            r = client.post("/mcp",
                            headers={**NO_ORIGIN_HEADERS, "Host": "evil.example$(id)"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "create_upload_url",
                                             "arguments": {}}})
            assert r.json()["result"]["isError"] is True, r.text

        assert sorted(p.name for p in reclaim.children(uploads.root)) == before
        assert uploads._reserved == {}

    def test_the_anchor_is_end_of_string_not_end_of_line(self):
        """`$` also matches before a trailing newline, so `evil.example\n` would
        be admitted by it and is not by `\\Z`.

        Tested by calling the validator, not over HTTP, and that is the honest
        framing: `urls.base_from` strips the header, so a trailing newline never
        reaches this rule and the HTTP case is a 200 for a *stripped* host. The
        anchor still has to mean what it says — the next caller of this function
        may not be an HTTP header.
        """
        from app.services.uploads import NoPublicUrl
        from app.services.views import require_usable_base_url

        require_usable_base_url("https://evil.example")          # the control
        with pytest.raises(NoPublicUrl):
            require_usable_base_url("https://evil.example\n")

    def test_the_view_builder_refuses_on_its_own(self):
        """Both façades check the address before minting, so the copy inside
        `upload_ticket` is no longer reachable through either door — which is
        exactly why it needs its own test rather than none.

        It is what makes the rule true of EVERY caller, including the deferred
        server-side fetch that would be a second writer. Called directly here,
        because that is the only way left to reach it.
        """
        from app.services.uploads import NoPublicUrl, Ticket
        from app.services.views import upload_ticket

        ticket = Ticket(handle="upl_00112233445566aa", token="tok.sig",
                        expires_at=1.0)
        assert upload_ticket(ticket, base_url="https://ok.example").upload_url
        with pytest.raises(NoPublicUrl):
            upload_ticket(ticket, base_url="https://evil.example$(id)")

    def test_an_ordinary_host_still_mints(self, client, uploads):
        """The control: the refusal above is not simply refusing everything."""
        for host in ("testserver", "testserver:8000", "a-b.example.com"):
            r = client.post("/api/uploads", headers={"Host": host})
            assert r.status_code == 200, (host, r.text)
            assert r.json()["upload_url"] == f"https://{host}/uploads/{r.json()['handle']}"

    def test_the_curl_is_a_single_safe_command(self, client, uploads):
        """shlex round-trips the command back to the exact URL and header, so
        nothing in it is a second word or an operator."""
        import shlex

        ticket = _rest_ticket(client)
        argv = shlex.split(ticket["curl"])

        assert argv[-1] == ticket["upload_url"]
        assert argv[argv.index("-H") + 1] == f"Authorization: Bearer {ticket['token']}"
        assert len(argv) == 6, argv  # curl -H <hdr> -F <file> <url>

    @pytest.mark.parametrize("host", ["evil.example$(id)", "evil.example;id",
                                      "o'quote.example", "has space.example"])
    def test_the_escaping_holds_on_its_own_with_the_host_check_disabled(self, host,
                                                                        monkeypatch):
        """The inner layer, tested by removing the outer one.

        With the host check in place nothing hostile can reach the string, which
        makes `shlex.quote` unobservable through the API — provably a no-op, and
        therefore untestable from outside. That is not a reason to leave it
        unchecked: it is the layer that would matter if the host rule were ever
        relaxed, so the rule is switched off here and the escaping is made to
        stand by itself.
        """
        import shlex

        from app.services import views
        from app.services.uploads import Ticket

        monkeypatch.setattr(views, "require_usable_base_url", lambda base_url: None)
        ticket = views.upload_ticket(
            Ticket(handle="upl_00112233445566aa", token="tok.sig", expires_at=1.0),
            base_url=f"https://{host}",
        )

        argv = shlex.split(ticket.curl)
        assert len(argv) == 6, argv
        assert argv[-1] == ticket.upload_url, (
            "the hostile host broke out of the URL argument"
        )
        assert host in argv[-1], "the host was mangled rather than quoted"

    def test_an_origin_that_disagrees_with_the_host_never_reaches_the_tool(
        self, client, uploads
    ):
        """The layer above, and ONLY for a mismatch.

        I described this once as "the Origin rule refuses a forged Host first",
        which overstates which guard does the work: it compares Origin against
        Host, so a caller who forges both passes it cleanly. See the next test —
        the host rule is what actually holds.
        """
        r = client.post("/mcp", headers={**HEADERS, "Host": "evil.example$(id)"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "create_upload_url", "arguments": {}}})
        assert r.status_code == 403, r.text

    def test_a_forged_host_with_a_matching_origin_is_stopped_by_the_host_rule(
        self, client, uploads
    ):
        """The case that shows which guard is load-bearing. Nothing is exposed —
        the request is still refused — but it is refused by the host rule, not
        by the Origin rule, which passes it straight through."""
        hostile = "evil.example$(id)"
        r = client.post("/mcp",
                        headers={**HEADERS, "Host": hostile,
                                 "Origin": f"https://{hostile}"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "create_upload_url", "arguments": {}}})

        assert r.status_code == 200, r.text  # the Origin rule let it through
        result = r.json()["result"]
        assert result["isError"] is True, result
        text = " ".join(c.get("text", "") for c in result["content"])
        assert "could not work out its own address" in text

    def test_the_mcp_door_refuses_the_same_host_with_the_same_sentence(self, client,
                                                                       uploads):
        """The MCP door collapses every refusal into one ValueError, so the
        message is the only thing distinguishing this from a full volume."""
        r = client.post("/mcp",
                        headers={**NO_ORIGIN_HEADERS, "Host": "evil.example$(id)"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "create_upload_url", "arguments": {}}})
        result = r.json()["result"]
        assert result["isError"] is True, result
        text = " ".join(c.get("text", "") for c in result["content"])
        assert "could not work out its own address" in text


class TestTheTwoRefusalsStayDistinguishable:
    """One door gets a status code per failure; the other gets one ValueError for
    all of them. So over MCP the message is the ONLY thing telling "the volume is
    full" from "this server cannot address itself", and the two must not
    converge — a model reading them does different things."""

    def test_a_full_volume_and_a_bad_host_read_differently_over_mcp(self, client,
                                                                    uploads,
                                                                    monkeypatch):
        from app.services import uploads as store

        def _call(headers):
            r = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "create_upload_url", "arguments": {}}})
            result = r.json()["result"]
            assert result["isError"] is True, result
            return " ".join(c.get("text", "") for c in result["content"])

        bad_host = _call({**NO_ORIGIN_HEADERS, "Host": "evil.example$(id)"})

        monkeypatch.setattr(store, "UPLOADS_BUDGET_BYTES", 8192)
        first = _rest_ticket(client)
        (uploads.root / first["handle"] / "filler.bin").write_bytes(b"x" * 8192)
        full = _call(HEADERS)

        assert bad_host != full
        assert "Disk space" in full and "Disk space" not in bad_host
        assert "address" in bad_host and "address" not in full

    def test_the_rest_door_gives_each_its_own_status(self, client, uploads,
                                                     monkeypatch):
        from app.services import uploads as store

        bad_host = client.post("/api/uploads", headers={"Host": "evil.example$(id)"})
        monkeypatch.setattr(store, "UPLOADS_BUDGET_BYTES", 8192)
        first = _rest_ticket(client)
        (uploads.root / first["handle"] / "filler.bin").write_bytes(b"x" * 8192)
        full = client.post("/api/uploads")

        assert (bad_host.status_code, full.status_code) == (400, 507)


class TestTheDescriptionDoesNotUnderstateTheEndpoint:
    """The sentence added beyond the plan was unpinned: replacing it with the
    outright lie "You may post exactly one file per command" left the suite
    green. Claim and behaviour are checked together here, so neither can move
    without the other."""

    def test_several_files_in_one_command_really_work_and_are_described(self, client,
                                                                        uploads):
        import re

        ticket = _rest_ticket(client)
        posted = client.post(
            ticket["upload_url"],
            headers={"Authorization": f"Bearer {ticket['token']}"},
            files=[("file", ("a.jpg", JPEG, "image/jpeg")),
                   ("file", ("b.png", PNG, "image/png"))],
        )
        assert posted.status_code == 200, posted.text
        assert len(posted.json()["files"]) == 2, "the endpoint takes several parts"

        r = client.post("/mcp", headers=HEADERS,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        doc = next(t for t in r.json()["result"]["tools"]
                   if t["name"] == "create_upload_url")["description"]

        assert not re.search(r"(exactly|only) one file per (command|request|call)",
                             doc, re.I), (
            "the description tells a model it may send one file per command, and "
            "the endpoint above just took two"
        )
        assert re.search(r"several files in one|more than one file|repeating `-F",
                         doc, re.I), (
            "the endpoint takes several files per command and the description "
            "never says so"
        )

    def test_the_expiry_clock_is_described_from_the_right_moment(self, client):
        """The clock runs from the mint, not from the upload — a file posted an
        hour into a ticket's life has one hour left, not two. The description
        said otherwise."""
        import re

        r = client.post("/mcp", headers=HEADERS,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        doc = next(t for t in r.json()["result"]["tools"]
                   if t["name"] == "create_upload_url")["description"]

        assert not re.search(r"hours after upload", doc, re.I)
        assert "the clock starts when you call this" in doc


class TestTheWireModelCannotSilentlyDropAField:
    """`StagedUpload` inherits pydantic's `extra='ignore'`, so a field added to
    the service record and passed through would vanish on the way out with no
    error anywhere. The two field sets are pinned against each other."""

    def test_the_wire_record_carries_every_field_the_store_records(self):
        import dataclasses

        from app.models import StagedFile as Wire
        from app.services.uploads import StagedFile as Record

        stored = {f.name for f in dataclasses.fields(Record)}
        published = set(Wire.model_fields)
        assert stored == published, (
            "the staging record and the model it is published as have drifted.\n"
            f"  recorded but never published: {sorted(stored - published)}\n"
            f"  published but never recorded: {sorted(published - stored)}"
        )

    def test_the_upload_response_is_that_record_plus_the_list(self):
        from app.models import StagedFile as Wire
        from app.models import StagedUpload

        assert set(StagedUpload.model_fields) == set(Wire.model_fields) | {"files"}


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

        # OUR records only. The first version of this scoped nothing, and its
        # control passed on httpx's own client-side line — `HTTP Request: POST
        # https://testserver/uploads/upl_…` — which carries the handle because
        # the handle is in the URL. With every cloakbiz logger silenced the whole
        # test still went green: a control written precisely so it could not pass
        # by capturing nothing, passing by capturing something irrelevant.
        ours = [r for r in caplog.records if r.name.startswith("cloakbiz")]
        logged = "\n".join(record.getMessage() for record in ours)
        assert ticket["token"] not in logged
        assert ticket["curl"] not in logged
        # The handle is deliberately NOT secret — it is in the URL path, it is
        # useless without the token, and a log that cannot name the ticket
        # cannot explain anything.
        # Per-record, not per-corpus, and this is the THIRD version of this
        # control. The first passed on httpx's own client-side line, because the
        # handle is in the URL. The second passed on any cloakbiz record that
        # happened to name the handle, so silencing the mint left the `staged …`
        # line standing in for it. A control satisfied by a line other than the
        # one it is about is not checking that line.
        minted = [r for r in ours
                  if r.name == "cloakbiz.uploads"
                  and r.getMessage().startswith("minted upload ticket")]
        assert len(minted) == 1, f"expected one mint record, got {len(minted)}"
        assert ticket["handle"] in minted[0].getMessage()
