"""The settings UI: login, session handling, and saving.

base_url is https so the client stores and re-sends the Secure session cookie —
over http it would be silently dropped, and every authenticated test would pass
for the wrong reason (or fail for a reason that never happens in production,
since Railway is HTTPS).
"""
from __future__ import annotations

import pathlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import CONFIG
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
    with TestClient(app, base_url="https://testserver") as c:
        # Repoint the services at a per-test volume; the module-level app is
        # shared, and one test's saved licence key must not be another's fixture.
        app.state.settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        app.state.secret = SecretService()
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
        assert "Set up this server here" in response.text
        assert "Everything this server needs" not in response.text

    def test_logout_clears_the_session(self, auth):
        auth.post("/logout")
        assert auth.get("/", follow_redirects=False).status_code == 303

    def test_a_forged_cookie_does_not_work(self, client):
        client.cookies.set(sessions.COOKIE_NAME, sessions.issue("some-other-secret-entirely"))
        assert client.get("/", follow_redirects=False).status_code == 303

    def test_the_recovery_path_is_on_the_login_page(self, client):
        """Where a locked-out person actually is.

        It used to live only on the settings page — behind this login — so the
        only person who could read it was the one who did not need it. And it is
        one line now, because recovery is one step: the secret is the Railway
        variable, so you read it off the Variables tab.
        """
        page = shown(client.get("/login"))
        assert "Forgotten it?" in page
        assert "Variables" in page

    def test_login_page_explains_an_unconfigured_deployment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APP_SECRET", raising=False)
        with TestClient(app, base_url="https://testserver") as c:
            app.state.secret = SecretService()
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
        assert "Pro key saved" in page.text

    def test_blank_secret_field_keeps_the_saved_value(self, auth):
        auth.post("/settings/cloakbrowser", data={"cloakbrowser_license_key": "cb_keepme"})
        # Saving the pin alone must not wipe a licence the user did not retype.
        auth.post("/settings/cloakbrowser",
                  data={"cloakbrowser_license_key": "", "cloakbrowser_version": "148.0.7778.215.5"})
        settings = app.state.settings.load()
        assert settings.cloakbrowser_license_key == "cb_keepme"
        assert settings.cloakbrowser_version == "148.0.7778.215.5"

    def test_public_action_explicitly_clears_a_saved_key(self, auth):
        auth.post("/settings/cloakbrowser", data={"cloakbrowser_license_key": "cb_remove"})
        response = auth.post(
            "/settings/cloakbrowser",
            data={"action": "public", "cloakbrowser_license_key": ""},
        )
        assert response.status_code == 200
        assert app.state.settings.load().cloakbrowser_license_key == ""
        page = shown(response)
        assert "Public build selected" in page
        assert "not been tested by us against the listing sites" in page

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

    def test_retired_claims_do_not_survive_in_template_SOURCE(self):
        """Every other test here reads rendered output, and a Jinja comment is
        not rendered. So a claim we retired can sit in the source forever, being
        read by the next person as the reason the code is the way it is, while
        the suite stays green — which is exactly what happened: the copy saying
        "Evomi accepts any password" was fixed and the comment asserting it was
        not, and it outlived the fix.

        Note the whitespace collapse. A grep for the phrase missed it because the
        comment wrapped it across a line break, so the words were never adjacent
        in the file. A guard that cannot see through wrapping is not a guard.
        """
        import re

        templates = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
        files = list(templates.glob("*.html"))
        assert files, "found no templates to scan — the guard would pass vacuously"
        retired = (
            # measured false: the check is skipped from a trusted address, not absent
            "accepts any password",
            "only rejects a wrong username",
            # Capacity cost/billing moved to the setup documentation. The
            # Settings accordion now explains only what the two controls do.
            "you only pay while a sweep is actually running",
            "it's cheap because it sleeps",
            "0.5–1 GB",
            "$10/GB per month",
            "every hour of the month",
            "costs pennies",
        )
        for path in files:
            src = re.sub(r"\s+", " ", path.read_text())
            for claim in retired:
                assert claim not in src, (
                    f"{path.name} still asserts a retired claim: {claim!r} — "
                    "check comments as well as copy"
                )

    def test_approved_settings_helpers_and_links_are_present(self, auth):
        page = shown(auth.get("/"))
        assert (
            "Your CloakBrowser key. It works without one — you'll get the public build, "
            "which gets past fewer bot detectors. A Pro key unlocks the private builds "
            "with more bypasses."
        ) in page
        assert 'href="https://cloakbrowser.dev/"' in page and "Get a key →" in page
        assert (
            "Use an Evomi proxy to get past IP and datacenter-IP detection — it makes your "
            "browser look like it's coming from a residential location. Optional, but "
            "websites like BizBuySell will block you if your IP comes from a non-residential "
            "location."
        ) in page
        assert 'href="https://evomi.com"' in page and "Get a proxy at Evomi →" in page
        assert "Choose the Residential product; Core Residential is a good place to start." in page
        assert "Country and region match the targeting options in your Evomi dashboard." in page
        assert (
            "Connect Notion so scraped listings can be saved into a database. (1) create an "
            "integration and copy its secret, (2) open the database or page you want it to "
            "use and share it with that integration — Notion blocks the key from touching "
            "anything you haven't shared."
        ) in page
        assert 'href="https://www.notion.so/my-integrations"' in page
        assert "Create an integration →" in page
        assert 'href="https://developers.notion.com/docs/create-a-notion-integration"' in page
        assert "How to share a page →" in page
        assert (
            "A profile is a saved browser identity — its cookies, logins, and location. "
            "Everything uses your Default profile unless you pick another, so you stay "
            "signed in across sessions."
        ) in page

    def test_capacity_copy_explains_the_controls_without_cost_copy(self, auth):
        page = shown(auth.get("/"))
        assert "Most browsers at once" in page
        assert "The total that can run at the same time." in page
        assert "Reserved for non-built-in tasks" in page
        assert (
            "Kept free so you or your assistant can drive a browser by hand, even while "
            "built-in tasks like sweeps are running."
        ) in page
        assert (
            "At 4 / 1: up to 4 browsers at once, 1 always free for you or your assistant "
            "to control directly, and 3 for built-in tasks."
        ) in page
        for retired in ("Serverless", "0.5–1 GB", "$10/GB per month", "costs pennies"):
            assert retired not in page

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

