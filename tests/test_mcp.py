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

from conftest import mint_access

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
    # follow_redirects=False on purpose. Mounting the endpoint (rather than
    # routing it) made a bare POST /mcp answer 307 to /mcp/, and every one of
    # these tests passed anyway because the client quietly followed it. A real
    # client is entitled not to. The endpoint is /mcp, so /mcp must answer.
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        # Since Step 4, /mcp is behind OAuth. These tests are about the transport
        # contract rather than the gate, so they carry a real token and the gate
        # gets its own file (test_guard.py).
        c.headers["Authorization"] = f"Bearer {mint_access(app)}"
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
        result = r.json()["result"]
        assert result["serverInfo"]["name"] == "cloak-biz-scraper"
        assert "can file them into Notion" in result["instructions"]

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
            "agent_browser",
            "create_upload_url",
            "server_info",
            "list_profiles",
            "create_profile",
            "update_profile",
            "new_proxy_session",
            "delete_profile",
            # The in-chat live view (MCP Apps): one model-facing, one app-only.
            "show_browser",
            "live_view",
        }

    def test_profile_tools_describe_safety_and_destructive_boundaries(self, client):
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        assert "never exposes" in tools["list_profiles"]["description"]
        assert "direct mode" in tools["new_proxy_session"]["description"]
        deletion = tools["delete_profile"]["description"]
        assert "irreversible" in deletion and "Default" in deletion and "closing" in deletion

    def test_every_tool_declares_its_safety(self, client):
        """The machine-readable twin of the prose above.

        Silence is not neutral here: `destructiveHint` and `openWorldHint` both
        default to true, so a tool that declares nothing is read by a client as
        the most dangerous thing it could be. Pinned as whole sets rather than
        per tool so that adding a tool has to make a decision about it.
        """
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        hints = {name: tool["annotations"] for name, tool in tools.items()}

        assert {n for n, h in hints.items() if h.get("readOnlyHint")} == {
            "get_scrape_listing_results",
            "list_profiles",
            "list_instances",
            "get_instance",
            "server_info",
            "show_browser",
            "live_view",
        }
        assert {n for n, h in hints.items() if h.get("destructiveHint")} == {
            "agent_browser",
            "delete_profile",
            "close_instance",
        }
        # True only where the caller can steer the tool at an arbitrary external
        # entity — the three that take or follow a URL. create_instance makes
        # outbound calls too, but only to endpoints its arguments cannot move.
        assert {n for n, h in hints.items() if h.get("openWorldHint")} == {
            "scrape_listings",
            "archive_page",
            "agent_browser",
        }
        # archive_page skips a page that already has its Source Content section,
        # so the same call twice leaves the page as the first call did.
        assert {n for n, h in hints.items() if h.get("idempotentHint")} == {
            "close_instance", "archive_page",
        }

    def test_the_async_pair_is_described_as_a_pair(self, client):
        """A model that does not know to call back reports zero listings for a
        sweep that is running perfectly well."""
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        assert "get_scrape_listing_results" in tools["scrape_listings"]["description"]
        assert "job_id" in tools["scrape_listings"]["description"]

    def test_the_sweep_is_described_as_reading_any_listings_page(self, client):
        """What the model is told decides which URLs it passes: any site's
        listings page, BizBuySell natively, other sites with the classifier key,
        and the two ways a page that loads can still fail its source."""
        description = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}[
            "scrape_listings"]["description"]
        assert "BizBuySell only" not in description
        assert "read natively" in description
        assert "TypeSafe Classifier (e.g. Jev)" in description
        assert "found no list of businesses for" in description
        assert "don't read as business listings" in description
        flat = " ".join(description.split())
        assert "The same on a LATER page stops that URL's paging there: the pages before it " \
               "are kept and returned" in flat
        assert "site override" in description and "Settings" in description
        assert "archive_page" in description

    def test_the_sweep_describes_triage(self, client):
        """What the model needs to use triage_prompt: what it writes, what it
        needs, that a decided row is left alone, and where failures show up."""
        tool = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}[
            "scrape_listings"]
        description = " ".join(tool["description"].split())
        assert "triage_prompt" in description
        assert "Needs sync=true" in description
        assert "TypeSafe Classifier (e.g. Jev)" in description
        for column in ("Bot Triage", "Triage Reason", "Triaged At", "Criteria Version"):
            assert column in description
        assert "never re-triaged" in description
        assert "Source Content" in description
        assert "triage.failures" in description
        schema = tool["inputSchema"]["properties"]["triage_prompt"]
        assert {"type": "null"} in schema.get("anyOf", []) and schema.get("default") is None
        output = tool["outputSchema"]
        assert "triage" in output["properties"]
        assert {"bot_triage", "triage_p_review"} <= set(output["$defs"]["Listing"]["properties"])

    def test_archive_page_describes_the_guard_and_the_repeat(self, client):
        """Behaviour only: what a repeat call does, and what happens to a page
        that turns out to be a wall or an error."""
        description = " ".join({t["name"]: t for t in rpc(client, "tools/list").json()[
            "result"]["tools"]}["archive_page"]["description"].split())
        assert "already has a Source Content section gets nothing appended" in description
        assert "TypeSafe Classifier (e.g. Jev)" in description
        assert "login wall, error, removed listing or anti-bot page is not written" in description

    def test_money_is_advertised_as_a_string_not_a_number(self, client):
        """The contract an agent reads. Money is quoted, never interpreted."""
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        schema = tools["scrape_listings"]["outputSchema"]
        listing = schema["$defs"]["Listing"]["properties"]
        for field in ("asking_price", "revenue", "cashflow", "ebitda"):
            assert listing[field]["type"] == "string", field

    def test_create_instance_describes_optional_proxy_and_fail_closed_fallback(self, client):
        tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
        description = tools["create_instance"]["description"]
        assert "direct datacenter connection" in description
        assert "never bypassed with a direct retry" in description
        assert "must be configured" not in description
        assert "public build" in description
        assert "fewer" in description and "not been tested by us" in description
        assert "never silently" in description


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


