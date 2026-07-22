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

    @pytest.mark.parametrize("redirect_uri", [
        "javascript:alert(document.domain)",
        "data:text/html,<script>alert(1)</script>",
        "file:///tmp/oauth-code",
        "/relative/callback",
        "not a URL",
        "https:missing-authority/callback",
    ])
    def test_registration_rejects_non_web_or_non_absolute_redirects(
        self, client, redirect_uri,
    ):
        """A successful login must only return its code over HTTP(S).

        The MCP SDK deliberately accepts any URI scheme here.  In particular,
        its ``AnyUrl`` parser accepts executable ``javascript:`` and ``data:``
        targets, so this application-level policy has to run before the SDK.
        Relative and malformed strings receive the same RFC 7591 error shape.
        """
        r = client.post("/register", json={"redirect_uris": [redirect_uri]})
        assert r.status_code == 400, r.text
        assert r.json()["error"] == "invalid_client_metadata"
        assert "redirect_uris" in r.json()["error_description"]
        assert r.headers["access-control-allow-origin"] == "*"

    @pytest.mark.parametrize("redirect_uri", [
        "https://client.example:8443/oauth/callback?source=connector",
        "http://127.0.0.1:43121/oauth/callback?source=native",
    ])
    def test_registration_preserves_valid_web_redirects(self, client, redirect_uri):
        """Custom ports, query strings, and loopback HTTP are legitimate.

        Loopback HTTP is the native-client exception: its random local port is
        how an installed connector receives the authorization response without
        pretending to own a public TLS endpoint.
        """
        r = client.post("/register", json={"redirect_uris": [redirect_uri]})
        assert r.status_code == 201, r.text
        assert redirect_uri in r.json()["redirect_uris"]

    def test_a_client_asking_only_for_the_auth_code_grant_can_register(self, client):
        """The hazard: this is RFC 7591's default, spelled out, and the SDK 400s it.

        §2: "If omitted, the default behavior is that the client will use only
        the 'authorization_code' Grant Type." The SDK hardcodes
        `{"authorization_code","refresh_token"}.issubset(...)` with no setting to
        turn it off — so it accepts the client that omits the field and refuses
        the client that says the same thing explicitly. A connector that
        registers this way could not be added at all.

        Measured against the deployed server before the fix: omitted -> 201,
        ["authorization_code"] -> 400, both -> 201.
        """
        r = client.post("/register", json={
            "redirect_uris": [REDIRECT],
            "client_name": "Auth-code-only Client",
            "grant_types": ["authorization_code"],
        })
        assert r.status_code == 201, r.text

    def test_omitting_grant_types_entirely_still_works(self, client):
        """The other half of the pair: don't fix the explicit case and break the
        implicit one.

        This path already worked and must keep working — the SDK's model supplies
        `["authorization_code","refresh_token"]` when the field is absent, so the
        check passes without us touching anything. Our substitution deliberately
        ignores this shape (`body.get("grant_types")` is None, not a list), and
        that has to stay true: a fix that rewrote every registration would be
        indistinguishable from this one until the day it wasn't.

        Measured against the deployed server before the fix: omitted -> 201.
        """
        r = client.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Silent Client",
        })
        assert r.status_code == 201, r.text
        assert set(r.json()["grant_types"]) == {"authorization_code", "refresh_token"}

    def test_the_substitution_is_told_to_the_client(self, client):
        """We hand it a grant it did not ask for, so it must be able to see that.

        RFC 7591 §3.2.1 lets a server "reject or replace any of the client's
        requested metadata values ... and substitute them with suitable values",
        and requires it to "return all registered metadata about this client".
        The registered set is what the client permanently carries, so returning
        the request's values rather than the registered ones would be a lie the
        client acts on.
        """
        r = client.post("/register", json={
            "redirect_uris": [REDIRECT],
            "client_name": "Auth-code-only Client",
            "grant_types": ["authorization_code"],
        })
        assert set(r.json()["grant_types"]) == {"authorization_code", "refresh_token"}

    def test_an_auth_code_only_client_can_actually_USE_the_flow(self, client):
        """201 is not the claim. A working connector is.

        Registering is worthless if the client it produced cannot then authorize
        and exchange a code — that is the thing Step 6 needs, and a status code
        does not prove it.
        """
        info = register(client, grant_types=["authorization_code"])
        verifier, challenge = pkce()
        code = login_and_get_code(client, info, challenge)
        r = exchange(client, info, code, verifier)
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]

    def test_a_grant_we_cannot_honour_is_still_refused(self, client):
        """The guard must still bite.

        If the fix were "stop checking grant_types", this would register happily
        and we would have advertised a grant /token cannot perform. We only
        substitute the exact shape RFC 7591 blesses; everything else is left to
        the SDK to judge.
        """
        r = client.post("/register", json={
            "redirect_uris": [REDIRECT],
            "client_name": "Client-credentials Client",
            "grant_types": ["client_credentials"],
        })
        assert r.status_code == 400, r.text

    def test_the_neighbouring_checks_survive_the_fix(self, client):
        """The fix re-feeds `RegistrationHandler.handle()`; it must not bypass it.

        The grant_types check sits *between* two load-bearing ones: scope-subset
        validation above, and `response_types must include "code"` below — the
        MCP spec's PKCE requirement. Reimplementing registration to dodge the bad
        check would silently drop both good ones, and no test would notice. So
        pin them: they run because we corrected the handler's input rather than
        replacing the handler.
        """
        no_code = client.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Implicit Client",
            "grant_types": ["authorization_code"],  # the path the fix touches
            "response_types": ["token"],
        })
        assert no_code.status_code == 400, "response_types check died with the fix"

        bad_scope = client.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "Greedy Client",
            "grant_types": ["authorization_code"],  # the path the fix touches
            "scope": "root-of-the-machine",
        })
        assert bad_scope.status_code == 400, "scope check died with the fix"

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