class TestLicenceVerify:
    def test_failure_is_reported_as_a_failure(self, auth, monkeypatch):
        from app.services import license as license_service
        from app.services.license import LicenseReport

        calls = []

        async def failed(key, pin=""):
            calls.append((key, pin))
            return LicenseReport(ok=False, message="Nope.")

        monkeypatch.setattr(license_service, "verify", failed)
        response = auth.post(
            "/settings/cloakbrowser",
            data={"action": "verify", "cloakbrowser_license_key": "cb_x"},
        )
        # A failed verification is not a successful page. 200 here would make
        # "did my licence work?" answerable only by reading the banner colour.
        assert response.status_code == 400
        assert calls == [("cb_x", "")], "action=verify must reach the verify service path"

    def test_no_key_verifies_as_public(self, auth, monkeypatch):
        from app.services import license as license_service
        from app.services.license import LicenseReport

        async def public(key, pin=""):
            assert key == ""
            return LicenseReport(
                ok=True,
                version="146.0.7680.177.3",
                message="CloakBrowser public build ready; fewer bypasses; not tested.",
                pro=False,
                binary_path="/cache/chromium-146.0.7680.177.3/chrome",
            )

        monkeypatch.setattr(license_service, "verify", public)
        response = auth.post("/settings/cloakbrowser", data={"action": "verify"})
        assert response.status_code == 200
        assert "public build ready" in shown(response)

    def test_public_mode_is_labelled_and_caveated(self, auth):
        page = shown(auth.get("/"))
        assert "Public build" in page
        assert "Without a key you're on the public build" in page
        assert "fewer bot detectors" in page
        assert "not tested" in page.lower() and "listing sites" in page.lower()

    def test_whitespace_key_renders_every_ui_status_as_public(self, auth):
        app.state.settings.update(cloakbrowser_license_key=" \t\r\n ")
        assert app.state.settings.load().cloakbrowser_license_key == ""
        page = shown(auth.get("/"))
        assert "Public build" in page
        assert "Without a key you're on the public build" in page
        assert "Pro key saved" not in page

    def test_verify_wait_state_names_the_measured_delay(self, auth):
        page = auth.get("/").text
        assert 'id="licence-form"' in page
        assert "Getting the browser — about ten seconds" in page
        assert "e.submitter" in page
        assert "if(!b||b.value!=='verify')return;" in page
        assert "a.name='action';a.value='verify'" in page
        assert "lf.querySelectorAll('button').forEach(function(x){x.disabled=true;});" in page