class TestLoopbackOriginRule:
    """The Step 3 review's recommendation: a loopback Host demands a loopback
    Origin, restoring the local/LAN protection FastMCP's `allowed_hosts` gave —
    without the 421 that would 421 every Railway request.

    What it adds on top of `Origin == Host` is narrow and worth being precise
    about: the equality rule already refuses a foreign Origin. This rule is what
    stops `MCP_ALLOWED_ORIGINS` — the operator's own escape hatch — from being
    the foot-gun that lets a public site reach a server bound to localhost.
    """

    def test_a_loopback_host_refuses_a_foreign_origin(self, client):
        r = client.post("/mcp", json=INIT, headers={
            **HEADERS, "Host": "127.0.0.1:8000", "Origin": "https://evil.example",
        })
        assert r.status_code == 403

    def test_a_loopback_host_allows_a_loopback_origin(self, client):
        r = client.post("/mcp", json=INIT, headers={
            **HEADERS, "Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000",
        })
        assert r.status_code == 200

    def test_the_operator_allowlist_cannot_open_localhost_to_the_web(self, monkeypatch, client):
        """The rule's actual job. `MCP_ALLOWED_ORIGINS` exists for a browser-based
        client on a known origin; it must not become a way for a public site to
        reach a server on your laptop."""
        import app.routes.mcp as mcp_routes

        monkeypatch.setattr(mcp_routes, "_EXTRA_ORIGINS", ("https://trusted.example",))
        # Allowed against a real deployment's host...
        assert client.post("/mcp", json=INIT, headers={
            **HEADERS, "Host": "app.up.railway.app", "Origin": "https://trusted.example",
        }).status_code == 200
        # ...and still refused against loopback, allowlist or not.
        assert client.post("/mcp", json=INIT, headers={
            **HEADERS, "Host": "127.0.0.1:8000", "Origin": "https://trusted.example",
        }).status_code == 403

    def test_localhost_by_name_counts_as_loopback(self, client):
        r = client.post("/mcp", json=INIT, headers={
            **HEADERS, "Host": "localhost:8000", "Origin": "https://evil.example",
        })
        assert r.status_code == 403

    def test_a_deployed_host_is_unaffected(self, client):
        """The rule must not touch a real deployment, which is the whole reason
        `allowed_hosts` could not be used."""
        r = client.post("/mcp", json=INIT, headers={**HEADERS, "Host": "app.up.railway.app"})
        assert r.status_code == 200, "a Railway host must never 421 or 403 again"

    def test_a_lookalike_origin_is_refused(self, client):
        """testserver.evil.example ends with nothing we accept — a prefix or
        suffix match here would be the bug."""
        r = client.post(
            "/mcp", json=INIT, headers={**HEADERS, "Origin": "https://testserver.evil.example"}
        )
        assert r.status_code == 403


