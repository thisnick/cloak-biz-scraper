"""One response policy across UI, OAuth, API, and the raw MCP endpoint."""
from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from conftest import isolate_auth, mint_access

from app.main import app
from app.response_security import ResponseSecurity

SECRET = "test-secret-value-long-enough"
REDIRECT = "https://client.example/callback"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        yield c


def _assert_browser_hardening(response) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp
    assert "connect-src 'self' ws: wss:" in csp


class TestAppWidePolicy:
    def test_login_page_is_hardened_and_not_cached(self, client):
        response = client.get("/login")
        _assert_browser_hardening(response)
        assert response.headers["cache-control"] == "no-store"

    def test_even_router_404s_receive_the_policy(self, client):
        response = client.get("/does-not-exist")
        assert response.status_code == 404
        _assert_browser_hardening(response)
        assert response.headers["cache-control"] == "no-store"

    def test_oauth_pages_drop_form_action_so_the_client_callback_is_not_blocked(self, client):
        # The /authorize login form, on success, redirects the browser to the MCP
        # client's registered callback (cross-origin, e.g. claude.ai). Chromium
        # enforces form-action ACROSS that redirect, so a strict form-action would
        # block every ChatGPT/Claude connector from finishing auth. These paths
        # must omit form-action while keeping the rest of the hardening; the
        # redirect_uri is validated server-side, which is the real control.
        csp = client.get("/authorize/login").headers["content-security-policy"]
        assert "form-action" not in csp
        assert "default-src 'self'" in csp and "frame-ancestors 'self'" in csp
        # Every non-OAuth page — including the dashboard login — keeps it strict.
        assert "form-action 'self'" in client.get("/login").headers["content-security-policy"]

    def test_https_gets_hsts_without_claiming_subdomains(self, client):
        value = client.get("/healthz").headers["strict-transport-security"]
        assert value == "max-age=31536000"
        assert "includeSubDomains" not in value
        assert "preload" not in value

    def test_plain_http_does_not_get_hsts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        with TestClient(app, base_url="http://testserver") as plain:
            isolate_auth(app, tmp_path)
            assert "strict-transport-security" not in plain.get("/healthz").headers

    def test_railway_forwarded_https_gets_hsts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        with TestClient(app, base_url="http://testserver") as edge:
            isolate_auth(app, tmp_path)
            response = edge.get("/healthz", headers={"X-Forwarded-Proto": "https"})
            assert response.headers["strict-transport-security"] == "max-age=31536000"

    def test_real_browser_keeps_same_origin_form_origin_concrete(self, client):
        """Chromium, not TestClient, proves the response policy and CSRF seam.

        Fetch serializes a non-CORS POST's Origin using its referrer policy.
        Under ``no-referrer`` Chromium sends the opaque value ``null`` even for
        this same-origin form; ``origin_allowed`` must reject it.  Render the
        real login form under the real response policy and observe only the
        Origin value the guard consumes; the destination host is fixed by the
        intercepted URL.
        """
        try:
            from playwright.sync_api import Error as PWError
            from playwright.sync_api import sync_playwright
        except ImportError:
            pytest.skip("Playwright is not installed")

        login = client.get("/login")
        response_headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Referrer-Policy": login.headers["referrer-policy"],
        }
        observed: list[str | None] = []

        with sync_playwright() as playwright:
            browser = None
            for launch in ({"channel": "chrome"}, {}):
                try:
                    browser = playwright.chromium.launch(headless=True, **launch)
                    break
                except PWError:
                    continue
            if browser is None:
                pytest.skip("no Chrome/Chromium available for referrer-policy check")

            page = browser.new_page()

            def serve(route):
                request = route.request
                if request.method == "GET":
                    route.fulfill(status=200, body=login.text, headers=response_headers)
                    return
                headers = request.all_headers()
                observed.append(headers.get("origin"))
                route.fulfill(status=200, content_type="text/plain", body="ok")

            page.route("https://policy.test/**", serve)
            try:
                page.goto("https://policy.test/login")
                page.locator("input[name=secret]").fill("synthetic-browser-test-secret")
                page.locator("button[type=submit]").click()
            finally:
                browser.close()

        assert observed == ["https://policy.test"]


class TestSensitiveResponsesNeverCache:
    def test_bearer_guard_error_is_no_store(self, client):
        """This 401 is written by raw ASGI middleware, not a FastAPI route."""
        response = client.get("/api/instances")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
        _assert_browser_hardening(response)

    def test_asgi_stream_chunks_are_forwarded_without_buffering(self):
        """MCP may stream ASGI body frames; header policy must not consume them.

        The production MCP manager currently chooses JSON responses for its
        stateless transport, but the protocol still advertises event streams in
        ``Accept`` and the SDK owns the response framing.  Pin the ASGI behavior
        so a future streamed SDK response remains a stream.
        """
        sent = []

        async def streaming_app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            })
            await send({"type": "http.response.body", "body": b"first\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"second\n"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def collect(message):
            sent.append(message)

        asyncio.run(ResponseSecurity(streaming_app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "scheme": "https",
                "headers": [],
            },
            receive,
            collect,
        ))

        start, first, second = sent
        assert dict(start["headers"])[b"content-type"] == b"text/event-stream"
        assert dict(start["headers"])[b"cache-control"] == b"no-store"
        assert first == {
            "type": "http.response.body", "body": b"first\n", "more_body": True,
        }
        assert second == {"type": "http.response.body", "body": b"second\n"}

    def test_authenticated_api_response_is_no_store(self, client):
        response = client.get(
            "/api/instances", headers={"Authorization": f"Bearer {mint_access(app)}"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def test_registration_client_secret_is_no_store_and_cors_survives(self, client):
        response = client.post("/register", json={"redirect_uris": [REDIRECT]})
        assert response.status_code == 201
        assert response.json()["client_secret"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == "*"

    def test_token_response_is_no_store_and_cors_survives(self, client):
        verifier = "a-verifier-long-enough-to-be-legitimate-0123456789"
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        info = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()
        authorize = client.get("/authorize", params={
            "response_type": "code",
            "client_id": info["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "response-security",
        })
        pending = parse_qs(urlparse(authorize.headers["location"]).query)["p"][0]
        consent = client.post(
            "/authorize/login", data={"p": pending, "secret": SECRET}
        )
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        response = client.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": info["client_id"],
            "client_secret": info["client_secret"],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        })
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == "*"

    def test_mcp_json_response_remains_usable_and_is_no_store(self, client):
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Authorization": f"Bearer {mint_access(app)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["tools"]
        assert response.headers["cache-control"] == "no-store"
        _assert_browser_hardening(response)


class TestVncTokenCaching:
    class _Instance:
        id = "inst1"
        origin = "interactive"
        subject = "owner"
        vnc_port = 5901

    def test_view_token_is_never_cacheable(self, client, monkeypatch):
        client.post("/login", data={"secret": SECRET})
        monkeypatch.setattr(
            app.state.instances, "get", lambda iid: self._Instance() if iid == "inst1" else None
        )
        response = client.get("/sessions/instances/inst1/vnc-token")
        assert response.status_code == 200
        assert response.json()["token"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"

    def test_control_token_is_never_cacheable(self, client, monkeypatch):
        client.post("/login", data={"secret": SECRET})
        monkeypatch.setattr(app.state.instances, "get", lambda iid: self._Instance())
        response = client.post("/sessions/instances/inst1/control")
        assert response.status_code == 200
        assert response.json()["token"]
        assert response.headers["cache-control"] == "no-store"