class TestProxyTest:
    def test_empty_proxy_is_presented_as_valid_direct_mode(self, auth):
        page = shown(auth.get("/"))
        assert '<span class="chip">Direct</span>' in auth.get("/").text
        assert "Direct mode is active" in page
        assert "Evomi Proxy" in page
        assert "Optional, but websites like BizBuySell will block you" in page
        assert "Without it, nothing launches" not in page

    def test_explicit_direct_action_clears_a_saved_proxy(self, auth):
        app.state.settings.update(
            proxy_user="u", proxy_password="secret", proxy_host="proxy.example",
            proxy_port="1000", proxy_last_check_ok=True,
            proxy_last_check_at=123.0, proxy_last_check_summary="working",
        )
        response = auth.post("/settings/proxy", data={"action": "direct"})
        assert response.status_code == 200
        settings = app.state.settings.load()
        assert (settings.proxy_user, settings.proxy_password,
                settings.proxy_host, settings.proxy_port) == ("", "", "", "")
        assert settings.proxy_status() == "direct"
        assert settings.proxy_last_check_ok is None
        assert "Direct connection selected" in shown(response)

    def test_partial_proxy_is_visible_and_never_labelled_direct(self, auth):
        response = auth.post(
            "/settings/proxy",
            data={"action": "save", "proxy_user": "u", "proxy_host": "proxy.example"},
        )
        assert response.status_code == 200
        assert app.state.settings.load().proxy_status() == "incomplete"
        page = shown(response)
        assert "Proxy settings are incomplete" in page
        assert '<span class="chip bad">Incomplete</span>' in response.text
        assert "Direct mode is active" not in page

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
        """A green result is not reliable evidence the credentials are right, and
        the page must not imply it is.

        The page used to assert the stronger, tidier claim — "this provider accepts
        any password and only rejects a wrong username" — as though it were a fact
        about the provider. It is a fact about the *address you ask from*: measured,
        the password check is skipped from a trusted address and enforced from a
        deployed one. The app cannot tell which it is, so it says "may not" and
        names the reason instead of over-claiming in either direction.
        """
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
        assert "may <strong>not</strong>" in page and "prove your password is right" in page
        # ...and it must not restate the over-claim it replaced
        assert "accepts any password" not in lowered

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
        assert "did not work when it was last tested" in page
        assert "Scrapes will fail until it does" in page
        assert '<span class="chip bad">Not working</span>' in auth.get("/").text
        # And the values they typed are still there to fix, not thrown away.
        assert app.state.settings.load().proxy_host == "192.0.2.1"

    @respx.mock
    def test_a_failed_retest_does_not_destroy_a_working_proxy(self, auth, monkeypatch):
        """The order bug: write-then-test replaced a routing proxy with a broken
        one on a single typo.

        A proxy is saved and proven to work. The user edits the form, mistypes
        the host, and clicks Save & test — which fails. The stored proxy must be
        the one that still works, not the typo: the test now runs *before* the
        write, so nothing is persisted when it fails against a working config.
        """
        from app.services import geo

        # 1. establish a working proxy
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(geo, "_geolocate", lambda ip: ("America/Los_Angeles", "en-US", "US", "San Jose"))
        auth.post("/settings/proxy", data={
            "action": "test", "proxy_user": "gooduser", "proxy_password": "goodpw",
            "proxy_host": "works.example.com", "proxy_port": "1000"})
        assert app.state.settings.load().proxy_host == "works.example.com"
        assert app.state.settings.load().proxy_last_check_ok is True

        # 2. mistype the host and re-test; it fails
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        r = auth.post("/settings/proxy", data={
            "action": "test", "proxy_user": "gooduser", "proxy_password": "",
            "proxy_host": "typo.invalid", "proxy_port": "1000"})
        assert r.status_code == 400
        assert "was kept unchanged" in r.text

        # 3. the working host is still there; the typo never landed
        after = app.state.settings.load()
        assert after.proxy_host == "works.example.com", "a typo overwrote a working proxy"
        assert after.proxy_last_check_ok is True, "the working verdict was downgraded"

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
        assert '<span class="chip ok">Working</span>' in auth.get("/").text
        assert "45.12.3.4" in page and "Last tested" in page

    def test_filling_the_form_in_is_not_evidence_of_anything(self, auth):
        auth.post(
            "/settings/proxy",
            data={"proxy_user": "u", "proxy_password": "pw", "proxy_host": "h.example.com",
                  "proxy_port": "1000"},
        )
        response = auth.get("/")
        page = shown(response)
        assert '<span class="chip warn">Not tested</span>' in response.text
        assert "Filling the form in does not prove the proxy routes" in page
        assert '<span class="chip ok">Working</span>' not in response.text

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
        assert '<span class="chip warn">Not tested</span>' in auth.get("/").text

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


# APP_SECRET is managed in Railway rather than this settings page. The
# forgotten-secret note lives on the login page, where a locked-out user can
# reach it.