class TestWhatAFailureTellsTheCaller:
    """A refusal's sentence is the answer, so it must reach the caller; a crash's
    text can carry paths and internals, so it must not. The 2.x SDK shows a tool's
    own message only for ToolError, and mcp_server.REFUSALS is what bridges the
    two — these pin both halves of that line."""

    def _call(self, client, name, arguments):
        body = rpc(client, "tools/call", {"name": name, "arguments": arguments}).json()
        result = body["result"]
        assert result["isError"] is True
        return result["content"][0]["text"]

    def test_a_refusal_raised_as_runtime_error_still_reads_in_full(self, client):
        """InstanceNotDrivable is a RuntimeError, not a ValueError — the case a
        ValueError-only bridge would silently reduce to a bare "Error executing"."""
        text = self._call(client, "agent_browser", {"instance_id": "nope", "command": "get url"})
        assert text.startswith("Error executing tool agent_browser: No running browser")

    def test_a_crash_keeps_its_text_on_the_server(self, client, monkeypatch):
        def crash(*args, **kwargs):
            raise OSError("/data/secret/path is not readable")

        monkeypatch.setattr("app.services.views.server_info", crash)
        text = self._call(client, "server_info", {})
        assert text == "Error executing tool server_info"
        assert "/data" not in text


class TestSweepRefusalsReachBothDoors:
    """MCP and REST start a sweep through the same `submit`, so a classifier key
    that fails its check refuses the call on both, with the same sentence."""

    WC = "https://www.websiteclosers.com/businesses-for-sale/"

    @pytest.fixture
    def keyed(self, client, tmp_path, monkeypatch):
        from app.services.settings import SettingsService

        settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        monkeypatch.setattr(app.state.scrape, "_settings", settings)
        return settings

    def _check_fails(self, monkeypatch, error):
        from app.services.typesafe import TypeSafeCheck

        async def check(key=None, model=None):
            return TypeSafeCheck(ok=False, message=str(error), error=error)

        monkeypatch.setattr(app.state.typesafe, "check", check)

    def _mcp_call(self, client, urls):
        r = rpc(client, "tools/call", {"name": "scrape_listings", "arguments": {"urls": urls}})
        assert r.status_code == 200, r.text
        return r.json()["result"]

    def test_a_rejected_key_is_the_tool_s_answer_and_a_409(self, client, keyed, monkeypatch):
        from app.services.typesafe import TypeSafeAuthError

        keyed.update(typesafe_openrouter_api_key="sk-or-test")
        self._check_fails(monkeypatch, TypeSafeAuthError("OpenRouter rejected the key (HTTP 401)."))
        result = self._mcp_call(client, [self.WC])
        assert result["isError"] is True
        assert "OpenRouter rejected the key (HTTP 401)." in result["content"][0]["text"]

        r = client.post("/api/scrape", json={"urls": [self.WC]})
        assert r.status_code == 409
        assert "OpenRouter rejected the key (HTTP 401)." in r.json()["detail"]

    def test_an_outage_is_a_503_over_rest(self, client, keyed, monkeypatch):
        from app.services.typesafe import TypeSafeUnavailable

        keyed.update(typesafe_openrouter_api_key="sk-or-test")
        self._check_fails(monkeypatch, TypeSafeUnavailable("The TypeSafe Classifier could not answer."))
        r = client.post("/api/scrape", json={"urls": [self.WC]})
        assert r.status_code == 503 and "could not answer" in r.json()["detail"]
        assert "could not answer" in self._mcp_call(client, [self.WC])["content"][0]["text"]

    def test_without_a_key_both_say_where_it_goes(self, client, keyed):
        hint = "add an OpenRouter key under Settings → TypeSafe Classifier (e.g. Jev)"
        result = self._mcp_call(client, [self.WC])
        assert result["isError"] is True and hint in result["content"][0]["text"]
        r = client.post("/api/scrape", json={"urls": [self.WC]})
        assert r.status_code == 422 and hint in r.json()["detail"]

    def test_a_bizbuysell_listing_page_is_refused_even_with_a_key(self, client, keyed):
        keyed.update(typesafe_openrouter_api_key="sk-or-test")
        detail = "https://www.bizbuysell.com/business-opportunity/premier-restoration/2515728/"
        text = self._mcp_call(client, [detail])["content"][0]["text"]
        assert "bizbuysell.com is read by this app's own adapter" in text
        assert "archive_page" in text


