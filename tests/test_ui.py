"""The settings UI: login, session handling, saving, and rotation.

base_url is https so the client stores and re-sends the Secure session cookie —
over http it would be silently dropped, and every authenticated test would pass
for the wrong reason (or fail for a reason that never happens in production,
since Railway is HTTPS).
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app
from app.services import sessions
from app.services.secret import SecretService
from app.services.settings import SettingsService
from app.stores.notion import API

SECRET = "test-secret-long-enough-1"


def shown(response) -> str:
    """The page as a reader sees it.

    Jinja escapes quotes, so a message about a property named 'Listing ID'
    reaches the HTML as &#39;Listing ID&#39;. Asserting against the raw source
    would mean writing the escapes into the tests and quietly weakening them.
    """
    import html

    return html.unescape(response.text)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A signed-out client on a private volume."""
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    with TestClient(app, base_url="https://testserver") as c:
        # Repoint the services at a per-test volume; the module-level app is
        # shared, and one test's saved licence key must not be another's fixture.
        app.state.settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        app.state.secret = SecretService(tmp_path / "auth.json", tmp_path / ".dek")
        app.state.secret.bootstrap()
        yield c


@pytest.fixture
def auth(client):
    """A signed-in client."""
    client.post("/login", data={"secret": SECRET})
    return client


