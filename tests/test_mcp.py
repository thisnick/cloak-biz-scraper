"""The MCP endpoint's transport contract.

These drive the real ASGI app through the real SDK — the JSON-RPC here is what a
client actually sends. That matters because both of the rules under test are
ones the SDK does *not* enforce for us, and both were found by probing it rather
than by reading it: its GET handler opens an SSE stream instead of refusing, and
its DNS-rebinding protection is off unless configured with an allowlist we have
no way to write.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "1"},
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", "test-secret-value-long-enough")
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    # follow_redirects=False on purpose. Mounting the endpoint (rather than
    # routing it) made a bare POST /mcp answer 307 to /mcp/, and every one of
    # these tests passed anyway because the client quietly followed it. A real
    # client is entitled not to. The endpoint is /mcp, so /mcp must answer.
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def rpc(client, method: str, params: dict | None = None, *, headers: dict | None = None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={**HEADERS, **(headers or {})},
    )


class TestStateless:
    def test_initialize_works_and_mints_no_session(self, client):
        """The whole point of stateless: nothing to pin a conversation to a process.

        A session id would make a second tool call depend on reaching the same
        container — which Railway is free to stop between two calls.
        """
        r = client.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in r.headers}
        assert r.json()["result"]["serverInfo"]["name"] == "cloak-biz-scraper"

    def test_tools_list_without_a_handshake(self, client):
        """A stateless server answers the first message it is given, whatever it is."""
        r = rpc(client, "tools/list")
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {
            "scrape_listings",
            "get_scrape_listing_results",
            "archive_page",
            "create_instance",
            "close_instance",
            "list_instances",
            "get_instance",
        }

    def test_the_async_pair_is_described_as_a_pair(self, client):
        """A model that does not know to call back reports zero listings for a
        sweep that is running perfectly well."""
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        assert "get_scrape_listing_results" in tools["scrape_listings"]["description"]
        assert "job_id" in tools["scrape_listings"]["description"]

    def test_money_is_advertised_as_a_string_not_a_number(self, client):
        """The contract an agent reads. Money is quoted, never interpreted."""
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        schema = tools["scrape_listings"]["outputSchema"]
        listing = schema["$defs"]["Listing"]["properties"]
        for field in ("asking_price", "revenue", "cashflow", "ebitda"):
            assert listing[field]["type"] == "string", field


class TestTheEndpointIsExactlySlashMcp:
    def test_post_to_mcp_is_answered_not_redirected(self, client):
        """A redirect here is the bug that hides from its own tests.

        Mounting the endpoint made /mcp answer 307 -> /mcp/, which every
        redirect-following client (httpx, this test client by default) papers
        over. Clients POST to /mcp; that is the endpoint, so that is what must
        answer.
        """
        r = client.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200, f"expected a real answer, got {r.status_code}"

    def test_get_is_refused_at_mcp_not_redirected(self, client):
        assert client.get("/mcp", headers=HEADERS).status_code == 405


class TestHostIsNotAllowlisted:
    def test_a_deployed_host_is_served(self, client):
        """The regression that would only ever fail in production.

        FastMCP's `host` setting defaults to 127.0.0.1, and a loopback host makes
        its constructor silently enable DNS-rebinding protection with an
        allowlist of localhost names. Left alone, every request carrying a real
        Railway domain in Host gets 421 Misdirected Request — while every local
        test passes, because Host is then 127.0.0.1 and matches. If a future SDK
        bump reintroduces that default, this fails here instead of on someone's
        deployment.
        """
        r = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Host": "cloak-biz-scraper-production.up.railway.app"}
        )
        assert r.status_code == 200, "a deployed hostname must not be treated as misdirected"


class TestGetIsRefused:
    def test_get_returns_405(self, client):
        """The SDK would hold an SSE stream open here. We have nothing to send
        down one, so a client waiting on it would wait forever."""
        r = client.get("/mcp", headers=HEADERS)
        assert r.status_code == 405
        assert r.headers["allow"] == "POST"

    def test_405_says_what_to_do_instead(self, client):
        assert "POST" in client.get("/mcp", headers=HEADERS).json()["error"]


class TestOriginIsValidated:
    def test_no_origin_is_allowed(self, client):
        """Every server-side MCP client — ChatGPT, Claude — sends no Origin.
        Refusing that would refuse the entire audience."""
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 200

    def test_a_foreign_origin_is_refused(self, client):
        r = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Origin": "https://evil.example"}
        )
        assert r.status_code == 403
        assert "another site" in r.json()["error"]

    def test_our_own_origin_is_allowed(self, client):
        r = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Origin": "https://testserver"}
        )
        assert r.status_code == 200

    def test_origin_is_checked_on_get_too(self, client):
        """Order matters: a cross-origin GET must not be told about the endpoint's
        shape before being refused."""
        r = client.get("/mcp", headers={**HEADERS, "Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_a_lookalike_origin_is_refused(self, client):
        """testserver.evil.example ends with nothing we accept — a prefix or
        suffix match here would be the bug."""
        r = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Origin": "https://testserver.evil.example"}
        )
        assert r.status_code == 403