SERP = "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"


class TestTriageRefusalsReachBothDoors:
    """A triage_prompt that cannot be honoured refuses the call on MCP and REST
    alike, with the same sentence — a setup problem (409), or an outage (503)."""

    @pytest.fixture
    def configured(self, client, tmp_path, monkeypatch):
        from app.services.settings import SettingsService

        settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        settings.update(notion_api_token="ntn_test", notion_db_id="db-test",
                        typesafe_openrouter_api_key="sk-or-test")
        monkeypatch.setattr(app.state.scrape, "_settings", settings)
        return settings

    def _both(self, client, arguments):
        r = rpc(client, "tools/call", {"name": "scrape_listings", "arguments": arguments})
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert result["isError"] is True, result
        rest = client.post("/api/scrape", json=arguments)
        return result["content"][0]["text"], rest

    def _check(self, monkeypatch, error=None):
        from app.services.typesafe import TypeSafeCheck

        async def check(key=None, model=None):
            if error is None:
                return TypeSafeCheck(ok=True, message="Working.")
            return TypeSafeCheck(ok=False, message=str(error), error=error)

        monkeypatch.setattr(app.state.typesafe, "check", check)

    def test_sync_false(self, client, configured):
        text, rest = self._both(client, {"urls": [SERP], "triage_prompt": "Reject restaurants."})
        assert "needs sync=true" in text
        assert rest.status_code == 409 and rest.json()["detail"] in text

    def test_a_blank_prompt(self, client, configured):
        text, rest = self._both(client, {"urls": [SERP], "sync": True, "triage_prompt": " "})
        assert "triage_prompt is empty" in text
        assert rest.status_code == 409

    def test_no_key(self, client, configured):
        configured.update(typesafe_openrouter_api_key="")
        text, rest = self._both(client, {"urls": [SERP], "sync": True,
                                         "triage_prompt": "Reject restaurants."})
        assert "no OpenRouter key is saved" in text
        assert rest.status_code == 409

    def test_a_rejected_key_even_for_bizbuysell(self, client, configured, monkeypatch):
        from app.services.typesafe import TypeSafeAuthError

        self._check(monkeypatch, TypeSafeAuthError("OpenRouter rejected the key (HTTP 401)."))
        text, rest = self._both(client, {"urls": [SERP], "sync": True,
                                         "triage_prompt": "Reject restaurants."})
        assert "OpenRouter rejected the key (HTTP 401)." in text
        assert rest.status_code == 409 and "HTTP 401" in rest.json()["detail"]

    def test_an_outage_is_a_503_over_rest(self, client, configured, monkeypatch):
        from app.services.typesafe import TypeSafeUnavailable

        self._check(monkeypatch, TypeSafeUnavailable("The TypeSafe Classifier could not answer."))
        text, rest = self._both(client, {"urls": [SERP], "sync": True,
                                         "triage_prompt": "Reject restaurants."})
        assert "could not answer" in text
        assert rest.status_code == 503

    def test_no_bot_triage_column(self, client, configured, monkeypatch):
        from app.stores.base import TriageUnavailable

        class NoColumn:
            async def prepare_triage(self, db_id, column_map=None):
                raise TriageUnavailable("This database has no 'Bot Triage' column.")

        self._check(monkeypatch)
        monkeypatch.setattr(app.state.scrape, "_store_factory", lambda settings: NoColumn())
        text, rest = self._both(client, {"urls": [SERP], "sync": True,
                                         "triage_prompt": "Reject restaurants."})
        assert "Can't triage into your Notion database" in text and "'Bot Triage'" in text
        assert rest.status_code == 409 and "'Bot Triage'" in rest.json()["detail"]