class TestRunEvidenceIsReachableButNotPublic:
    """The evidence a sweep captures, and who may read it.

    Ported from browserd, which serves the same three routes with no auth at
    all. That was fine there — a private sidecar on one machine. Here the same
    files are screenshots of pages fetched through the user's residential proxy,
    on a public URL, so the port is the cookie gate.
    """

    def _job(self, auth, **fields):
        job = app.state.jobs.create(url="https://example.com/search", source="bizbuysell_serp", **fields)
        root = CONFIG.evidence_dir / job.id
        (root / "page-01-blocked").mkdir(parents=True, exist_ok=True)
        (root / "page-01-blocked" / "shot.png").write_bytes(b"\x89PNG-pretend")
        return job

    def test_a_signed_out_client_gets_nothing(self, client):
        """The whole finding, if it fails: a 200 without a session."""
        job = self._job(client)
        for path in (
            "/runs",
            f"/runs/{job.id}",
            f"/runs/{job.id}/evidence/page-01-blocked/shot.png",
        ):
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 200, f"{path} served a logged-out caller"
            assert b"PNG-pretend" not in r.content

    def test_a_signed_in_owner_can_read_the_screenshot(self, auth):
        """And the point of the port: the picture of the blocked page."""
        job = self._job(auth)
        r = auth.get(f"/runs/{job.id}/evidence/page-01-blocked/shot.png")
        assert r.status_code == 200
        assert r.content == b"\x89PNG-pretend"

    def test_a_run_lists_what_it_captured(self, auth):
        job = self._job(auth)
        r = auth.get(f"/runs/{job.id}")
        assert r.status_code == 200
        assert "page-01-blocked/shot.png" in r.json()["evidence"]

    def test_a_guessed_id_buys_nothing_without_a_session(self, client, auth):
        """Guessability must not be load-bearing.

        A job id is short hex; assume it is guessable. The defence is the
        session, not the id — so knowing a real id gets a signed-out caller
        exactly nowhere.
        """
        job = self._job(auth)
        signed_out = TestClient(app, base_url="https://testserver")
        r = signed_out.get(f"/runs/{job.id}/evidence/page-01-blocked/shot.png",
                           follow_redirects=False)
        assert r.status_code != 200
        assert b"PNG-pretend" not in r.content

    # Payloads split by whether they REACH the code under test. This split is the
    # test, not decoration: the HTTP client collapses a plain `../` to an absolute
    # path *before the request is sent*, so `../../.dek` arrives as `/runs/.dek`,
    # matches the get_run route, and dies on isalnum() — it never touches the
    # containment check and proves nothing about it. Only a form the client cannot
    # collapse (percent-encoded, or an absolute override) reaches get_evidence.
    #
    # Every payload below asserts *which handler answered it*, via the 404 detail
    # string — "no such evidence" comes only from the containment branch, "no such
    # run" only from get_run. So there are no silent passengers: a payload that
    # stopped reaching the code (a client that changed its normalisation, a
    # refactor that moved the check) flips its detail and fails here. Verified by
    # removing the containment check and watching the REACHES set serve HTTP 200
    # with the canary in the body.
    _REACHES_CONTAINMENT = (
        "..%2f..%2ftraversal-canary.txt",           # percent-encoded ../, at the canary
        "%2e%2e%2f%2e%2e%2ftraversal-canary.txt",   # dots encoded too
        "..%2f..%2f.dek",                           # the real prize: the data key
        "/etc/passwd",                              # absolute path overrides the join
    )
    _NORMALISED_BY_THE_CLIENT = (
        "../../.dek",                               # -> /runs/.dek before it is sent
        "../../traversal-canary.txt",
        "page-01-blocked/../../../traversal-canary.txt",
    )

    def test_traversal_cannot_reach_anything_outside_the_run(self, auth):
        """`{name:path}` takes slashes, and /data holds the keys to everything.

        Two directories above a run's evidence sit `settings.json` — the licence,
        proxy and Notion credentials — and the `.dek` that decrypts it. This is
        the one attack that turns a diagnostic route into a credential leak, so
        the canary is planted exactly where `.dek` lives: reaching it means
        reading the key.

        (A canary, not a real `.dek`: an earlier draft wrote the secret over the
        actual key file and broke every later test with a DecryptError. Tests
        that trample shared state are their own bug — see Step 3.)
        """
        job = self._job(auth)
        canary = CONFIG.evidence_dir.parent / "traversal-canary.txt"
        canary.write_text("CANARY-WHERE-THE-DEK-LIVES")
        try:
            for attempt in self._REACHES_CONTAINMENT:
                r = auth.get(f"/runs/{job.id}/evidence/{attempt}")
                assert r.status_code == 404, f"served something for {attempt}"
                assert b"CANARY" not in r.content, f"LEAKED via {attempt}"
                assert r.json()["detail"] == "no such evidence", (
                    f"{attempt} did NOT reach the containment check "
                    f"(got {r.json().get('detail')!r}) — this payload is now vacuous"
                )
        finally:
            canary.unlink(missing_ok=True)

    def test_the_readable_traversal_payloads_die_at_the_client(self, auth):
        """The other half of the split, pinned so nobody mistakes it for coverage.

        These never reach get_evidence — the client normalises them away first —
        so they cannot exercise containment no matter what containment does. The
        assertion is on the detail string precisely so that if a future client
        stops normalising them, this flips to "no such evidence", fails, and tells
        us the readable payloads are suddenly live and need real handling.
        """
        job = self._job(auth)
        for attempt in self._NORMALISED_BY_THE_CLIENT:
            r = auth.get(f"/runs/{job.id}/evidence/{attempt}")
            assert r.status_code == 404
            assert r.json()["detail"] == "no such run", (
                f"{attempt} now reaches our code — it is no longer just documentation"
            )

    def test_a_symlink_out_of_the_run_is_refused(self, auth):
        """resolve() follows links, so a link planted inside the evidence dir
        lands outside it and fails the containment check like any other escape."""
        job = self._job(auth)
        canary = CONFIG.evidence_dir.parent / "traversal-canary.txt"
        canary.write_text("CANARY-WHERE-THE-DEK-LIVES")
        try:
            link = CONFIG.evidence_dir / job.id / "innocent.png"
            link.symlink_to(canary)
            r = auth.get(f"/runs/{job.id}/evidence/innocent.png")
            assert r.status_code == 404
            assert b"CANARY" not in r.content
        finally:
            canary.unlink(missing_ok=True)

    def test_an_unknown_run_is_refused_before_the_filesystem(self, auth):
        assert auth.get("/runs/../settings").status_code == 404
        assert auth.get("/runs/not-a-real-job-id").status_code == 404


