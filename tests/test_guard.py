"""The gate in front of the tool surface.

Step 3 shipped `/mcp` and `/api/*` answering 200 to anyone who found the URL.
Every test here is a way in that must now be closed, and a way in that must
stay open — a gate that also blocks discovery or the healthcheck is a gate onto
a server nobody can connect to or deploy.
"""
from __future__ import annotations

import pytest
from conftest import isolate_auth, mint_access
from fastapi.testclient import TestClient

from app.main import app
from app.services import oauth as oauth_service
from app.services import signing

SECRET = "test-secret-value-long-enough"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        yield c


# Every door onto the service layer. Listed explicitly rather than discovered
# from the router: a route added without auth is exactly the bug this file
# exists to catch, and deriving the list from the app would let it in.
PROTECTED = [
    ("POST", "/mcp"),
    ("POST", "/api/scrape"),
    ("GET", "/api/scrape/anything"),
    ("POST", "/api/archive"),
    ("GET", "/api/profiles"),
    ("POST", "/api/profiles"),
    ("PATCH", "/api/profiles"),
    ("POST", "/api/profiles/new-proxy-session"),
    ("DELETE", "/api/profiles?name=anything"),
    ("POST", "/api/instances"),
    ("GET", "/api/instances"),
    ("GET", "/api/instances/abc"),
    ("DELETE", "/api/instances/abc"),
]


class TestUnauthenticatedIsRefused:
    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_no_token_is_401(self, client, method, path):
        assert client.request(method, path, json={}).status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED)
    def test_the_401_points_at_the_metadata(self, client, method, path):
        """The header that makes a client able to fix itself.

        A bare 401 makes an MCP client give up; this one sends it to discovery,
        where it can register and come back with a token. It is protocol, not
        decoration.
        """
        header = client.request(method, path, json={}).headers["www-authenticate"]
        assert header.startswith("Bearer ")
        assert 'error="invalid_token"' in header
        assert (
            'resource_metadata="https://testserver/.well-known/'
            'oauth-protected-resource/mcp"' in header
        )

    def test_the_advertised_metadata_url_actually_resolves(self, client):
        """The header is only useful if the URL in it is real — a client follows
        it verbatim, so a typo here is an unconnectable server."""
        header = client.post("/mcp", json={}).headers["www-authenticate"]
        url = header.split('resource_metadata="')[1].split('"')[0]
        assert client.get(url.replace("https://testserver", "")).status_code == 200

    def test_a_garbage_token_is_401(self, client):
        r = client.post("/mcp", json={}, headers={"Authorization": "Bearer nonsense"})
        assert r.status_code == 401

    def test_a_token_signed_with_another_secret_is_401(self, client):
        forged = signing.issue(
            {"aud": oauth_service._AUD_ACCESS, "sub": "owner", "cid": "x"},
            "a-completely-different-secret", ttl_sec=3600,
        )
        r = client.post("/mcp", json={}, headers={"Authorization": f"Bearer {forged}"})
        assert r.status_code == 401

    def test_an_expired_token_is_401(self, client):
        expired = mint_access(app, ttl_sec=-1)
        r = client.post("/mcp", json={}, headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401

    def test_a_refresh_token_is_not_an_access_token(self, client):
        """Both are HMAC'd with the same secret, so the signature alone cannot
        tell them apart. A refresh token lives thirty days and is stored by the
        client; if it were accepted here, the hour-long access token would be
        pointless."""
        pair = app.state.oauth._mint(
            subject="owner", client_id="c", scopes=oauth_service.SCOPES, resource=None
        )
        r = client.post("/mcp", json={}, headers={"Authorization": f"Bearer {pair.refresh_token}"})
        assert r.status_code == 401

    def test_a_session_cookie_does_not_open_the_api(self, client):
        """The UI session is for the UI. Honouring it here would make every
        /api/* route reachable from any page the owner has open."""
        from app.services import sessions

        client.cookies.set(sessions.COOKIE_NAME, sessions.issue(SECRET))
        assert client.get("/api/instances").status_code == 401
        client.cookies.clear()

    def test_a_cdp_token_does_not_open_the_api(self, client):
        """The leakiest token in the system — it rides in a URL — must not be
        the most powerful."""
        from app.services import tokens

        bad = tokens.issue("abc", SECRET)
        assert client.get("/api/instances",
                          headers={"Authorization": f"Bearer {bad}"}).status_code == 401


class TestAuthenticatedIsServed:
    def test_a_real_token_opens_mcp(self, client):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Authorization": f"Bearer {mint_access(app)}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code == 200
        assert "tools" in r.json()["result"]

    def test_a_real_token_opens_the_api(self, client):
        r = client.get("/api/instances", headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 200

    def test_bearer_is_case_insensitive(self, client):
        """RFC 7235 says the scheme is case-insensitive, and clients vary."""
        r = client.get("/api/instances", headers={"Authorization": f"bEaReR {mint_access(app)}"})
        assert r.status_code == 200


class TestWhatMustStayOpen:
    def test_healthz_needs_no_token(self, client):
        """Railway's healthcheck has no credential to offer, and a deployment
        that fails its healthcheck never boots at all."""
        assert client.get("/healthz").status_code == 200

    @pytest.mark.parametrize("path", [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ])
    def test_discovery_needs_no_token(self, client, path):
        """The machinery for getting a token cannot itself require one."""
        assert client.get(path).status_code == 200

    def test_registration_needs_no_token(self, client):
        r = client.post("/register", json={"redirect_uris": ["https://example.com/cb"]})
        assert r.status_code == 201

    def test_the_login_page_needs_no_token(self, client):
        assert client.get("/login").status_code == 200


class TestRotationRevokesEverything:
    def test_changing_the_secret_kills_live_access_tokens(self, client, monkeypatch):
        """The revocation story, and the reason there is no /revoke endpoint.

        Tokens are stateless, so nothing is stored that could be struck out —
        the only lever is the signing key, and this proves the lever works.
        Changing APP_SECRET is what an operator does in Railway; here it is one
        setenv, and monkeypatch restores it, so nothing leaks into a later test.
        """
        token = mint_access(app)
        assert client.get("/api/instances",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 200

        monkeypatch.setenv("APP_SECRET", "a-brand-new-secret-value-here")
        assert client.get("/api/instances",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401