class TestLogin:
    def test_right_secret_signs_in(self, client):
        response = client.post("/login", data={"secret": SECRET}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert sessions.COOKIE_NAME in response.cookies

    def test_wrong_secret_rejected(self, client):
        response = client.post("/login", data={"secret": "nope"}, follow_redirects=False)
        assert response.status_code == 401
        assert sessions.COOKIE_NAME not in response.cookies
        assert "not the right secret" in response.text

    def test_empty_secret_rejected(self, client):
        assert client.post("/login", data={"secret": ""}, follow_redirects=False).status_code == 401

    def test_cookie_is_locked_down(self, client):
        response = client.post("/login", data={"secret": SECRET}, follow_redirects=False)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie   # no page script ever needs to read it
        assert "Secure" in cookie     # it is a bearer credential on a public host
        assert "SameSite=lax" in cookie  # blocks cross-site POSTs, survives OAuth redirects

    def test_signed_out_is_redirected_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_signed_in_sees_the_settings(self, auth):
        response = auth.get("/")
        assert response.status_code == 200
        assert "Residential proxy" in response.text

    def test_logout_clears_the_session(self, auth):
        auth.post("/logout")
        assert auth.get("/", follow_redirects=False).status_code == 303

    def test_a_forged_cookie_does_not_work(self, client):
        client.cookies.set(sessions.COOKIE_NAME, sessions.issue("some-other-secret-entirely"))
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_login_page_explains_an_unconfigured_deployment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APP_SECRET", raising=False)
        with TestClient(app, base_url="https://testserver") as c:
            app.state.secret = SecretService(tmp_path / "auth.json", tmp_path / ".dek")
            app.state.secret.bootstrap()
            page = c.get("/login")
            assert "no secret set" in page.text
            assert "Variables" in page.text, "say where to set it, not just that it is unset"
            # And no secret means no way in — not a way in for everyone.
            assert c.post("/login", data={"secret": ""}, follow_redirects=False).status_code == 503


class TestWriteRoutesRequireAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/settings/cloakbrowser",
            "/settings/proxy",
            "/settings/pool",
            "/settings/notion",
            "/settings/notion/select",
            "/settings/notion/verify",
            "/settings/notion/create",
            "/settings/secret",
        ],
    )
    def test_signed_out_post_is_refused(self, client, path):
        response = client.post(path, data={}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


class TestSaving:
    def test_saves_and_persists(self, auth, tmp_path):
        auth.post(
            "/settings/proxy",
            data={"proxy_user": "u1", "proxy_password": "pw", "proxy_host": "h.example.com",
                  "proxy_port": "1000", "proxy_country": "US", "proxy_region": "california"},
        )
        reopened = SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()
        assert reopened.proxy_user == "u1"
        assert reopened.proxy_host == "h.example.com"

    def test_secrets_are_not_rendered_back_into_the_page(self, auth):
        auth.post("/settings/cloakbrowser", data={"cloakbrowser_license_key": "cb_verysecret"})
        page = auth.get("/")
        assert "cb_verysecret" not in page.text
        assert "licence saved" in page.text

    def test_blank_secret_field_keeps_the_saved_value(self, auth):
        auth.post("/settings/cloakbrowser", data={"cloakbrowser_license_key": "cb_keepme"})
        # Saving the pin alone must not wipe a licence the user did not retype.
        auth.post("/settings/cloakbrowser",
                  data={"cloakbrowser_license_key": "", "cloakbrowser_version": "148.0.7778.215.5"})
        settings = app.state.settings.load()
        assert settings.cloakbrowser_license_key == "cb_keepme"
        assert settings.cloakbrowser_version == "148.0.7778.215.5"

    def test_malformed_pin_is_reported_not_stored(self, auth):
        response = auth.post("/settings/cloakbrowser", data={"cloakbrowser_version": "latest"})
        assert response.status_code == 400
        page = shown(response)
        assert "Invalid browser version pin" in page
        assert app.state.settings.load().cloakbrowser_version == ""

    def test_validation_errors_do_not_leak_pydantic_machinery(self, auth):
        # str(ValidationError) reads "1 validation error for Settings
        # cloakbrowser_version Value error, ... [type=value_error,
        # input_value='latest']" — a traceback pasted at someone who typed in a
        # box. Only the sentence our validator wrote should survive.
        page = shown(auth.post("/settings/cloakbrowser", data={"cloakbrowser_version": "latest"}))
        for noise in ("validation error for Settings", "type=value_error", "input_value",
                      "further information", "Value error,"):
            assert noise not in page, f"pydantic internals leaked to the user: {noise!r}"

    def test_pool_saves(self, auth):
        auth.post("/settings/pool", data={"max_instances": "6", "interactive_reserve": "2"})
        assert app.state.settings.load().max_instances == 6

    def test_pool_warns_above_eight_but_obeys(self, auth):
        response = auth.post("/settings/pool", data={"max_instances": "12", "interactive_reserve": "1"})
        assert response.status_code == 200
        assert app.state.settings.load().max_instances == 12, "guidance, not a cap"
        assert "is a lot" in response.text

    def test_impossible_reserve_is_refused_readably(self, auth):
        response = auth.post("/settings/pool", data={"max_instances": "2", "interactive_reserve": "2"})
        assert response.status_code == 400
        page = shown(response)
        assert "must be less than" in page
        assert "type=value_error" not in page and "validation error for Settings" not in page
        assert app.state.settings.load().max_instances == 4

    def test_the_cost_guidance_is_on_the_page(self, auth):
        page = auth.get("/").text
        assert "0.5–1 GB" in page
        assert "$10/GB per month" in page
        assert "sleeps when idle" in page
        assert "costs pennies" in page


class TestLicenceVerify:
    def test_failure_is_reported_as_a_failure(self, auth, monkeypatch):
        from app.services import license as license_service
        from app.services.license import LicenseReport

        async def failed(key, pin=""):
            return LicenseReport(ok=False, message="Nope.")

        monkeypatch.setattr(license_service, "verify", failed)
        response = auth.post(
            "/settings/cloakbrowser",
            data={"action": "verify", "cloakbrowser_license_key": "cb_x"},
        )
        # A failed verification is not a successful page. 200 here would make
        # "did my licence work?" answerable only by reading the banner colour.
        assert response.status_code == 400

    def test_no_key_yet_asks_for_one(self, auth):
        response = auth.post("/settings/cloakbrowser", data={"action": "verify"})
        assert response.status_code == 400
        assert "No licence key yet" in shown(response)


class TestProxyTest:
    @respx.mock
    def test_reports_what_it_measured(self, auth, monkeypatch):
        from app.services import geo

        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(
            geo, "_geolocate", lambda ip: ("America/Los_Angeles", "en-US", "US", "San Jose")
        )
        response = auth.post(
            "/settings/proxy",
            data={"action": "test", "proxy_user": "u", "proxy_password": "pw",
                  "proxy_host": "h.example.com", "proxy_port": "1000"},
        )
        assert response.status_code == 200
        assert "45.12.3.4" in response.text
        assert "San Jose" in response.text
        assert "America/Los_Angeles" in response.text

    @respx.mock
    def test_never_claims_to_have_checked_the_credentials(self, auth, monkeypatch):
        """Evomi accepts any password. A green result here is not evidence the
        credentials are right, and the page must not imply it is."""
        from app.services import geo

        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(geo, "_geolocate", lambda ip: ("America/Los_Angeles", "en-US", "US", "San Jose"))
        page = auth.post(
            "/settings/proxy",
            data={"action": "test", "proxy_user": "u", "proxy_password": "pw",
                  "proxy_host": "h.example.com", "proxy_port": "1000"},
        ).text
        lowered = page.lower()
        for lie in ("credentials ok", "credentials verified", "credentials are valid",
                    "password ok", "password verified", "authentication succeeded"):
            assert lie not in lowered
        assert "does <strong>not</strong>" in page and "prove your password is right" in page

    @respx.mock
    def test_unreachable_proxy_is_an_error_not_a_shrug(self, auth):
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        response = auth.post(
            "/settings/proxy",
            data={"action": "test", "proxy_user": "u", "proxy_password": "pw",
                  "proxy_host": "dead.example.com", "proxy_port": "1000"},
        )
        assert response.status_code == 400
        assert "exit IP is unknown" in response.text
        assert "America/Los_Angeles" not in response.text, "never a timezone we did not measure"

    @respx.mock
    def test_a_failed_test_leaves_the_page_saying_it_is_broken(self, auth):
        """The defect: a user who walks away must not come back to a green light.

        Every field is filled in, so any status derived from the form says
        "configured" — while the config cannot route and the error banner died
        with the response that carried it. Saving what they typed is right; the
        page just has to keep saying it does not work.
        """
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        auth.post(
            "/settings/proxy",
            data={"action": "test", "proxy_user": "u", "proxy_password": "pw",
                  "proxy_host": "192.0.2.1", "proxy_port": "1000"},
        )
        # Come back later. Fresh GET, no banner from the POST.
        page = shown(auth.get("/"))
        assert "not working" in page
        assert "did not work when it was last tested" in page
        assert "Scrapes will fail until it does" in page
        assert '<span class="pill unset">not working</span>' in auth.get("/").text
        # And the values they typed are still there to fix, not thrown away.
        assert app.state.settings.load().proxy_host == "192.0.2.1"

    @respx.mock
    def test_a_passing_test_is_remembered_too(self, auth, monkeypatch):
        from app.services import geo

        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(geo, "_geolocate", lambda ip: ("America/Los_Angeles", "en-US", "US", "San Jose"))
        auth.post(
            "/settings/proxy",
            data={"action": "test", "proxy_user": "u", "proxy_password": "pw",
                  "proxy_host": "h.example.com", "proxy_port": "1000"},
        )
        page = shown(auth.get("/"))
        assert '<span class="pill set">working</span>' in auth.get("/").text
        assert "45.12.3.4" in page and "Last tested" in page

    def test_filling_the_form_in_is_not_evidence_of_anything(self, auth):
        auth.post(
            "/settings/proxy",
            data={"proxy_user": "u", "proxy_password": "pw", "proxy_host": "h.example.com",
                  "proxy_port": "1000"},
        )
        response = auth.get("/")
        page = shown(response)
        assert '<span class="pill warn">not tested yet</span>' in response.text
        assert "Filling the form in does not prove the proxy routes" in page
        assert '<span class="pill set">working</span>' not in response.text

    @respx.mock
    def test_editing_the_proxy_retires_the_old_verdict(self, auth, monkeypatch):
        """A 'working' measured against a different host is not a measurement of
        this one."""
        from app.services import geo

        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(geo, "_geolocate", lambda ip: ("America/Los_Angeles", "en-US", "US", "San Jose"))
        auth.post("/settings/proxy", data={"action": "test", "proxy_user": "u",
                                           "proxy_password": "pw", "proxy_host": "h.example.com",
                                           "proxy_port": "1000"})
        assert app.state.settings.load().proxy_status() == "working"

        auth.post("/settings/proxy", data={"proxy_user": "u", "proxy_password": "pw",
                                           "proxy_host": "somewhere-else.example.com",
                                           "proxy_port": "1000"})
        assert app.state.settings.load().proxy_status() == "untested"
        assert "not tested yet" in shown(auth.get("/"))

    def test_incomplete_proxy_is_not_tested(self, auth):
        response = auth.post("/settings/proxy", data={"action": "test", "proxy_user": "u"})
        assert response.status_code == 400
        assert "Fill in the username" in response.text


class TestNotionUi:
    @respx.mock
    def test_select_stores_and_verifies(self, auth):
        respx.get(f"{API}/databases/db-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "db-1",
                    "title": [{"plain_text": "My Listings"}],
                    "properties": {
                        "Listing Title": {"id": "t", "type": "title", "title": {}},
                        "URL": {"id": "u", "type": "url", "url": {}},
                    },
                },
            )
        )
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        response = auth.post("/settings/notion/select", data={"db_id": "db-1"})

        # Reports precisely what is wrong...
        page = shown(response)
        assert "'Normalized URL' is missing — add it as a Text column." in page
        assert "'Listing ID' is missing — add it as a Text column." in page
        assert "cannot sync yet" in page
        assert "Add these before syncing can work" in page
        # ...and keeps the selection so the user can fix Notion and re-verify.
        assert app.state.settings.load().notion_db_id == "db-1"

    @respx.mock
    def test_a_hand_built_database_reads_as_a_warning_not_a_failure(self, auth):
        """The most likely real database anyone points at this.

        Nick's actual DB has the required four and text prices — so it syncs, and
        loses exactly the column the tool exists to sort by. Green would hide
        that; red would send them fixing a blocker they do not have.
        """
        text = lambda: {"id": "x", "type": "rich_text", "rich_text": {}}  # noqa: E731
        respx.get(f"{API}/databases/db-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "db-1",
                    "title": [{"plain_text": "Listings"}],
                    "properties": {
                        "Listing Title": {"id": "t", "type": "title", "title": {}},
                        "URL": {"id": "u", "type": "url", "url": {}},
                        "Normalized URL": text(),
                        "Listing ID": text(),
                        "Asking Price": text(),  # the hand-built reality
                        "Bot Triage": text(),    # a column we know nothing about
                    },
                },
            )
        )
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        response = auth.post("/settings/notion/select", data={"db_id": "db-1"})
        page = shown(response)

        assert '<div class="banner warn">' in response.text, "not a success, not a failure"
        assert "will sync, but these columns will be left empty" in page
        assert "Add these before syncing can work" not in page, "there is no blocker here"
        # The consequence, in their words — and the payoff for fixing it.
        assert "'Asking Price' is a Text column, but this app writes Number values." in page
        assert "sort and filter" in page
        assert "rich_text" not in page, "API type names mean nothing to the reader"
        # Their column is visible to us and explicitly out of bounds.
        assert "Bot Triage" in page and "left exactly as they are" in page

    @respx.mock
    def test_create_is_only_ever_explicit(self, auth):
        """Nothing in the app may create a database except this click."""
        created = respx.post(f"{API}/databases").mock(
            return_value=httpx.Response(200, json={"id": "db-new", "title": [{"plain_text": "L"}]})
        )
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})

        # Loading the page, saving a token, and listing must never create one.
        respx.post(f"{API}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "has_more": False})
        )
        auth.get("/")
        auth.post("/settings/notion", data={"action": "list", "notion_api_token": "ntn_x"})
        assert not created.called

        respx.get(f"{API}/databases/db-new").mock(
            return_value=httpx.Response(
                200, json={"id": "db-new", "title": [{"plain_text": "L"}], "properties": {}}
            )
        )
        auth.post("/settings/notion/create", data={"parent_page_id": "page-1", "title": "L"})
        assert created.called

    @respx.mock
    def test_create_does_not_claim_a_completeness_it_did_not_check(self, auth):
        """The same failure mode as a fabricated timezone, wearing a different hat.

        We build the schema from the same table we verify against, so a clean
        report is all but certain — which is exactly why hardcoding the happy
        sentence would never be caught. Assert the message follows the report.
        """
        respx.post(f"{API}/databases").mock(
            return_value=httpx.Response(200, json={"id": "db-new", "title": [{"plain_text": "L"}]})
        )
        respx.get(f"{API}/databases/db-new").mock(
            return_value=httpx.Response(
                200, json={"id": "db-new", "title": [{"plain_text": "L"}], "properties": {}}
            )
        )
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        response = auth.post("/settings/notion/create", data={"parent_page_id": "page-1"})
        page = shown(response)
        assert "has every column this app writes" not in page
        assert "'Listing ID' is missing" in page

    def test_create_without_a_parent_is_refused(self, auth):
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        response = auth.post("/settings/notion/create", data={"parent_page_id": ""})
        assert response.status_code == 400
        assert "Pick a page" in response.text

    @respx.mock
    def test_nothing_shared_is_explained_not_left_blank(self, auth):
        respx.post(f"{API}/search").mock(
            return_value=httpx.Response(200, json={"results": [], "has_more": False})
        )
        response = auth.post("/settings/notion", data={"action": "list", "notion_api_token": "ntn_x"})
        assert "share it with your integration" in response.text.lower()

    @respx.mock
    def test_bad_token_is_reported_readably(self, auth):
        respx.post(f"{API}/search").mock(
            return_value=httpx.Response(401, json={"message": "API token is invalid."})
        )
        response = auth.post("/settings/notion", data={"action": "list", "notion_api_token": "ntn_bad"})
        assert response.status_code == 400
        assert "rejected the API token" in response.text


