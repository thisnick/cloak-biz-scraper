"""The OAuth 2.1 flow, driven the way a real client drives it.

These go through the actual HTTP endpoints — register, authorize, log in,
exchange — rather than calling the provider directly, because most of what can
go wrong here is in the wiring between the SDK's handlers and our login step,
and a provider unit test cannot see any of it.
"""
from __future__ import annotations

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from conftest import isolate_auth

from app.main import app

SECRET = "test-secret-value-long-enough"
REDIRECT = "https://client.example/callback"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        yield c


def pkce(verifier: str = "a-verifier-long-enough-to-be-legitimate-0123456789") -> tuple[str, str]:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def register(client, **overrides) -> dict:
    """Register and return the client's full information.

    The whole dict, not just the id, because DCR decides here whether this is a
    confidential client (it gets a secret and must present it at /token) or a
    public one. Passing only the id around would have every test silently
    exercise one shape.
    """
    body = {"redirect_uris": [REDIRECT], "client_name": "Test Client", **overrides}
    r = client.post("/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def authorize(client, info: dict, challenge: str, **extra):
    params = {
        "response_type": "code",
        "client_id": info["client_id"],
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "the-client-state",
        **extra,
    }
    return client.get("/authorize", params=params)


def login_and_get_code(client, info: dict, challenge: str) -> str:
    """The whole browser half of the flow: authorize, prove the secret, read the
    code out of the redirect the client would have received."""
    r = authorize(client, info, challenge)
    assert r.status_code == 302, r.text
    blob = parse_qs(urlparse(r.headers["location"]).query)["p"][0]

    r = client.post("/authorize/login", data={"p": blob, "secret": SECRET})
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def exchange(client, info: dict, code: str, verifier: str | None, **overrides):
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": info["client_id"],
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }
    if info.get("client_secret"):
        body["client_secret"] = info["client_secret"]
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/token", data=body)


class TestDiscovery:
    def test_the_issuer_is_the_host_that_was_asked(self, client):
        """Built per request, because Railway assigns the domain and never tells
        the app what it is — a baked-in issuer would be wrong for every real
        deployment, and asking the user for their own URL would be the second
        variable the whole product promises they will not need."""
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["issuer"] == "https://testserver"
        assert meta["authorization_endpoint"] == "https://testserver/authorize"
        assert meta["token_endpoint"] == "https://testserver/token"
        assert meta["registration_endpoint"] == "https://testserver/register"

    def test_a_forwarded_scheme_is_honoured(self, client):
        """Railway terminates TLS and speaks http to the container. Without this
        the issuer would advertise http://, which RFC 8414 clients refuse — and
        it would look perfect in local testing, where http is the truth."""
        meta = client.get(
            "/.well-known/oauth-authorization-server",
            headers={"X-Forwarded-Proto": "https", "Host": "app.up.railway.app"},
        ).json()
        assert meta["issuer"] == "https://app.up.railway.app"

    def test_only_s256_is_offered(self, client):
        """`plain` is a PKCE challenge that is not a challenge; OAuth 2.1 drops it."""
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["code_challenge_methods_supported"] == ["S256"]

    def test_the_protected_resource_names_this_server_as_its_own_as(self, client):
        meta = client.get("/.well-known/oauth-protected-resource").json()
        assert meta["resource"] == "https://testserver/mcp"
        assert meta["authorization_servers"] == ["https://testserver"]

    def test_both_metadata_paths_agree(self, client):
        """RFC 9728 §3.1 puts it under the resource path; clients in the wild ask
        for the bare one too. A 404 on discovery is an unconnectable server."""
        bare = client.get("/.well-known/oauth-protected-resource").json()
        suffixed = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert bare == suffixed


class TestBrowserBasedClientsCanReachTheFlow:
    """The MCP Inspector runs in a web page, and the SDK's own create_auth_routes
    puts CORS on exactly these endpoints for that reason. We mount the handlers
    directly, so its wrapper is not in our path and we have to do it ourselves —
    an omission that would only show up as "the Inspector cannot register".
    """

    def test_discovery_is_readable_cross_origin(self, client):
        r = client.get("/.well-known/oauth-authorization-server")
        assert r.headers["access-control-allow-origin"] == "*"

    @pytest.mark.parametrize("path", ["/register", "/token"])
    def test_preflight_is_answered(self, client, path):
        r = client.options(path, headers={
            "Origin": "https://inspector.example",
            "Access-Control-Request-Method": "POST",
        })
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "*"
        assert "POST" in r.headers["access-control-allow-methods"]

    def test_the_registration_response_is_readable_cross_origin(self, client):
        r = client.post("/register", json={"redirect_uris": [REDIRECT]})
        assert r.headers["access-control-allow-origin"] == "*"

    def test_the_token_response_is_readable_cross_origin(self, client):
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)
        r = exchange(client, info, code, verifier)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"

    def test_authorize_is_not_cross_origin_readable(self, client):
        """The one page where APP_SECRET gets typed. A script on another origin
        has no business reading it."""
        info = register(client)
        r = authorize(client, info, pkce()[1])
        blob = parse_qs(urlparse(r.headers["location"]).query)["p"][0]
        page = client.get(f"/authorize/login?p={blob}")
        assert "access-control-allow-origin" not in page.headers