class TestTheConsentPageSaysWhoIsAsking:
    """Anyone can send the owner a link to this page — that is inherent to OAuth,
    and no check on the server can tell a phished authorize request from a real
    one, because it *is* a real one. The defence is what the page tells the
    reader, so it has to tell them something they can act on.
    """

    def consent_page(self, client, **overrides):
        info = register(client, **overrides)
        r = authorize(client, info, pkce()[1])
        blob = parse_qs(urlparse(r.headers["location"]).query)["p"][0]
        return client.get(f"/authorize/login?p={blob}").text

    def test_it_names_the_client(self, client):
        assert "Test Client" in self.consent_page(client)

    def test_it_shows_where_the_code_would_go(self, client):
        """The fact worth checking. A name is whatever the attacker typed; the
        redirect host is where the authorization code physically goes."""
        assert "client.example" in self.consent_page(client)

    def test_it_does_not_present_the_name_as_proof(self, client):
        page = self.consent_page(client, client_name="Anthropic Official")
        assert "Anthropic Official" in page
        assert "anyone can claim any name" in page, (
            "a self-declared name shown without caveat is an identity claim we cannot back"
        )

    def test_it_says_what_approving_costs(self, client):
        page = self.consent_page(client)
        assert "full use of this server" in page
        assert "close this page" in page.lower()

    def test_it_points_to_the_authoritative_railway_variable(self, client):
        page = self.consent_page(client)
        assert "APP_SECRET" in page
        assert "Railway service's" in page and "Variables" in page
        assert "changed it here" not in page

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