class TestSecretRotation:
    NEW = "a-brand-new-secret-000001"

    def test_rotate_then_the_old_secret_stops_working(self, auth, client):
        response = auth.post(
            "/settings/secret", data={"current_secret": SECRET, "new_secret": self.NEW}
        )
        assert response.status_code == 200
        assert app.state.secret.verify(self.NEW)
        assert not app.state.secret.verify(SECRET)

    def test_rotating_keeps_the_rotating_user_signed_in(self, auth):
        auth.post("/settings/secret", data={"current_secret": SECRET, "new_secret": self.NEW})
        # The cookie was signed with the old secret; without a re-issue the user
        # would be thrown out of the page they are looking at.
        assert auth.get("/", follow_redirects=False).status_code == 200

    def test_rotating_signs_out_every_other_session(self, auth, tmp_path):
        other = TestClient(app, base_url="https://testserver")
        other.cookies.set(sessions.COOKIE_NAME, sessions.issue(SECRET))
        assert other.get("/", follow_redirects=False).status_code == 200

        auth.post("/settings/secret", data={"current_secret": SECRET, "new_secret": self.NEW})
        assert other.get("/", follow_redirects=False).status_code == 303

    def test_wrong_current_secret_refuses(self, auth):
        response = auth.post(
            "/settings/secret", data={"current_secret": "wrong", "new_secret": self.NEW}
        )
        assert response.status_code == 401
        assert app.state.secret.verify(SECRET), "a refused rotation must change nothing"

    def test_short_new_secret_refused(self, auth):
        response = auth.post("/settings/secret", data={"current_secret": SECRET, "new_secret": "abc"})
        assert response.status_code == 400
        assert "at least 16 characters" in response.text
        assert app.state.secret.verify(SECRET)

    def test_rotating_does_not_strand_the_settings(self, auth, tmp_path):
        """The property that makes rotation safe to offer at all."""
        auth.post("/settings/proxy", data={"proxy_user": "keepme", "proxy_password": "pw",
                                           "proxy_host": "h", "proxy_port": "1000"})
        auth.post("/settings/secret", data={"current_secret": SECRET, "new_secret": self.NEW})

        reopened = SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()
        assert reopened.proxy_user == "keepme"

    def test_the_recovery_path_is_documented_on_the_page(self, auth):
        page = auth.get("/").text
        assert "APP_SECRET_RESET" in page, "a forgotten secret must not be a dead end"