class TestDynamicClientRegistration:
    def test_a_client_can_register_itself(self, client):
        """Without DCR, ChatGPT and Claude cannot connect at all."""
        info = register(client)
        assert info["client_id"]
        assert REDIRECT in info["redirect_uris"]

    def test_registration_survives_a_restart(self, client, tmp_path):
        """The one that Railway's scale-to-zero makes non-negotiable.

        An in-memory registry would de-register every client on the first
        six-minute nap, and it would look like the connector logging itself out
        at random — a bug that needs an idle timeout to reproduce and never
        happens while anyone is watching.

        The restart is simulated by building a brand-new provider over the same
        volume and serving from it: nothing in-process is reused, so a client
        that is still recognised can only have come off the disk.
        """
        from app.services.oauth import OAuthProvider, OAuthStore

        info = register(client)

        app.state.oauth = OAuthProvider(
            OAuthStore(tmp_path / "oauth.json", tmp_path / ".dek"), app.state.secret
        )
        r = authorize(client, info, pkce()[1])
        # Recognised rather than "client not found".
        assert r.status_code == 302, r.text

    def test_a_client_registered_on_another_volume_is_unknown(self, client, tmp_path):
        """The control for the test above: proof that recognition comes from the
        store rather than from the endpoint waving everything through."""
        from app.services.oauth import OAuthProvider, OAuthStore

        info = register(client)
        app.state.oauth = OAuthProvider(
            OAuthStore(tmp_path / "elsewhere.json", tmp_path / ".dek"), app.state.secret
        )
        assert authorize(client, info, pkce()[1]).status_code == 400

    def test_an_unknown_client_is_refused(self, client):
        r = authorize(client, {"client_id": "no-such-client-id"}, pkce()[1])
        assert r.status_code == 400


class TestTheHappyPath:
    def test_register_authorize_login_exchange(self, client):
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        r = exchange(client, info, code, verifier)
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["token_type"] == "Bearer"
        assert payload["access_token"]
        assert payload["refresh_token"]

        # The token is not just well-formed, it opens the door.
        assert client.get(
            "/api/instances", headers={"Authorization": f"Bearer {payload['access_token']}"}
        ).status_code == 200

    def test_a_public_client_needs_no_secret(self, client):
        """The other half of DCR, and the shape a browser-based or native client
        registers as: no client_secret at all, PKCE carrying the whole proof.
        Only testing the confidential shape would leave this path unexercised
        until a real client hit it."""
        verifier, challenge = pkce()
        info = register(client, token_endpoint_auth_method="none")
        assert not info.get("client_secret")

        code = login_and_get_code(client, info, challenge)
        r = exchange(client, info, code, verifier)
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]

    def test_the_state_is_returned_untouched(self, client):
        """The client's CSRF defence. Losing it silently breaks their check."""
        _, challenge = pkce()
        info = register(client)
        r = authorize(client, info, challenge)
        blob = parse_qs(urlparse(r.headers["location"]).query)["p"][0]
        r = client.post("/authorize/login", data={"p": blob, "secret": SECRET})
        assert parse_qs(urlparse(r.headers["location"]).query)["state"] == ["the-client-state"]

    def test_a_refresh_token_buys_a_new_access_token(self, client):
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)
        first = exchange(client, info, code, verifier).json()

        r = client.post("/token", data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": info["client_id"],
            "client_secret": info.get("client_secret"),
        })
        assert r.status_code == 200, r.text
        assert client.get(
            "/api/instances",
            headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        ).status_code == 200


class TestLoginIsWhatAuthorizes:
    def test_the_wrong_secret_yields_no_code(self, client):
        _, challenge = pkce()
        info = register(client)
        r = authorize(client, info, challenge)
        blob = parse_qs(urlparse(r.headers["location"]).query)["p"][0]

        r = client.post("/authorize/login", data={"p": blob, "secret": "not-the-secret"})
        assert r.status_code == 401
        assert "location" not in r.headers

    def test_a_forged_pending_blob_is_refused(self, client):
        """The blob carries where the code gets delivered. If it could be edited,
        an attacker would redirect the code to themselves."""
        from app.services import signing

        forged = signing.issue(
            {"aud": "oauth:pending", "cid": "x", "cc": "y", "ru": "https://evil.example/steal",
             "rux": True, "state": None, "scopes": ["mcp"], "res": None},
            "a-different-secret", ttl_sec=600,
        )
        r = client.post("/authorize/login", data={"p": forged, "secret": SECRET})
        assert r.status_code == 400
        assert "location" not in r.headers

    def test_an_expired_pending_blob_is_refused(self, client):
        from app.services import signing

        stale = signing.issue(
            {"aud": "oauth:pending", "cid": "x", "cc": "y", "ru": REDIRECT,
             "rux": True, "state": None, "scopes": ["mcp"], "res": None},
            SECRET, ttl_sec=-1,
        )
        assert client.post("/authorize/login", data={"p": stale, "secret": SECRET}).status_code == 400