class TestSessionsControls:
    """The full-control actions on the dashboard: new instance, run sweep, close.

    They go through the one service layer, and they carry both CSRF layers — the
    SameSite=lax session cookie (via _require) and an Origin check (via
    _require_same_origin). These launch browsers and spend proxy money, so the
    guards matter; each is watched failing below.
    """

    def _stub_services(self, monkeypatch):
        """Neuter the real browser/sweep launch — this tests the route + guards,
        not CloakBrowser. Records the calls so the happy path can prove the
        service layer was actually reached."""
        calls = {"launch": 0, "start": 0, "stop": 0}

        async def fake_launch(req, **kw):
            calls["launch"] += 1
            return object()

        def fake_start(url, **kw):
            calls["start"] += 1
            return object()

        async def fake_stop(iid):
            calls["stop"] += 1
            return True

        monkeypatch.setattr(app.state.instances, "launch", fake_launch)
        monkeypatch.setattr(app.state.scrape, "start", fake_start)
        monkeypatch.setattr(app.state.instances, "stop", fake_stop)
        return calls

    # ── happy path: signed in, same origin → the service layer is reached ──
    def test_new_instance_reaches_the_service_layer(self, auth, monkeypatch):
        calls = self._stub_services(monkeypatch)
        r = auth.post("/sessions/instances", data={"profile": "p"},
                      follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?view=browsers"
        assert calls["launch"] == 1

    def test_run_sweep_reaches_the_service_layer(self, auth, monkeypatch):
        calls = self._stub_services(monkeypatch)
        r = auth.post("/sessions/sweep",
                      data={"url": "https://www.bizbuysell.com/x", "max_pages": "2"},
                      follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?view=tasks"
        assert calls["start"] == 1

    def test_close_instance_reaches_the_service_layer(self, auth, monkeypatch):
        calls = self._stub_services(monkeypatch)
        r = auth.post("/sessions/instances/abc123/close", follow_redirects=False)
        assert r.status_code == 303
        assert calls["stop"] == 1

    # ── the PRG target must be a real 200 page, not a 404 ──
    def test_a_successful_action_lands_on_the_dashboard_not_a_404(self, auth, monkeypatch):
        """A successful click redirected to /sessions, which is a 404 — the
        dashboard is the single page at /. A "it worked" that lands on a blank
        404 is a worse first impression than the error case, so follow the
        redirect the whole way and prove the page is real."""
        self._stub_services(monkeypatch)
        r = auth.post("/sessions/instances", data={"profile": "p"})  # follows the 303
        assert r.status_code == 200
        assert 'class="app"' in r.text, "landed on the real dashboard"
        assert 'data-section="browsers" class="on"' in r.text, "and on the Browsers section"

    def test_a_sweep_lands_on_the_tasks_section(self, auth, monkeypatch):
        self._stub_services(monkeypatch)
        r = auth.post("/sessions/sweep", data={"url": "https://x"})
        assert r.status_code == 200
        assert 'data-section="tasks" class="on"' in r.text

    # ── guard 1: no session → nothing happens ──
    def test_signed_out_cannot_launch_anything(self, client, monkeypatch):
        calls = self._stub_services(monkeypatch)
        for path, data in (
            ("/sessions/instances", {"profile": "p"}),
            ("/sessions/sweep", {"url": "https://x"}),
            ("/sessions/instances/abc/close", {}),
        ):
            r = client.post(path, data=data, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login"
        assert calls == {"launch": 0, "start": 0, "stop": 0}, "a signed-out call reached the service"

    # ── guard 2: foreign Origin → refused, even with a valid session ──
    def test_a_foreign_origin_is_refused(self, auth, monkeypatch):
        calls = self._stub_services(monkeypatch)
        for path, data in (
            ("/sessions/instances", {"profile": "p"}),
            ("/sessions/sweep", {"url": "https://x"}),
            ("/sessions/instances/abc/close", {}),
        ):
            r = auth.post(path, data=data, headers={"Origin": "https://evil.example"},
                          follow_redirects=False)
            assert r.status_code == 403, f"{path} allowed a cross-origin POST"
        assert calls == {"launch": 0, "start": 0, "stop": 0}, "a cross-origin call reached the service"

    # ── the absent-Origin policy the lead asked to pin: allowed ──
    def test_an_absent_origin_is_allowed(self, auth, monkeypatch):
        """SameSite=lax is the floor; a same-origin request that omits Origin (or a
        server-side caller) must not be blocked for a threat the cookie already
        stops."""
        calls = self._stub_services(monkeypatch)
        r = auth.post("/sessions/instances", data={"profile": "p"},
                      follow_redirects=False)  # TestClient sends no Origin
        assert r.status_code == 303 and calls["launch"] == 1

    # ── the same-origin case is allowed ──
    def test_our_own_origin_is_allowed(self, auth, monkeypatch):
        calls = self._stub_services(monkeypatch)
        r = auth.post("/sessions/sweep", data={"url": "https://x"},
                      headers={"Origin": "https://testserver"}, follow_redirects=False)
        assert r.status_code == 303 and calls["start"] == 1

    # ── guard 3: a state change must not be reachable by GET ──
    def test_state_changers_reject_GET(self, auth):
        for path in ("/sessions/instances", "/sessions/sweep",
                     "/sessions/instances/abc/close"):
            assert auth.get(path, follow_redirects=False).status_code == 405, (
                f"{path} answered a GET — SameSite=lax leaks the cookie on cross-site GET"
            )

    # ── the Origin check is UNIFORM: settings POSTs get it too ──
    def test_settings_posts_also_reject_a_foreign_origin(self, auth):
        r = auth.post("/settings/pool", data={"max_instances": "4", "interactive_reserve": "1"},
                      headers={"Origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403, "settings mutate credentials; they get the same Origin rule"

    def test_logout_rejects_a_foreign_origin(self, auth):
        assert auth.post("/logout", headers={"Origin": "https://evil.example"},
                         follow_redirects=False).status_code == 403


class TestNewBrowserLicenceErrors:
    """A present bad key must remain a visible UI/API error, never downgrade."""

    def _launch_raises(self, monkeypatch, exc):
        async def boom(req, **kw):
            raise exc

        monkeypatch.setattr(app.state.instances, "launch", boom)

    def test_an_unusable_key_also_gets_a_banner_400(self, auth, monkeypatch):
        """A mistyped or expired key is the very next first-boot moment, and it
        raises LicenseNotPro from the same launch path — also a banner 400."""
        from app.services.license import LicenseNotPro

        self._launch_raises(monkeypatch, LicenseNotPro(
            "CloakBrowser rejected this licence key. Check it was copied whole."
        ))
        r = auth.post("/sessions/instances", data={"profile": "p"}, follow_redirects=False)
        assert r.status_code == 400
        assert 'class="banner' in r.text and '{"detail"' not in r.text

    def test_the_rest_twin_keeps_the_bad_key_error_as_json(self, client, monkeypatch):
        from conftest import mint_access
        from app.services.license import LicenseNotPro

        self._launch_raises(
            monkeypatch,
            LicenseNotPro("Saved key was rejected; refusing a public downgrade."),
        )
        r = client.post(
            "/api/instances",
            json={"profile": "p"},
            headers={"Authorization": f"Bearer {mint_access(app)}"},
        )
        assert r.status_code == 400
        assert "public downgrade" in r.json()["detail"]

    def test_a_full_pool_keeps_its_own_status_under_the_banner(self, auth, monkeypatch):
        """The banner is presentation only: a non-licence failure still carries
        its own distinct code (429 here), which the Reviewer's tests rely on."""
        from app.services.instances import CapExceeded

        self._launch_raises(monkeypatch, CapExceeded("pool full (4); reserve in use"))
        r = auth.post("/sessions/instances", data={"profile": "p"}, follow_redirects=False)
        assert r.status_code == 429, "status is unchanged; only the rendering became a banner"
        assert 'class="banner' in r.text and "pool full" in shown(r)

    def test_a_sweep_error_banners_on_the_tasks_tab(self, auth, monkeypatch):
        from app.services.scrape import NotionNotConfigured

        def boom(url, **kw):
            raise NotionNotConfigured("Connect Notion in Settings before saving a sweep.")

        monkeypatch.setattr(app.state.scrape, "start", boom)
        r = auth.post("/sessions/sweep", data={"url": "https://x"}, follow_redirects=False)
        assert r.status_code == 409
        assert 'class="banner' in r.text and '{"detail"' not in r.text
        assert 'data-section="tasks" class="on"' in r.text


class TestLivePaneTokens:
    """The dashboard's live noVNC panes fetch their token here, at connect time,
    so no token that grants sight of the user's browser is baked into the page.
    A view token is the default; "Take control" is a separate POST-only
    escalation, refused outright for a sweep's browser.
    """

    def _stub(self, monkeypatch, *, origin="interactive", vnc_port=6100, subject=None):
        from types import SimpleNamespace

        inst = SimpleNamespace(id="inst1", origin=origin, vnc_port=vnc_port, subject=subject)
        monkeypatch.setattr(app.state.instances, "get",
                            lambda iid: inst if iid == "inst1" else None)
        return inst

    # ── the default: a fresh, view-only token ──
    def test_a_pane_gets_a_fresh_view_only_token(self, auth, monkeypatch):
        from app.services import tokens

        self._stub(monkeypatch)
        r = auth.get("/sessions/instances/inst1/vnc-token")
        assert r.status_code == 200
        token = r.json()["token"]
        assert tokens.verify(token, "inst1", SECRET, kind=tokens.VNC), "a real VNC token"
        assert not tokens.verify(token, "inst1", SECRET, kind=tokens.CDP), "never a driver"
        assert not tokens.grants_control(token, "inst1", SECRET), "view-only by default"

    def test_no_token_for_a_browser_without_a_live_view(self, auth, monkeypatch):
        self._stub(monkeypatch, vnc_port=None)
        assert auth.get("/sessions/instances/inst1/vnc-token").status_code == 404

    def test_no_token_for_an_unknown_instance(self, auth, monkeypatch):
        self._stub(monkeypatch)
        assert auth.get("/sessions/instances/nope/vnc-token").status_code == 404

    # ── the page must not carry a token itself ──
    def test_the_dashboard_html_carries_no_vnc_token(self, auth, monkeypatch):
        """The whole reason the pane fetches a token: none may sit in the markup,
        the DOM, or view-source. A running instance with a live view is exactly
        the case that would leak one."""
        from test_vnc import FakeInstance

        from app.services.views import instance_view

        async def _noop_stop(iid):
            return True

        inst = FakeInstance(iid="inst1", origin="interactive", vnc_port=6100)
        monkeypatch.setattr(app.state.instances, "running", {"inst1": inst})
        # The fake never really launched, so shutdown must not try to close it.
        monkeypatch.setattr(app.state.instances, "stop", _noop_stop)
        page = auth.get("/").text
        # The pane is rendered (so this is a real negative, not an empty page)…
        assert 'data-instance="inst1"' in page
        # …but the freshly-minted VNC token for it appears nowhere in the source.
        vnc_url = instance_view(inst, secret=SECRET, base_url="https://testserver").vnc_url
        token = vnc_url.split("t%3D")[1].split("&")[0]
        assert token not in page

    # ── guard: unauth mints nothing ──
    def test_signed_out_gets_no_token(self, client, monkeypatch):
        self._stub(monkeypatch)
        r = client.get("/sessions/instances/inst1/vnc-token", follow_redirects=False)
        assert r.status_code == 303 and "token" not in r.text
        p = client.post("/sessions/instances/inst1/control", follow_redirects=False)
        assert p.status_code == 303 and "token" not in p.text

    # ── guard: a foreign Origin mints nothing ──
    def test_a_foreign_origin_gets_no_token(self, auth, monkeypatch):
        self._stub(monkeypatch)
        r = auth.get("/sessions/instances/inst1/vnc-token",
                     headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        p = auth.post("/sessions/instances/inst1/control",
                      headers={"Origin": "https://evil.example"})
        assert p.status_code == 403

    # ── Take control: the escalation ──
    def test_take_control_mints_a_control_token(self, auth, monkeypatch):
        from app.services import tokens

        self._stub(monkeypatch, origin="interactive")
        r = auth.post("/sessions/instances/inst1/control")
        assert r.status_code == 200
        token = r.json()["token"]
        assert tokens.grants_control(token, "inst1", SECRET), "control was asked for"
        assert not tokens.verify(token, "inst1", SECRET, kind=tokens.CDP), "still not a driver token"

    def test_take_control_is_refused_for_a_sweeps_browser(self, auth, monkeypatch):
        """A sweep is mid-navigation; a click would corrupt it. Break the origin
        check in the endpoint and this is the test that falls."""
        self._stub(monkeypatch, origin="task")
        r = auth.post("/sessions/instances/inst1/control")
        assert r.status_code == 409
        assert "token" not in r.text

    def test_take_control_is_post_only(self, auth, monkeypatch):
        """A control grant behind GET would be reachable cross-site — SameSite=lax
        leaks the cookie on a top-level cross-site GET."""
        self._stub(monkeypatch)
        assert auth.get("/sessions/instances/inst1/control",
                        follow_redirects=False).status_code == 405

    def test_take_control_404s_for_an_unknown_instance(self, auth, monkeypatch):
        self._stub(monkeypatch)
        assert auth.post("/sessions/instances/nope/control").status_code == 404


class TestProfileEndpoints:
    """Settings → Profiles: cookie-authed, CSRF'd profile management. The server
    guards (Default undeletable, in-use blocked) are the real ones — the client
    confirm is UX only."""

    @pytest.fixture
    def profiles(self, monkeypatch, tmp_path):
        from app.services.profiles import ProfileStore
        ps = ProfileStore(tmp_path / "prof")
        monkeypatch.setattr(app.state.instances, "profiles", ps)
        monkeypatch.setattr(app.state.instances, "running", {})
        return ps

    def _names(self, ps):
        return {p.name for p in ps.all()}

    def _mk(self, ps, name):
        return ps.get_or_create(name, default_country="US", default_region="california")

    def _running(self, monkeypatch, name):
        # A full instance (renderable by instance_view — the 409 re-renders the
        # dashboard), with its profile set to the one under test.
        from test_vnc import FakeInstance

        async def _noop_stop(iid):
            return True

        inst = FakeInstance(iid="i1", origin="interactive", vnc_port=None)
        inst.profile = name
        monkeypatch.setattr(app.state.instances, "running", {"i1": inst})
        # The fake never really launched; shutdown must not try to close it.
        monkeypatch.setattr(app.state.instances, "stop", _noop_stop)

    def test_create(self, auth, profiles):
        r = auth.post("/settings/profiles/create", data={"name": "research"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?view=settings"
        assert "research" in self._names(profiles)

    def test_create_needs_a_name(self, auth, profiles):
        assert auth.post("/settings/profiles/create", data={"name": "  "},
                         follow_redirects=False).status_code == 400

    def test_rename_keeps_the_profile_under_the_new_name(self, auth, profiles):
        self._mk(profiles, "old")
        r = auth.post("/settings/profiles/rename", data={"name": "old", "new_name": "new"},
                      follow_redirects=False)
        assert r.status_code == 303 and self._names(profiles) == {"new"}

    def test_rename_is_blocked_while_a_browser_is_open(self, auth, profiles, monkeypatch):
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/rename", data={"name": "busy", "new_name": "x"},
                      follow_redirects=False)
        assert r.status_code == 409 and "busy" in self._names(profiles)

    def test_delete_removes_the_profile(self, auth, profiles):
        self._mk(profiles, "gone")
        r = auth.post("/settings/profiles/delete", data={"name": "gone"}, follow_redirects=False)
        assert r.status_code == 303 and "gone" not in self._names(profiles)

    def test_delete_default_is_refused(self, auth, profiles):
        profiles.ensure_default(default_country="US", default_region="california")
        r = auth.post("/settings/profiles/delete", data={"name": "Default"}, follow_redirects=False)
        assert r.status_code == 400 and "Default" in self._names(profiles)

    def test_delete_is_blocked_while_a_browser_is_open(self, auth, profiles, monkeypatch):
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/delete", data={"name": "busy"}, follow_redirects=False)
        assert r.status_code == 409 and "busy" in self._names(profiles)

    def test_rotate_changes_the_session_token(self, auth, profiles):
        tok = self._mk(profiles, "r").session_token
        r = auth.post("/settings/profiles/rotate", data={"name": "r"}, follow_redirects=False)
        assert r.status_code == 303
        assert {p.name: p for p in profiles.all()}["r"].session_token != tok

    def test_geo_updates_the_exit(self, auth, profiles):
        self._mk(profiles, "g")
        r = auth.post("/settings/profiles/geo",
                      data={"name": "g", "country": "GB", "region": "london"}, follow_redirects=False)
        assert r.status_code == 303
        assert {p.name: p for p in profiles.all()}["g"].country == "GB"

    def test_all_reject_a_foreign_origin(self, auth, profiles):
        self._mk(profiles, "p")
        for path, data in (
            ("/settings/profiles/create", {"name": "z"}),
            ("/settings/profiles/rename", {"name": "p", "new_name": "q"}),
            ("/settings/profiles/delete", {"name": "p"}),
            ("/settings/profiles/rotate", {"name": "p"}),
            ("/settings/profiles/geo", {"name": "p", "country": "GB"}),
        ):
            r = auth.post(path, data=data, headers={"Origin": "https://evil.example"},
                          follow_redirects=False)
            assert r.status_code == 403, f"{path} allowed a cross-origin POST"

    def test_signed_out_cannot_manage_profiles(self, client, profiles):
        r = client.post("/settings/profiles/create", data={"name": "z"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "z" not in self._names(profiles)

    def test_get_is_rejected(self, auth):
        assert auth.get("/settings/profiles/create", follow_redirects=False).status_code == 405


class TestNewBrowserProfile:
    """The reported confusion and its fix: '+ New browser' defaults to the ONE
    Default profile (not a throwaway), so opening twice reuses one identity."""

    def _capture_launch(self, monkeypatch):
        seen = {}

        async def fake_launch(req, **kw):
            seen["profile"] = req.profile
            return object()

        monkeypatch.setattr(app.state.instances, "launch", fake_launch)
        return seen

    def test_new_browser_defaults_to_the_default_profile(self, auth, monkeypatch):
        seen = self._capture_launch(monkeypatch)
        assert auth.post("/sessions/instances", data={}, follow_redirects=False).status_code == 303
        assert seen["profile"] == "Default"  # not session-<time>; twice -> same identity

    def test_new_browser_uses_a_picked_existing_profile(self, auth, monkeypatch):
        seen = self._capture_launch(monkeypatch)
        auth.post("/sessions/instances", data={"profile": "research"}, follow_redirects=False)
        assert seen["profile"] == "research"

    def test_new_browser_can_open_a_freshly_named_profile(self, auth, monkeypatch):
        seen = self._capture_launch(monkeypatch)
        auth.post("/sessions/instances",
                  data={"profile": "__new__", "new_profile": "research"}, follow_redirects=False)
        assert seen["profile"] == "research"  # the __new__ sentinel is resolved to the typed name

    def test_the_dashboard_offers_the_dialog_and_the_profiles_manager(self, auth, monkeypatch, tmp_path):
        from app.services.profiles import ProfileStore
        ps = ProfileStore(tmp_path / "prof")
        monkeypatch.setattr(app.state.instances, "profiles", ps)
        monkeypatch.setattr(app.state.instances, "running", {})
        ps.ensure_default(default_country="US", default_region="california")
        ps.get_or_create("research", default_country="US", default_region="california")
        page = auth.get("/").text
        assert 'id="nb-dialog"' in page                          # B: the New-browser dialog
        assert 'action="/settings/profiles/create"' in page      # C: the Profiles manager
        assert "research" in page and "Default" in page           # both profiles listed

    def test_direct_mode_does_not_claim_profile_geo_is_the_exit(self, auth, monkeypatch, tmp_path):
        from app.services.profiles import ProfileStore
        ps = ProfileStore(tmp_path / "prof")
        monkeypatch.setattr(app.state.instances, "profiles", ps)
        monkeypatch.setattr(app.state.instances, "running", {})
        ps.ensure_default(default_country="US", default_region="california")
        page = auth.get("/").text
        assert "Direct (no proxy)" in page
        assert "Rotate IP" not in page
        assert "Change a profile's exit location" not in page