class TestConnectedAppsListing:
    """The display-only view behind Settings → Connected apps. It must show every
    registered client and never a secret, token, or code."""

    def test_list_returns_registered_clients(self, client):
        register(client, client_name="ChatGPT")
        register(client, client_name="Claude")

        apps = app.state.oauth.list_clients()
        names = {a["client_name"] for a in apps}
        assert {"ChatGPT", "Claude"} <= names
        assert all(a["client_id"] for a in apps)

    def test_a_registered_at_timestamp_is_surfaced_when_present(self, client):
        """The SDK stamps client_id_issued_at at registration, so a freshly
        registered client carries a registered-at the UI can show."""
        register(client, client_name="ChatGPT")
        app_view = app.state.oauth.list_clients()[0]
        assert app_view.get("registered_at")

    def test_a_record_without_a_timestamp_gets_none_fabricated(self, client):
        """If the stored record has no client_id_issued_at, the view omits it
        rather than inventing one."""
        register(client, client_name="ChatGPT")
        store = app.state.oauth._store
        cid = next(iter(store._load()["clients"]))
        store._load()["clients"][cid].pop("client_id_issued_at", None)
        assert "registered_at" not in store.list_clients()[0]

    def test_a_nameless_client_falls_back_to_its_redirect_host(self, client):
        """DCR does not require client_name; the redirect host is where codes
        physically go, so it is the honest label when no name was given."""
        info = register(client)
        store = app.state.oauth._store
        store._load()["clients"][info["client_id"]]["client_name"] = None
        view = next(a for a in store.list_clients() if a["client_id"] == info["client_id"])
        assert view["client_name"] == "client.example"

    def test_the_listing_never_contains_a_secret_token_or_code(self, client):
        """A confidential client has a client_secret in the store; it must never
        reach the view. Checked against the raw JSON so a nested leak cannot hide.
        """
        import json as _json

        info = register(client)  # confidential: gets a client_secret
        assert info.get("client_secret"), "expected a confidential client for this test"

        blob = _json.dumps(app.state.oauth.list_clients())
        assert info["client_secret"] not in blob
        for banned in ("client_secret", "code", "refresh_token", "access_token", "secret"):
            assert banned not in blob


class TestDisconnectRevokes:
    """Deleting a registration is the disconnect, and it has to actually revoke:
    the point is that a removed client cannot mint a new access token."""

    def test_delete_removes_the_client(self, client):
        info = register(client)
        assert app.state.oauth.delete_client(info["client_id"]) is True
        assert app.state.oauth._store.get_client(info["client_id"]) is None
        ids = {a["client_id"] for a in app.state.oauth.list_clients()}
        assert info["client_id"] not in ids

    def test_deleting_an_unknown_client_is_a_harmless_false(self, client):
        assert app.state.oauth.delete_client("no-such-client") is False

    def test_a_disconnected_client_cannot_refresh_over_http(self, client):
        """The end-to-end guarantee: obtain a refresh token, disconnect the
        client, and the refresh grant no longer buys an access token."""
        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)
        first = exchange(client, info, code, verifier).json()

        assert app.state.oauth.delete_client(info["client_id"]) is True

        r = client.post("/token", data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": info["client_id"],
            "client_secret": info.get("client_secret"),
        })
        # The SDK's ClientAuthenticator rejects the unknown client at /token (401)
        # before the grant runs; either way the client gets no new access token.
        assert r.status_code in (400, 401)
        assert "access_token" not in r.json()

    @pytest.mark.asyncio
    async def test_the_provider_itself_refuses_a_deleted_clients_refresh(self, client):
        """The second lock: even reached directly — past the SDK's client
        authentication — exchange_refresh_token refuses a client the store no
        longer knows. This is what keeps the guarantee off the SDK's call order.
        """
        from mcp.server.auth.provider import RefreshToken, TokenError

        verifier, challenge = pkce()
        info = register(client)
        code = login_and_get_code(client, info, challenge)
        first = exchange(client, info, code, verifier).json()

        provider = app.state.oauth
        stored = provider._store.get_client(info["client_id"])
        loaded = await provider.load_refresh_token(stored, first["refresh_token"])
        assert loaded is not None

        provider.delete_client(info["client_id"])
        with pytest.raises(TokenError) as excinfo:
            await provider.exchange_refresh_token(stored, loaded, loaded.scopes)
        assert excinfo.value.error == "invalid_grant"