class TestPKCEIsEnforced:
    def test_a_missing_challenge_is_refused_at_authorize(self, client):
        """PKCE is not optional in OAuth 2.1, and this is where it is required."""
        client_id = register(client)["client_id"]
        r = client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        })
        # Refused, one way or another — never a 302 to the login form.
        assert r.status_code != 302 or "error=" in r.headers.get("location", "")

    def test_an_empty_challenge_never_reaches_the_login_form(self, client):
        """Measured against the live server: the SDK types `code_challenge` as a
        plain `str`, so an EMPTY one validates and redirects to the login form.

        It was never exploitable — nothing hashes to "", so the code could not be
        redeemed — but it was worse than useless: the user would be asked to type
        APP_SECRET, the one credential protecting everything, to authorize a
        client whose code was dead on arrival, and the only symptom would be an
        `invalid_grant` naming nothing they did.
        """
        info = register(client)
        r = client.get("/authorize", params={
            "response_type": "code", "client_id": info["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": "", "code_challenge_method": "S256",
        })
        assert "/authorize/login" not in r.headers.get("location", ""), (
            "an empty PKCE challenge must not get as far as asking for the secret"
        )
        assert "error=invalid_request" in r.headers.get("location", "")

    def test_a_short_challenge_is_refused(self, client):
        """RFC 7636 §4.2 bounds the challenge at 43-128 characters; a short one
        is not an S256 digest of anything."""
        info = register(client)
        r = client.get("/authorize", params={
            "response_type": "code", "client_id": info["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": "abc", "code_challenge_method": "S256",
        })
        assert "/authorize/login" not in r.headers.get("location", "")

    def test_a_real_challenge_still_gets_through(self, client):
        """The control: the length check must not refuse a legitimate challenge."""
        info = register(client)
        r = authorize(client, info, pkce()[1])
        assert "/authorize/login" in r.headers["location"]

    def test_the_plain_method_is_refused(self, client):
        """`plain` is a PKCE challenge that is not a challenge. OAuth 2.1 drops
        it, and the metadata only ever advertised S256."""
        info = register(client)
        r = client.get("/authorize", params={
            "response_type": "code", "client_id": info["client_id"],
            "redirect_uri": REDIRECT, "code_challenge": pkce()[1],
            "code_challenge_method": "plain",
        })
        assert "/authorize/login" not in r.headers.get("location", "")

    def test_the_wrong_verifier_is_refused_at_token(self, client):
        """The check that makes an intercepted code useless."""
        _, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        r = exchange(client, info, code, "a-completely-different-verifier-9876543210")
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_a_missing_verifier_is_refused_at_token(self, client):
        _, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        r = exchange(client, info, code, None)
        assert r.status_code == 400

    def test_a_failed_verifier_does_not_burn_the_code(self, client):
        """A client that fumbles PKCE should be able to retry, not be sent back
        through the whole browser flow. Consuming on load rather than on exchange
        would turn a retry-able mistake into a dead end."""
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        assert exchange(client, info, code, "wrong-verifier-0123456789").status_code == 400
        assert exchange(client, info, code, verifier).status_code == 200


class TestCodesAreSingleUse:
    def test_a_code_cannot_be_exchanged_twice(self, client):
        """OAuth 2.1 / RFC 6749 §10.5. A replayed code is the classic way a
        leaked authorization code becomes a second, silent session."""
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        assert exchange(client, info, code, verifier).status_code == 200
        second = exchange(client, info, code, verifier)
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"

    def test_an_expired_code_is_refused(self, client):
        """Backdated in the store rather than by patching the clock: `time` is
        the module the whole process shares, and moving it moves it for the HTTP
        client too. This exercises the real expiry check on a real record.
        """
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)

        from app.services import oauth as oauth_service

        store = app.state.oauth._store
        store._load()["codes"][oauth_service._hash(code)]["expires_at"] = time.time() - 1

        r = exchange(client, info, code, verifier)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_another_clients_code_is_refused(self, client):
        """A code belongs to the client it was issued to. Otherwise a second
        registered client could spend the first one's code."""
        verifier, challenge = pkce()
        first = register(client)
        second = register(client, client_name="Other")
        code = login_and_get_code(client, first, challenge)

        r = exchange(client, second, code, verifier)
        assert r.status_code in (400, 401)
