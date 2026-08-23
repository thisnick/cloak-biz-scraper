"""The settings UI: login, session handling, and saving.

base_url is https so the client stores and re-sends the Secure session cookie —
over http it would be silently dropped, and every authenticated test would pass
for the wrong reason (or fail for a reason that never happens in production,
since Railway is HTTPS).
"""
from __future__ import annotations

import inspect
import pathlib
import shutil
import types

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import CONFIG
from app.main import app
from app.services import sessions
from app.services.presentation import human_size
from app.services.scrape import ScrapeService
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


def _pin_capacity(monkeypatch, bytes_):
    """Pin the container's DETECTED memory ceiling so pool_warning() is
    deterministic instead of inheriting the host's.

    pool_warning() is resource-aware: it reads the real cgroup/proc limit and
    only falls back to the legacy cost-threshold ("… is a lot") when that limit
    is UNREADABLE. That makes any un-pinned assertion host-dependent — it passes
    on a developer's macOS (no cgroup, limit=None, legacy path) but flips on a
    Linux CI runner (cgroup readable, resource-aware path). Patch detect_capacity
    at its module so the test owns the regime: `bytes_=None` = unreadable (legacy
    path), a byte count = a readable ceiling (resource-aware path)."""
    import app.services.capacity as capacity

    monkeypatch.setattr(
        capacity, "detect_capacity",
        lambda **kw: capacity.Capacity(memory_limit_bytes=bytes_),
    )


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
            "/settings/notion/mapping",
            "/settings/connections/disconnect",
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
        before = auth.get("/").text
        assert "Clear licence key" in before
        assert "Use public build" not in before
        response = auth.post(
            "/settings/cloakbrowser",
            data={"action": "public", "cloakbrowser_license_key": ""},
        )
        assert response.status_code == 200
        assert app.state.settings.load().cloakbrowser_license_key == ""
        page = shown(response)
        assert "Licence key cleared" in page
        assert "Later launches will use the public build" in page
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
            "A saved browser identity — cookies, logins, and its own exit location."
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

    def test_pool_warns_above_eight_but_obeys(self, auth, monkeypatch):
        # Regime (a): memory ceiling UNREADABLE (as on a host without cgroups/
        # proc), so the legacy cost-threshold warning is the one that fires. Pin
        # it so this holds on any CI host, not only where the limit happens to be
        # unreadable — before pinning, a Linux runner (~16 GB readable) put 12
        # under the safe count and dropped the warning entirely.
        _pin_capacity(monkeypatch, None)
        response = auth.post("/settings/pool", data={"max_instances": "12", "interactive_reserve": "1"})
        assert response.status_code == 200
        assert app.state.settings.load().max_instances == 12, "guidance, not a cap"
        assert "is a lot" in response.text

    def test_pool_warns_when_it_exceeds_detected_memory(self, auth, monkeypatch):
        # Regime (b): the one that turned CI red. On a Linux runner the cgroup IS
        # readable, so the resource-aware path runs. Pin a small ceiling (4 GB ->
        # safe 4) and post a pool above it: it must warn with the measured
        # guidance (naming the safe count and a failure mode) and still obey.
        _pin_capacity(monkeypatch, 4 * 1024 ** 3)
        response = auth.post("/settings/pool", data={"max_instances": "12", "interactive_reserve": "1"})
        assert response.status_code == 200
        assert app.state.settings.load().max_instances == 12, "guidance, not a cap"
        page = shown(response)
        assert "sized for about 4 browser(s)" in page
        assert "Page crashed" in page
        assert "is a lot" not in page, "resource-aware warning, not the legacy cost one"

    def test_pool_is_silent_within_detected_memory(self, auth, monkeypatch):
        # The other half of regime (b), and exactly what the CI host was doing to
        # the legacy assertion: at or under the detected safe count there is no
        # nag, whichever warning path is live.
        _pin_capacity(monkeypatch, 4 * 1024 ** 3)  # safe 4
        response = auth.post("/settings/pool", data={"max_instances": "4", "interactive_reserve": "1"})
        assert response.status_code == 200
        assert app.state.settings.load().max_instances == 4
        page = shown(response)
        assert "is a lot" not in page
        assert "sized for" not in page

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
        assert "can't sync yet" in page
        assert "<b>Normalized URL</b> — add a Text column" in page
        assert "<b>Listing ID</b> — add a Text column" in page
        # ...and keeps the selection so the user can fix Notion and re-verify.
        assert app.state.settings.load().notion_db_id == "db-1"

    @respx.mock
    def test_a_hand_built_database_maps_its_columns_and_adapts_the_values(self, auth):
        """The most likely real database anyone points at this.

        Nick's actual DB has the required four and text prices. Under column
        mapping, selecting it defaults an identity map and the values ADAPT to the
        columns: a price in a Text column simply saves as text, which is the
        owner's call — no nag, no blocker. So it reads ready, the mapping table
        shows Asking Price landing in a Text column, and the column we know
        nothing about is left untouched.
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

        # Ready, not a warning and not a blocker: the text price adapts.
        assert "is ready" in page
        assert "can't sync yet" not in page, "there is no blocker here"
        assert "change it from Text to Number" not in page, "no nag — text money is fine"
        assert "rich_text" not in page, "API type names mean nothing to the reader"
        # The mapping table shows the field landing in the user's Text column.
        assert 'name="map_asking_price"' in page
        assert "Asking Price · Text" in page
        # A column we know nothing about is not mapped, so it is left untouched.
        map_ = app.state.settings.load().notion_column_map
        assert map_["asking_price"] == "Asking Price"
        assert "Bot Triage" not in map_.values()

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
        assert "every field will sync" not in page
        assert "<b>Listing ID</b> — add a Text column" in page

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


# A database whose columns are named nothing like the app's defaults.
_RENAMED_DB = {
    "id": "db-1",
    "title": [{"plain_text": "My Deals"}],
    "properties": {
        "Deal": {"id": "t", "type": "title", "title": {}},
        "Link": {"id": "u", "type": "url", "url": {}},
        "Canonical": {"id": "c", "type": "rich_text", "rich_text": {}},
        "Ref": {"id": "r", "type": "rich_text", "rich_text": {}},
        "Ask": {"id": "a", "type": "number", "number": {}},
        "Notes": {"id": "no", "type": "rich_text", "rich_text": {}},
    },
}


class TestNotionMapping:
    def _renamed(self):
        respx.get(f"{API}/databases/db-1").mock(
            return_value=httpx.Response(200, json=_RENAMED_DB)
        )

    @respx.mock
    def test_select_renders_the_mapping_table(self, auth):
        self._renamed()
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        page = shown(auth.post("/settings/notion/select", data={"db_id": "db-1"}))

        # A row per field, each with its own select of the user's columns.
        assert 'name="map_listing_title"' in page
        assert 'name="map_asking_price"' in page
        # Identity defaulting pre-selected the same-named columns; renamed ones
        # are offered in the dropdown for the user to choose.
        assert "Deal · Title" in page
        assert "Ask · Number" in page
        # None of these columns are named like the defaults, so identity matching
        # leaves the required fields unmapped (the user must pick from the table)
        # and defaults every optional to "don't sync".
        saved = app.state.settings.load().notion_column_map
        assert "listing_title" not in saved  # unmapped, awaiting the user's choice
        assert saved["asking_price"] is None
        assert saved["source"] is None

    @respx.mock
    def test_saving_a_mapping_stores_it_and_reverifies(self, auth):
        self._renamed()
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        auth.post("/settings/notion/select", data={"db_id": "db-1"})

        response = auth.post(
            "/settings/notion/mapping",
            data={
                "map_listing_title": "Deal",
                "map_url": "Link",
                "map_normalized_url": "Canonical",
                "map_listing_id": "Ref",
                "map_asking_price": "Ask",
                "map_revenue": "",  # don't sync
            },
        )
        saved = app.state.settings.load().notion_column_map
        assert saved["normalized_url"] == "Canonical"
        assert saved["listing_id"] == "Ref"
        assert saved["revenue"] is None
        # All required fields point at real columns, so it verifies clean.
        assert "is ready" in shown(response)

    @respx.mock
    def test_mapping_a_required_field_to_nothing_blocks(self, auth):
        self._renamed()
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        auth.post("/settings/notion/select", data={"db_id": "db-1"})

        response = auth.post(
            "/settings/notion/mapping",
            data={
                "map_listing_title": "Deal",
                "map_url": "Link",
                "map_normalized_url": "Canonical",
                "map_listing_id": "",  # required, left unset
            },
        )
        assert "can't sync yet" in shown(response)
        assert "listing_id" not in app.state.settings.load().notion_column_map

    @respx.mock
    def test_a_submitted_column_that_does_not_exist_is_ignored(self, auth):
        """A tampered or stale form must not make us believe in a column that is
        not in the database."""
        self._renamed()
        auth.post("/settings/notion", data={"notion_api_token": "ntn_x"})
        auth.post("/settings/notion/select", data={"db_id": "db-1"})

        auth.post(
            "/settings/notion/mapping",
            data={
                "map_listing_title": "Deal",
                "map_url": "Link",
                "map_normalized_url": "Canonical",
                "map_listing_id": "Ref",
                "map_asking_price": "Nonexistent Column",
            },
        )
        saved = app.state.settings.load().notion_column_map
        # Not a real column -> treated as "don't sync", never stored as a target.
        assert saved["asking_price"] is None


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


class TestRunResultsEndpoint:
    """`/runs/{job_id}/results` renders the full sweep result — the actual
    listings, not the count `/runs/{job_id}` exposes — behind the same cookie
    gate, so the Tasks-history "View" link can open it as raw JSON.
    """

    def _job_with_listings(self):
        from app.models import Listing

        return app.state.jobs.create(
            urls=["https://www.bizbuysell.com/x-businesses-for-sale/"],
            source="bizbuysell_serp", status="completed", pages_crawled=2,
            summary="1 of 1 source(s) swept · 1 listing(s) across 2 pages · Nothing was saved (sync=false).",
            listings=[Listing(listing_id="2453593", url="https://x/1", normalized_url="x/1",
                              title="Remodeling Contractor", asking_price="$965,000",
                              source="bizbuysell_serp")],
        )

    def test_it_returns_the_actual_listings_not_a_count(self, auth):
        job = self._job_with_listings()
        r = auth.get(f"/runs/{job.id}/results")
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"] == job.id
        assert body["status"] == "completed"
        # The whole point: the listings themselves, with their fields — /runs/{id}
        # returns only len(listings).
        assert isinstance(body["listings"], list) and len(body["listings"]) == 1
        assert body["listings"][0]["listing_id"] == "2453593"
        assert body["listings"][0]["asking_price"] == "$965,000"

    def test_it_matches_the_scraperesult_shape(self, auth):
        """It is the same ScrapeResult the tools return, built by ScrapeResult.of,
        so the same keys are present."""
        job = self._job_with_listings()
        body = auth.get(f"/runs/{job.id}/results").json()
        assert set(body) >= {
            "job_id", "status", "source", "summary", "pages_crawled",
            "error", "synced", "listings", "evidence_dir",
        }
        assert body["evidence_dir"].endswith(job.id)

    def test_an_unknown_job_is_404(self, auth):
        assert auth.get("/runs/deadbeef0000/results").status_code == 404

    def test_a_traversal_id_never_reaches_the_filesystem(self, auth):
        # jobs.get returns None for a non-alnum id, so "../settings" is a 404, not
        # a read off the volume.
        assert auth.get("/runs/../settings/results").status_code == 404
        assert auth.get("/runs/not-a-real-job/results").status_code == 404

    def test_signed_out_gets_nothing(self, client):
        """The cookie gate: a real job's results are not served to a logged-out
        caller (redirected to login), and no listing data leaks. The job is
        written directly (no HTTP), so the `client` here is genuinely signed out."""
        job = self._job_with_listings()
        r = client.get(f"/runs/{job.id}/results", follow_redirects=False)
        assert r.status_code != 200
        assert b"2453593" not in r.content

    def test_the_view_link_is_in_the_tasks_history(self, auth):
        """The dashboard offers the link, opening in a new tab."""
        job = self._job_with_listings()
        page = auth.get("/").text
        assert f'href="/runs/{job.id}/results"' in page
        assert 'target="_blank"' in page and 'rel="noopener"' in page

    def test_the_history_names_the_task_specifically(self, auth):
        """The Task column shows the specific label, not the bare word "Sweep"."""
        self._job_with_listings()
        page = auth.get("/").text
        assert "Listing sweep · BizBuySell" in page
        assert "<b>Sweep</b>" not in page, "the generic label is gone"


class TestArchiveTasksInTheDashboard:
    """`archive_page` records a task, so the Tasks tab has to be able to render
    one — and every reader of a task had been written when a task was only ever
    a sweep. Each of these renders an archive through a path that used to reach
    for `listings` or `pages_crawled` and would now raise on a record that has
    never had them.
    """

    URL = "https://www.bizbuysell.com/Business-Opportunity/a-laundromat/2274905/"

    @pytest.fixture(autouse=True)
    def _leave_the_store_as_it_was_found(self):
        """app.state.jobs is one real store shared by the whole suite, and this
        class writes a *working* task into it. Left behind, that is a task every
        later render shows as running — a fixture nobody asked for. So remove
        exactly what this test added (finishing it first, since `drop` rightly
        refuses a working record)."""
        before = {j.id for j in app.state.jobs.all()}
        yield
        for job in app.state.jobs.all():
            if job.id in before:
                continue
            if job.status == "working":
                job.status = "completed"
                app.state.jobs.save(job)
            app.state.jobs.drop(job.id)

    def _archive(self, **fields):
        return app.state.jobs.create(
            kind="archive", url=self.URL, notion_page_id="page-77", **fields
        )

    def _finished(self):
        return self._archive(
            status="completed", title="A Laundromat", blocks_appended=12,
            used_path="readability",
            summary="Archived 'A Laundromat' into Notion (12 blocks).",
        )

    def test_a_finished_archive_is_a_row_in_the_history(self, auth):
        self._finished()
        page = shown(auth.get("/"))
        assert "Archive · A Laundromat" in page, "the archive's own label"
        assert "12 blocks appended" in page, "an archive's result is blocks, not listings"

    def test_a_running_archive_is_under_running_now(self, auth):
        self._archive(summary="Archiving www.bizbuysell.com…")
        page = shown(auth.get("/"))
        # The row that used to read "page 0 / 1" off fields an archive has never
        # had; it names the page being read instead.
        assert "Archive · www.bizbuysell.com" in page
        assert self.URL in page
        assert "Nothing running right now" not in page

    def test_a_blocked_archive_reads_as_blocked(self, auth):
        self._archive(
            status="failed", summary="Blocked by the site; nothing written.",
            error="bizbuysell.com served an anti-bot page instead of the listing.",
        )
        assert "Blocked" in shown(auth.get("/"))

    def test_a_notion_refusal_is_not_dressed_up_as_a_block(self):
        """The two failures must not wear each other's badge. "Notion accepted 3
        block(s) and then refused the rest" contains the word "block" — under the
        sweep's test it would have sent the user off to rotate an exit IP over a
        problem in their Notion page. Asserted on the classifier itself, because
        on the page the two rows differ by one badge among many."""
        from app.routes.ui import _job_result

        refused = self._archive(
            status="failed", summary="Read the page, but could not write it to Notion.",
            error="Notion accepted 3 block(s) and then refused the rest.",
        )
        assert _job_result(refused) == ("asleep", "Stopped")

    def test_runs_lists_an_archive_without_pretending_it_has_listings(self, auth):
        task = self._finished()
        rows = {r["job_id"]: r for r in auth.get("/runs").json()}
        row = rows[task.id]
        assert row["kind"] == "archive"
        assert row["blocks_appended"] == 12 and row["notion_page_id"] == "page-77"
        # `"listings": 0` on an archive would read as a sweep that found nothing.
        assert "listings" not in row and "pages_crawled" not in row

    def test_one_run_reports_what_the_archive_did(self, auth):
        task = self._finished()
        body = auth.get(f"/runs/{task.id}").json()
        assert body["kind"] == "archive" and body["status"] == "completed"
        assert body["urls"] == [self.URL]
        assert body["title"] == "A Laundromat" and body["used_path"] == "readability"
        assert "listings" not in body

    def test_the_view_link_returns_the_archive_record_itself(self, auth):
        """Every history row offers "View"; an archive has no ScrapeResult, so it
        hands back its own record rather than a sweep-shaped answer."""
        task = self._finished()
        page = auth.get("/").text
        assert f'href="/runs/{task.id}/results"' in page

        body = auth.get(f"/runs/{task.id}/results").json()
        assert body["kind"] == "archive"
        assert body["notion_page_id"] == "page-77" and body["blocks_appended"] == 12
        assert "listings" not in body

    def test_the_app_files_archives_into_the_one_task_list(self, auth):
        """Wiring, not behaviour: the dashboard reads `app.state.jobs`, so an
        archive service holding any OTHER store would run, record diligently, and
        show up nowhere. One store is the whole feature."""
        assert app.state.archive._jobs is app.state.jobs

    def test_a_sweep_still_renders_exactly_as_it_did(self, auth):
        """The other half: narrowing the readers must not have changed the sweep."""
        from app.models import Listing

        job = app.state.jobs.create(
            url="https://www.bizbuysell.com/x-businesses-for-sale/", source="bizbuysell_serp",
            status="completed", pages_crawled=2,
            listings=[Listing(listing_id="1", title="A Business")],
        )
        page = shown(auth.get("/"))
        assert "Listing sweep · BizBuySell" in page
        assert "1 listings" in page
        row = {r["job_id"]: r for r in auth.get("/runs").json()}[job.id]
        assert row["kind"] == "sweep" and row["listings"] == 1 and row["pages_crawled"] == 2


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

        def fake_start(*args, **kwargs):
            # Bind against the REAL ScrapeService.start signature (self stubbed)
            # so a call the real method would reject — e.g. a removed kwarg like
            # db_id — raises here too, instead of a `**kw` catch-all swallowing
            # it and hiding a route/service signature drift behind a green test.
            inspect.signature(ScrapeService.start).bind(None, *args, **kwargs)
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

    def test_run_sweep_drives_the_real_start_without_500(self, auth, monkeypatch):
        """The route calls the REAL ScrapeService.start (not a stub), so a
        signature drift between the route and start() — like the removed db_id
        kwarg that made this route 500 — is caught end to end. Only the per-source
        browser work (_sweep) is neutered; start()'s validation, job creation and
        the real call binding all run."""
        async def fake_sweep(job, i, url, source, prog):
            return {"blocked": False, "error": None,
                    "data": {"listings": [], "pages_crawled": 1}}

        monkeypatch.setattr(app.state.scrape, "_sweep", fake_sweep)
        r = auth.post(
            "/sessions/sweep",
            data={"url": "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/?view=tasks"

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

    def test_duplicate_create_keeps_the_existing_ui_success_flow(self, auth, profiles):
        existing = self._mk(profiles, "research")
        token = existing.session_token
        r = auth.post(
            "/settings/profiles/create", data={"name": "research"}, follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/?view=settings"
        assert {p.name: p for p in profiles.all()}["research"].session_token == token

    def test_create_persists_the_region_the_dialog_offers(self, auth, profiles):
        """The dialog offers country *and* region; the route has always accepted
        both. It used to render only country, so a region was uneditable at
        birth and had to be set again straight afterwards."""
        r = auth.post(
            "/settings/profiles/create",
            data={"name": "research", "country": "GB", "region": "london"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        made = {p.name: p for p in profiles.all()}["research"]
        assert (made.country, made.region) == ("GB", "london")

    # ── Edit: one dialog per row, name and location together ──

    def test_edit_renames_and_moves_in_one_post(self, auth, profiles):
        self._mk(profiles, "old")
        r = auth.post(
            "/settings/profiles/edit",
            data={"name": "old", "new_name": "new", "country": "GB", "region": "london"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and self._names(profiles) == {"new"}
        moved = {p.name: p for p in profiles.all()}["new"]
        assert (moved.country, moved.region) == ("GB", "london")

    def test_edit_with_only_the_location_changed_keeps_the_name(self, auth, profiles):
        self._mk(profiles, "keep")
        r = auth.post(
            "/settings/profiles/edit",
            data={"name": "keep", "new_name": "keep", "country": "GB", "region": ""},
            follow_redirects=False,
        )
        assert r.status_code == 303 and self._names(profiles) == {"keep"}
        moved = {p.name: p for p in profiles.all()}["keep"]
        assert (moved.country, moved.region) == ("GB", "")

    def test_edit_that_changes_nothing_is_not_an_error(self, auth, profiles):
        """Default in direct mode sends only the profile it is editing — no
        rename (disabled), no geography (no proxy). Pressing Save on an
        unchanged form is not a failure, so it must not surface as one."""
        self._mk(profiles, "still")
        r = auth.post("/settings/profiles/edit", data={"name": "still"},
                      follow_redirects=False)
        assert r.status_code == 303 and self._names(profiles) == {"still"}

    def test_edit_is_blocked_while_a_browser_is_open(self, auth, profiles, monkeypatch):
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/edit", data={"name": "busy", "new_name": "x"},
                      follow_redirects=False)
        assert r.status_code == 409 and "busy" in self._names(profiles)

    def test_edit_moves_an_open_profile_without_renaming_it(self, auth, profiles, monkeypatch):
        """Geography is not identity: it applies on the next launch, so it does
        not need the browser closed — which is why the dialog keeps offering it
        when it has withdrawn the name box."""
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/edit",
                      data={"name": "busy", "country": "GB", "region": "london"},
                      follow_redirects=False)
        assert r.status_code == 303
        assert {p.name: p for p in profiles.all()}["busy"].country == "GB"

    def test_missing_edit_keeps_the_existing_bad_request_status(self, auth, profiles):
        r = auth.post(
            "/settings/profiles/edit",
            data={"name": "missing", "new_name": "new"},
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_edit_refuses_a_name_that_is_already_taken_and_says_so(self, auth, profiles):
        self._mk(profiles, "one")
        self._mk(profiles, "two")
        r = auth.post("/settings/profiles/edit", data={"name": "one", "new_name": "two"})
        assert r.status_code == 409
        assert "already exists" in shown(r)
        assert self._names(profiles) == {"one", "two"}

    def test_edit_refuses_an_emptied_name_rather_than_ignoring_it(self, auth, profiles):
        self._mk(profiles, "one")
        r = auth.post("/settings/profiles/edit", data={"name": "one", "new_name": "  "},
                      follow_redirects=False)
        assert r.status_code == 400 and self._names(profiles) == {"one"}

    def test_default_can_be_moved_but_not_renamed_by_a_crafted_form(self, auth, profiles):
        profiles.ensure_default(default_country="US", default_region="california")
        r = auth.post(
            "/settings/profiles/edit",
            data={"name": "Default", "country": "GB", "region": "london"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert {p.name: p for p in profiles.all()}["Default"].country == "GB"

        r = auth.post(
            "/settings/profiles/edit",
            data={"name": "Default", "new_name": "renamed"},
            follow_redirects=False,
        )
        assert r.status_code == 409 and "Default" in self._names(profiles)

    def test_edit_only_sends_arguments_the_real_service_accepts(self):
        """Bind the real signature. A UI stub that swallows **kwargs would let a
        renamed service argument reach production as a 500."""
        from app.services.profiles import ProfileService

        sig = inspect.signature(ProfileService.update_profile)
        for sent in (
            {"new_name": "n"},
            {"country": "GB", "region": "london"},
            {"new_name": "n", "country": "GB", "region": "london"},
        ):
            sig.bind(object(), "p", **sent)

    def test_delete_removes_the_profile(self, auth, profiles):
        self._mk(profiles, "gone")
        r = auth.post("/settings/profiles/delete", data={"name": "gone"}, follow_redirects=False)
        assert r.status_code == 303 and "gone" not in self._names(profiles)

    def test_delete_default_is_refused(self, auth, profiles):
        profiles.ensure_default(default_country="US", default_region="california")
        r = auth.post("/settings/profiles/delete", data={"name": "Default"}, follow_redirects=False)
        assert r.status_code == 400 and "Default" in self._names(profiles)

    def test_delete_missing_keeps_the_existing_idempotent_redirect(self, auth, profiles):
        r = auth.post(
            "/settings/profiles/delete", data={"name": "missing"}, follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/?view=settings"

    def test_delete_is_blocked_while_a_browser_is_open(self, auth, profiles, monkeypatch):
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/delete", data={"name": "busy"}, follow_redirects=False)
        assert r.status_code == 409 and "busy" in self._names(profiles)

    def _jar(self, profile) -> pathlib.Path:
        return pathlib.Path(profile.user_data_dir) / "Cookies"

    def test_clear_wipes_the_saved_data_but_keeps_the_profile(self, auth, profiles):
        p = self._mk(profiles, "research")
        self._jar(p).write_text("session=abc123")
        r = auth.post("/settings/profiles/clear", data={"name": "research"},
                      follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?view=settings"
        kept = {x.name: x for x in profiles.all()}["research"]
        assert kept.user_data_dir == p.user_data_dir and kept.session_token == p.session_token
        assert not self._jar(p).exists()
        assert pathlib.Path(kept.user_data_dir).is_dir()

    def test_clear_works_on_the_default_profile(self, auth, profiles):
        """The gap this closes: Default cannot be deleted, so before Clear its
        cookies could never be reset from the UI."""
        d = profiles.ensure_default(default_country="US", default_region="california")
        self._jar(d).write_text("session=abc123")
        r = auth.post("/settings/profiles/clear", data={"name": "Default"},
                      follow_redirects=False)
        assert r.status_code == 303
        assert "Default" in self._names(profiles)
        assert not self._jar(d).exists()

    def test_clear_is_blocked_while_a_browser_is_open(self, auth, profiles, monkeypatch):
        p = self._mk(profiles, "busy")
        self._jar(p).write_text("session=abc123")
        self._running(monkeypatch, "busy")
        r = auth.post("/settings/profiles/clear", data={"name": "busy"},
                      follow_redirects=False)
        assert r.status_code == 409
        assert self._jar(p).read_text() == "session=abc123"  # nothing was wiped

    def test_clear_of_a_missing_profile_is_a_404(self, auth, profiles):
        r = auth.post("/settings/profiles/clear", data={"name": "missing"},
                      follow_redirects=False)
        assert r.status_code == 404

    def test_clear_rejects_a_get(self, auth, profiles):
        self._mk(profiles, "research")
        assert auth.get("/settings/profiles/clear",
                        follow_redirects=False).status_code == 405

    def test_signed_out_cannot_clear(self, client, profiles):
        p = self._mk(profiles, "research")
        self._jar(p).write_text("session=abc123")
        r = client.post("/settings/profiles/clear", data={"name": "research"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert self._jar(p).read_text() == "session=abc123"

    def test_the_page_offers_clear_for_every_profile_including_default(self, auth, profiles):
        profiles.ensure_default(default_country="US", default_region="california")
        self._mk(profiles, "research")
        body = shown(auth.get("/?view=settings"))
        assert body.count('action="/settings/profiles/clear"') == 2   # Default included
        assert body.count('action="/settings/profiles/delete"') == 1  # still not Default
        assert "Clear the “" in body                                  # confirm dialog

    def test_a_profile_name_cannot_break_out_of_the_confirm_dialog(self, auth, profiles):
        """A profile name is untrusted input even here: create_profile is an MCP
        tool, so an agent reading a hostile page can choose the name that lands
        in the owner's own settings page."""
        import html as _html
        import re

        self._mk(profiles, "x');alert(1);//")
        raw = auth.get("/?view=settings").text
        # The script the browser actually runs: attribute entities are decoded
        # before the JS is parsed, which is exactly where attribute escaping on
        # its own stops helping.
        scripts = [_html.unescape(m) for m in re.findall(r"onsubmit='([^']*)'", raw)]
        assert scripts, "no confirm dialogs rendered — the assertion below proves nothing"
        assert not [s for s in scripts if "');alert(1);//" in s]
        assert [s for s in scripts if "\\u0027);alert(1);//" in s]  # inert, inside a string

    # ── The rendered section: one dialog per row, none offering the impossible ──

    def _proxied(self):
        app.state.settings.update(
            proxy_user="u", proxy_password="p", proxy_host="proxy.example", proxy_port="1000",
        )

    def _dialogs(self, body: str) -> dict[str, str]:
        """Each row's Edit dialog, keyed by the profile it edits."""
        import re

        found = {}
        for block in re.findall(r'<dialog id="pedit-\d+".*?</dialog>', body, re.S):
            owner = re.search(r'name="name" value="([^"]*)"', block)
            assert owner, "an edit dialog does not say which profile it edits"
            found[owner.group(1)] = block
        return found

    def test_every_row_gets_an_edit_dialog_with_name_and_location_together(self, auth, profiles):
        self._proxied()
        self._mk(profiles, "research")
        body = shown(auth.get("/?view=settings"))
        assert 'data-dlg-open="pedit-1"' in body
        dialog = self._dialogs(body)["research"]
        assert 'action="/settings/profiles/edit"' in dialog
        assert 'name="new_name"' in dialog
        assert 'name="country"' in dialog and 'name="region"' in dialog

    def test_defaults_dialog_offers_its_location_but_no_rename(self, auth, profiles):
        """Default cannot be renamed — the store raises. So the dialog must not
        hand the user a box whose only outcome is that refusal."""
        self._proxied()
        profiles.ensure_default(default_country="US", default_region="california")
        dialog = self._dialogs(shown(auth.get("/?view=settings")))["Default"]
        assert 'name="new_name"' not in dialog
        assert "Default cannot be renamed." in dialog
        assert 'name="country"' in dialog and 'name="region"' in dialog

    def test_an_open_profiles_dialog_offers_its_location_but_no_rename(
        self, auth, profiles, monkeypatch
    ):
        self._proxied()
        self._mk(profiles, "busy")
        self._running(monkeypatch, "busy")
        dialog = self._dialogs(shown(auth.get("/?view=settings")))["busy"]
        assert 'name="new_name"' not in dialog
        assert "close it to rename it" in dialog
        assert 'name="country"' in dialog

    def test_default_in_direct_mode_gets_no_edit_button_at_all(self, auth, profiles):
        """No proxy, no rename: the dialog would be empty, so there is no button
        to open it."""
        profiles.ensure_default(default_country="", default_region="")
        body = shown(auth.get("/?view=settings"))
        assert self._dialogs(body) == {}
        assert "data-dlg-open=\"pedit-" not in body

    def test_new_profile_is_a_dialog_that_offers_country_and_region(self, auth, profiles):
        self._proxied()
        body = shown(auth.get("/?view=settings"))
        assert 'data-dlg-open="pnew-dialog"' in body
        dialog = body.split('<dialog id="pnew-dialog"', 1)[1].split("</dialog>", 1)[0]
        assert 'action="/settings/profiles/create"' in dialog
        assert 'name="name"' in dialog
        assert 'name="country"' in dialog and 'name="region"' in dialog

    def test_the_detached_bottom_forms_are_gone(self, auth, profiles):
        """Renaming and relocating used to be two forms at the foot of the
        section, each with its own profile picker, detached from the row they
        acted on. The row's own dialog replaced both."""
        self._proxied()
        self._mk(profiles, "research")
        body = shown(auth.get("/?view=settings"))
        assert 'action="/settings/profiles/rename"' not in body
        assert 'action="/settings/profiles/geo"' not in body
        assert "Rename a profile" not in body
        assert "Change a profile's exit location" not in body
        assert "Add a profile" not in body

    def test_the_section_says_a_profile_keeps_the_location_it_was_created_with(
        self, auth, profiles
    ):
        """The owner read the proxy's country as the one that applies, and it is
        only the default for new profiles. The page has to say so."""
        self._proxied()
        body = shown(auth.get("/?view=settings"))
        assert "Each profile keeps its own exit country and region" in body
        assert "apply only to profiles created afterward" in body

    def test_row_actions_are_named_icon_buttons_that_keep_their_description(self, auth, profiles):
        """The row actions are icon-only buttons. Each MUST carry a non-empty
        aria-label — a nameless icon button is unusable to a screen reader or by
        keyboard — and MUST keep its explanation in the hover title, which is
        where the description went when the text labels were dropped."""
        import re

        self._proxied()
        self._mk(profiles, "research")  # idle, non-Default → shows all four actions
        body = auth.get("/?view=settings").text

        # Every icon button anywhere on the page (row actions AND the topbar
        # hamburger/collapse) has an accessible name.
        for tag in re.findall(r'<button class="iconbtn[^"]*"[^>]*>', body):
            name = re.search(r'aria-label="([^"]+)"', tag)
            assert name and name.group(1).strip(), f"icon button without a name: {tag}"

        # The four actions are present as icon buttons and their description
        # survived the move into the hover title.
        for label, needle in (
            ("Edit", "change this profile"),
            ("New proxy session", "does not change a browser that is already open"),
            ("Clear", "erase cookies, logins and cache"),
            ("Delete", "remove this profile and its saved data"),
        ):
            tag = re.search(
                rf'<button class="iconbtn[^"]*"[^>]*aria-label="{re.escape(label)}"[^>]*>', body
            )
            assert tag, f"no icon button for {label!r}"
            assert 'title="' in tag.group(0) and needle in tag.group(0), (
                f"{label} lost its hover description"
            )

        # Delete stays the destructive one; the old text labels are gone.
        assert re.search(r'<button class="iconbtn danger"[^>]*aria-label="Delete"', body)
        for gone in (">Edit</button>", ">New proxy session</button>", ">Clear</button>", ">Delete</button>"):
            assert gone not in body

    def test_a_hostile_profile_name_cannot_break_out_of_the_edit_dialog(self, auth, profiles):
        """Names are user input — create_profile is an MCP tool — and the dialog
        puts one in a heading, a hidden value, and a text input's value."""
        import re

        hostile = '"><img src=x onerror=alert(1)>'
        self._proxied()
        self._mk(profiles, hostile)
        raw = auth.get("/?view=settings").text
        assert 'id="pedit-1"' in raw, "no edit dialog rendered — the rest proves nothing"
        assert hostile not in raw           # never lands verbatim
        assert "<img" not in raw            # …so no tag of its own is ever opened
        assert 'value="&#34;&gt;&lt;img src=x onerror=alert(1)&gt;"' in raw  # inert, in the box
        # Ids come from the row's position, never from the name.
        assert re.findall(r'<dialog id="(pedit-[^"]*)"', raw) == ["pedit-1"]

    def test_the_re_measure_button_is_gone_and_sizes_still_load_themselves(self, auth, profiles):
        self._mk(profiles, "research")
        body = shown(auth.get("/?view=settings"))
        assert "sizes-remeasure" not in body
        assert "Re-measure" not in body
        # The automatic after-paint fetch stays exactly as it was.
        assert 'data-size-for="research"' in body
        assert "'/settings/profiles/sizes'" in body
        assert "refresh=true" not in body

    def test_rotate_changes_the_session_token(self, auth, profiles):
        app.state.settings.update(
            proxy_user="u", proxy_password="p", proxy_host="proxy.example", proxy_port="1000",
        )
        tok = self._mk(profiles, "r").session_token
        r = auth.post("/settings/profiles/rotate", data={"name": "r"}, follow_redirects=False)
        assert r.status_code == 303
        assert {p.name: p for p in profiles.all()}["r"].session_token != tok

    def test_rotate_refuses_direct_mode_without_changing_the_token(self, auth, profiles):
        tok = self._mk(profiles, "r").session_token
        r = auth.post("/settings/profiles/rotate", data={"name": "r"})
        assert r.status_code == 409 and "direct mode" in r.text
        assert {p.name: p for p in profiles.all()}["r"].session_token == tok

    def test_rotate_refuses_partial_proxy_without_changing_the_token(self, auth, profiles):
        app.state.settings.update(proxy_user="only-one-field")
        tok = self._mk(profiles, "r").session_token
        r = auth.post("/settings/profiles/rotate", data={"name": "r"})
        assert r.status_code == 409 and "incomplete" in r.text
        assert {p.name: p for p in profiles.all()}["r"].session_token == tok

    def test_the_superseded_rename_and_geo_routes_are_gone(self, auth, profiles):
        """Edit replaced both, and the two detached bottom forms with them. A
        route left behind is a second way in that nothing exercises."""
        self._mk(profiles, "p")
        for path, data in (
            ("/settings/profiles/rename", {"name": "p", "new_name": "q"}),
            ("/settings/profiles/geo", {"name": "p", "country": "GB"}),
        ):
            assert auth.post(path, data=data, follow_redirects=False).status_code == 404

    def test_all_reject_a_foreign_origin(self, auth, profiles):
        self._mk(profiles, "p")
        for path, data in (
            ("/settings/profiles/create", {"name": "z"}),
            ("/settings/profiles/edit", {"name": "p", "new_name": "q"}),
            ("/settings/profiles/clear", {"name": "p"}),
            ("/settings/profiles/delete", {"name": "p"}),
            ("/settings/profiles/rotate", {"name": "p"}),
        ):
            r = auth.post(path, data=data, headers={"Origin": "https://evil.example"},
                          follow_redirects=False)
            assert r.status_code == 403, f"{path} allowed a cross-origin POST"
        assert self._names(profiles) == {"p"}

    def test_signed_out_cannot_manage_profiles(self, client, profiles):
        r = client.post("/settings/profiles/create", data={"name": "z"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "z" not in self._names(profiles)

    def test_signed_out_cannot_edit_a_profile(self, client, profiles):
        self._mk(profiles, "p")
        r = client.post("/settings/profiles/edit", data={"name": "p", "new_name": "q"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert self._names(profiles) == {"p"}

    def test_get_is_rejected(self, auth):
        assert auth.get("/settings/profiles/create", follow_redirects=False).status_code == 405
        assert auth.get("/settings/profiles/edit", follow_redirects=False).status_code == 405


class TestProfileSizesEndpoint:
    """Disk usage is fetched by the page after it paints — never measured
    during a render, and never able to stop the settings page rendering."""

    @pytest.fixture
    def profiles(self, monkeypatch, tmp_path):
        from app.services.profiles import ProfileStore
        ps = ProfileStore(tmp_path / "prof")
        monkeypatch.setattr(app.state.instances, "profiles", ps)
        monkeypatch.setattr(app.state.instances, "running", {})
        return ps

    def _mk(self, ps, name, *, size: int = 0):
        p = ps.get_or_create(name, default_country="US", default_region="california")
        if size:
            (pathlib.Path(p.user_data_dir) / "Cookies").write_bytes(b"x" * size)
        return p

    def _rows(self, response) -> dict:
        return {r["name"]: r for r in response.json()["profiles"]}

    def test_reports_bytes_files_and_freshness_per_profile(self, auth, profiles):
        self._mk(profiles, "research", size=40)
        self._mk(profiles, "empty")
        rows = self._rows(auth.get("/settings/profiles/sizes"))
        assert rows["research"]["bytes"] == 40 and rows["research"]["files"] == 1
        assert rows["empty"] == {
            "name": "empty", "bytes": 0, "files": 0,
            "measured_at": rows["empty"]["measured_at"], "age_sec": rows["empty"]["age_sec"],
        }
        assert rows["research"]["age_sec"] >= 0

    def test_the_settings_page_render_never_walks_a_profile(self, auth, profiles, monkeypatch):
        """The owner's hard constraint: a warm Chromium profile is thousands of
        files, and no page render may wait on counting them."""
        from app.services import profile_sizes

        self._mk(profiles, "research", size=40)
        walks = []
        monkeypatch.setattr(
            profile_sizes, "measure_dir", lambda p: walks.append(p) or (0, 0)
        )
        assert auth.get("/?view=settings").status_code == 200
        assert walks == []
        # …and the row is there waiting to be filled in.
        assert 'data-size-for="research"' in auth.get("/?view=settings").text

    def test_a_second_visit_serves_the_cache_and_refresh_re_walks(self, auth, profiles):
        p = self._mk(profiles, "research", size=40)
        first = self._rows(auth.get("/settings/profiles/sizes"))["research"]
        (pathlib.Path(p.user_data_dir) / "Cookies").write_bytes(b"x" * 400)

        cached = self._rows(auth.get("/settings/profiles/sizes"))["research"]
        assert cached["bytes"] == 40 and cached["measured_at"] == first["measured_at"]

        fresh = self._rows(auth.get("/settings/profiles/sizes?refresh=true"))["research"]
        assert fresh["bytes"] == 400

    def test_clearing_a_profile_invalidates_its_cached_size(self, auth, profiles):
        self._mk(profiles, "research", size=40)
        assert self._rows(auth.get("/settings/profiles/sizes"))["research"]["bytes"] == 40
        assert auth.post("/settings/profiles/clear", data={"name": "research"},
                         follow_redirects=False).status_code == 303
        assert self._rows(auth.get("/settings/profiles/sizes"))["research"]["bytes"] == 0

    def test_deleting_a_profile_drops_it_from_the_sizes(self, auth, profiles):
        self._mk(profiles, "gone", size=40)
        assert "gone" in self._rows(auth.get("/settings/profiles/sizes"))
        auth.post("/settings/profiles/delete", data={"name": "gone"})
        assert "gone" not in self._rows(auth.get("/settings/profiles/sizes"))

    def test_a_missing_user_data_dir_reports_zero_rather_than_failing(self, auth, profiles):
        p = self._mk(profiles, "research", size=40)
        shutil.rmtree(p.user_data_dir)
        rows = self._rows(auth.get("/settings/profiles/sizes"))
        assert rows["research"]["bytes"] == 0 and rows["research"]["files"] == 0

    def test_signed_out_is_not_served(self, client, profiles):
        self._mk(profiles, "research", size=40)
        r = client.get("/settings/profiles/sizes", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "research" not in r.text

    def test_the_settings_page_still_renders_when_measuring_is_broken(self, auth, profiles,
                                                                      monkeypatch):
        """The page carries the licence, proxy, and Notion config. A sizing
        failure degrades to a page with no sizes, never to no page."""
        from app.services import profile_sizes

        self._mk(profiles, "research", size=40)
        monkeypatch.setattr(
            profile_sizes, "measure_dir",
            lambda p: (_ for _ in ()).throw(OSError("disk is having a day")),
        )
        page = auth.get("/?view=settings")
        assert page.status_code == 200 and "Profiles" in page.text
        # The endpoint answers with no sizes rather than an error the page has
        # to handle; the placeholders simply stay.
        sizes = auth.get("/settings/profiles/sizes")
        assert sizes.status_code == 200 and sizes.json() == {"profiles": []}


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
        assert "New proxy session" not in page
        assert "Change a profile's exit location" not in page

    def test_proxy_mode_explains_that_a_new_session_applies_on_the_next_launch(
        self, auth, monkeypatch, tmp_path
    ):
        from app.services.profiles import ProfileStore

        ps = ProfileStore(tmp_path / "prof")
        monkeypatch.setattr(app.state.instances, "profiles", ps)
        monkeypatch.setattr(app.state.instances, "running", {})
        ps.ensure_default(default_country="US", default_region="california")
        app.state.settings.update(
            proxy_user="u", proxy_password="p", proxy_host="proxy.example", proxy_port="1000"
        )

        page = auth.get("/").text
        # The explanation now lives in the icon button's hover tooltip.
        assert "New proxy session" in page
        assert "keeps cookies and logins" in page
        assert "does not change a browser that is already open" in page


class TestConnectedAppsUi:
    """Settings → Connected apps: the cookie-authed owner's view of the OAuth
    clients, with a Disconnect that removes a registration. Mirrors every other
    settings mutation — POST-only, session cookie plus same-origin."""

    @pytest.fixture
    def wired(self, auth, tmp_path):
        """Point the shared app at a clean OAuth store for this test, keeping the
        signed-in secret so the session and any minted token still verify."""
        from app.services.oauth import OAuthProvider, OAuthStore

        app.state.oauth = OAuthProvider(
            OAuthStore(tmp_path / "oauth.json", tmp_path / ".dek"), app.state.secret
        )
        return auth

    def _register(self, client, name="ChatGPT", host="chatgpt.example"):
        r = client.post(
            "/register",
            json={"redirect_uris": [f"https://{host}/cb"], "client_name": name},
        )
        assert r.status_code == 201, r.text
        return r.json()

    def _connect(self, client, name="ChatGPT", host="chatgpt.example"):
        """Register AND mark authorized, so the app is genuinely connected and
        appears in the listing. The full OAuth HTTP flow is exercised in
        test_oauth; here we only need the resulting store state."""
        info = self._register(client, name=name, host=host)
        app.state.oauth._store.mark_authorized(info["client_id"])
        return info

    def test_empty_state_points_at_the_connect_tab(self, wired):
        page = wired.get("/").text
        assert "No apps connected yet — add one from the Connect tab." in page

    def test_a_connected_app_is_listed_by_name(self, wired):
        self._connect(wired, name="Claude Code")
        page = wired.get("/").text
        assert "Claude Code" in page
        assert "Disconnect" in page

    def test_a_merely_registered_app_is_not_listed(self, wired):
        """A DCR registration that never completed auth holds no access; it must
        not clutter the page as 'connected'."""
        self._register(wired, name="Abandoned")
        page = wired.get("/").text
        assert "Abandoned" not in page
        assert "No apps connected yet — add one from the Connect tab." in page

    def test_the_page_carries_the_revocation_note(self, wired):
        """The one-line honesty about what Disconnect does and does not do."""
        page = shown(wired.get("/"))
        assert "loses access within an hour" in page
        assert "To revoke everything immediately" in page
        assert "in Railway and redeploy" in page

    def test_disconnect_removes_the_client(self, wired):
        info = self._connect(wired, name="ChatGPT")
        assert "ChatGPT" in wired.get("/").text

        r = wired.post("/settings/connections/disconnect",
                       data={"client_id": info["client_id"]}, follow_redirects=False)
        assert r.status_code == 200, r.text
        assert app.state.oauth._store.get_client(info["client_id"]) is None
        assert "disconnected" in shown(r).lower()

    def test_disconnect_rejects_a_foreign_origin(self, wired):
        info = self._register(wired)
        r = wired.post("/settings/connections/disconnect",
                       data={"client_id": info["client_id"]},
                       headers={"Origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403, "a state change that revokes access gets the same Origin rule"
        assert app.state.oauth._store.get_client(info["client_id"]) is not None

    def test_the_rendered_page_never_shows_a_client_secret(self, wired):
        info = self._connect(wired)  # confidential client: gets a secret
        assert info.get("client_secret")
        assert info["client_secret"] not in wired.get("/").text


# A real JPEG header. Staging checks the first bytes, never the filename, so a
# fixture with the wrong ones is refused — which is the point.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60


def _no_node(what: str):
    """Skip on a contributor's machine; FAIL in CI.

    CI pins node with `actions/setup-node`, so a skip there cannot be a genuine
    absence — it means this guard has gone silent, which must not read as a
    pass. Locally node really may not be installed, and failing someone's test
    run over a JavaScript comparison they did not touch would cost more than the
    gap it closes. `CI` is set by GitHub Actions and by nothing local.
    """
    import os

    if os.environ.get("CI"):
        pytest.fail(f"node is missing in CI, so {what} did not run")
    pytest.skip(f"node is not installed; {what} cannot be checked")


def _boundary_bytes() -> list[int]:
    """Byte counts where two roundings are most likely to part company.

    COMPUTED, not typed. The first version of this test listed twelve values by
    hand and every one of them happened to be a case where the two
    implementations agreed — it passed while they disagreed on 1,521 of 6,139
    values, because half-to-even and half-up differ at exactly the boundaries a
    person picking round numbers never writes down. So the sample is generated
    from the shape of the disagreement instead: every tenth-of-a-unit below ten,
    and every half-unit at and above it, at each scale.
    """
    values = {0, 1, 512, 1023, 1024}
    for scale in (1024, 1024 ** 2, 1024 ** 3):
        # Below ten the JS rounds `n * 10`, so its boundaries sit at every
        # HUNDREDTH-and-a-half of a unit — 1.25 KB is one, and it is the case a
        # sample stepping in tenths walks straight past.
        for tenth in range(0, 100):
            values.add(round(scale * (tenth + 0.5) / 10))
            values.add(round(scale * tenth / 10))
        # At and above ten it rounds `n`, so the boundaries are half-units.
        for whole in (10, 11, 99, 100, 500, 512, 1023):
            values.add(scale * whole)
            values.add(round(scale * (whole + 0.5)))
    return sorted(values)


# The four the review measured, kept by name so the regression they represent
# is legible without running the generator in your head.
MEASURED_DISAGREEMENTS = (10752, 1280, 512512, 11010048)


class TestOneByteFormatter:
    """"How big is that" had three answers: a service's refusals, the settings
    banners, and the dashboard's own JavaScript. Two of those are Python and now
    share one function; the third cannot import it, so it is checked against it.
    """

    def test_the_two_python_callers_are_the_same_function(self):
        """Not a tautology: it is the assertion that fails the day somebody
        writes a second `human_size` next to a call site because importing felt
        like a detour."""
        from app.routes import ui
        from app.services import presentation, uploads

        assert ui.human_size is presentation.human_size
        assert uploads.human_size is presentation.human_size

    @staticmethod
    def _run_js(counts: list[int]) -> list[str]:
        """The dashboard's `human()`, extracted from the shipped template and
        run by node — the actual bytes that reach a browser, in one process for
        the whole sample rather than one process per value."""
        import json
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            _no_node("the JS/Python formatter guard")

        template = pathlib.Path("app/templates/index.html").read_text()
        source = re.search(r"function human\(n\)\{.*?\n    \}", template, re.S)
        assert source, "the dashboard's human() could not be found to test"

        script = (source.group(0) + "\nconst xs=JSON.parse(process.argv[1]);"
                  "process.stdout.write(JSON.stringify(xs.map(human)));")
        out = subprocess.run([node, "-e", script, json.dumps(counts)],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_dashboard_javascript_agrees_over_every_rounding_boundary(self):
        counts = _boundary_bytes()
        assert len(counts) > 100, "the sample generator produced almost nothing"

        rendered = self._run_js(counts)
        disagreements = [(n, js, human_size(n))
                         for n, js in zip(counts, rendered) if js != human_size(n)]

        assert not disagreements, (
            f"the dashboard and the server disagree about "
            f"{len(disagreements)} of {len(counts)} byte counts, e.g.\n"
            + "\n".join(f"  {n:>15,}  row(JS)={js!r}  banner(py)={py!r}"
                         for n, js, py in disagreements[:6])
        )

    @pytest.mark.parametrize("count", MEASURED_DISAGREEMENTS)
    def test_the_measured_disagreements_are_gone(self, count):
        """The four the review actually caught, named individually so a failure
        points at the specific regression rather than at a generated list."""
        assert self._run_js([count])[0] == human_size(count)

    def test_the_disk_space_total_degrades_one_row_at_a_time(self):
        """The chip over the accordion, run for real.

        Its three inputs fail independently — that is the whole design of the
        endpoint beneath it — so a chip that shows nothing when one of them
        failed throws away two measurements that worked. Checked by running the
        shipped function, because a chip is JavaScript and every other assertion
        about it would be about the source rather than the behaviour.
        """
        import json
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            _no_node("the Disk space total")

        template = pathlib.Path("app/templates/index.html").read_text()
        human = re.search(r"function human\(n\)\{.*?\n    \}", template, re.S)
        total = re.search(r"function usedTotal\(sizes\)\{.*?\n    \}", template, re.S)
        assert human and total, "the dashboard's usedTotal() could not be found"

        cases = [[1024, 2048, 4096], [1024, None, 4096], [None, None, 4096],
                 [None, None, None]]
        script = (human.group(0) + total.group(0) +
                  "\nconst xs=JSON.parse(process.argv[1]);"
                  "process.stdout.write(JSON.stringify(xs.map(usedTotal)));")
        out = json.loads(subprocess.run([node, "-e", script, json.dumps(cases)],
                                        capture_output=True, text=True,
                                        check=True).stdout)

        assert out[0] == f"{human_size(1024 + 2048 + 4096)} used"
        assert out[1].startswith(f"{human_size(1024 + 4096)} used"), out[1]
        assert "could not be measured" in out[1]
        assert out[2].startswith(f"{human_size(4096)} used"), out[2]
        assert out[3] == "", "nothing measured should show nothing, not '0 B used'"

    def test_the_generated_sample_really_covers_the_measured_ones(self):
        """A control on the generator: if it stopped producing boundary values,
        the sweep above would pass on a sample that proves nothing."""
        assert set(MEASURED_DISAGREEMENTS) <= set(_boundary_bytes())


class TestStorage:
    """Disk space: the three things that grow on the volume forever.

    The numbers are shown where the buttons are, so both are tested together —
    a measurement that fails must still leave a settings page that can configure
    a licence, and a button must never remove anything the service would refuse
    to remove.
    """

    @pytest.fixture
    def storage(self, monkeypatch, tmp_path):
        """Point both reclaim services at this test's own volume."""
        from app.services.binaries import BrowserBuilds
        from app.services.history import TaskHistory
        from app.services.jobs import JobStore

        cache = tmp_path / "cloakbrowser-cache"
        cache.mkdir()
        jobs = JobStore(tmp_path / "jobs", boot_id="boot-1",
                        evidence_root=tmp_path / "evidence")
        monkeypatch.setattr(app.state, "jobs", jobs)
        monkeypatch.setattr(app.state.instances, "running", {})
        monkeypatch.setattr(
            app.state, "browser_builds",
            BrowserBuilds(cache, lambda: app.state.settings, app.state.instances),
        )
        monkeypatch.setattr(app.state, "task_history", TaskHistory(lambda: app.state.jobs))

        from app.services.uploads import StagedUploads, UploadService

        uploads = UploadService(tmp_path / "uploads")
        monkeypatch.setattr(app.state, "uploads", uploads)
        monkeypatch.setattr(app.state, "staged_uploads",
                            StagedUploads(lambda: app.state.uploads))
        return types.SimpleNamespace(cache=cache, jobs=jobs, uploads=uploads)

    def _ticket(self, storage, *, files=()):
        """A real staged ticket, through the real store — the size this row
        reports is only meaningful if the bytes got there the way real ones do."""
        import asyncio

        async def build():
            ticket = await storage.uploads.mint(subject="owner", secret=SECRET)
            for name, payload in files:
                async def one(data=payload):
                    yield data
                await storage.uploads.stage(ticket.handle, subject="owner",
                                            filename=name, stream=one())
            return ticket

        return asyncio.run(build())

    def _expire(self, storage, ticket):
        """Age a ticket, AFTER every ticket a test needs has been minted.

        Order matters and it bit this file once: minting sweeps, so a ticket
        expired before a later `_ticket()` call is swept away by that call and
        the test measures a volume with one fewer ticket on it than it thinks.
        """
        import json
        import time

        record = storage.uploads.root / ticket.handle / ".ticket.json"
        manifest = json.loads(record.read_text())
        manifest["expires"] = time.time() - 1
        record.write_text(json.dumps(manifest))
        return ticket

    @staticmethod
    def _on_disk(root) -> int:
        """What the volume is actually holding. Every size assertion below is
        derived from this rather than from a number written into the test — a
        literal compared against another literal proves only that someone typed
        the same thing twice."""
        import os

        total = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
        return total

    def _build(self, storage, name, *, size=100, in_use=False):
        directory = storage.cache / name
        directory.mkdir(parents=True)
        (directory / "chrome").write_bytes(b"x" * size)
        if in_use:
            app.state.settings.update(binary_last_path=str(directory / "chrome"))
        return directory

    def _run(self, storage, *, status="completed", size=100):
        job = storage.jobs.create(source="bizbuysell_serp", url="https://x/y/")
        job.status = status
        storage.jobs.save(job)
        evidence = storage.jobs.evidence_root / job.id / "source-01"
        evidence.mkdir(parents=True)
        (evidence / "page.png").write_bytes(b"x" * size)
        return job

    # ── the measurement endpoint ─────────────────────────────────────────────

    def test_reports_what_the_browser_cache_and_the_history_are_using(self, auth, storage):
        self._build(storage, "chromium-148.0.7778.215.5-pro", size=700, in_use=True)
        self._build(storage, "chromium-146.0.7680.177.3", size=300)
        self._run(storage, size=250)

        body = auth.get("/settings/storage").json()

        assert body["builds"]["in_use"]["version"] == "148.0.7778.215.5"
        assert body["builds"]["in_use"]["pro"] is True
        assert body["builds"]["in_use"]["bytes"] == 700
        assert body["builds"]["stale_count"] == 1 and body["builds"]["stale_bytes"] == 300
        assert body["builds"]["total_bytes"] == 1000
        assert body["history"] == {
            "runs": 1, "bytes": 250, "files": 1, "orphans": 0,
            "measured_at": body["history"]["measured_at"],
        }

    def test_with_no_recorded_build_it_still_reports_sizes_and_says_why(self, auth, storage):
        self._build(storage, "chromium-148.0.7778.215.5-pro", size=700)

        builds = auth.get("/settings/storage").json()["builds"]

        assert builds["in_use_known"] is False and builds["stale_count"] == 0
        assert builds["total_bytes"] == 700, "a full volume is always explainable"
        assert "Save & verify" in builds["reason"]

    def test_orphaned_evidence_is_counted(self, auth, storage):
        (storage.jobs.evidence_root / "deadbeef1234" / "final").mkdir(parents=True)
        (storage.jobs.evidence_root / "deadbeef1234" / "final" / "p.png").write_bytes(b"x" * 900)

        history = auth.get("/settings/storage").json()["history"]

        assert history["runs"] == 0 and history["orphans"] == 1 and history["bytes"] == 900

    def test_a_second_visit_serves_the_cache_and_refresh_re_walks(self, auth, storage):
        directory = self._build(storage, "chromium-148.0.7778.215.5-pro", size=100, in_use=True)
        assert auth.get("/settings/storage").json()["builds"]["total_bytes"] == 100

        (directory / "extra").write_bytes(b"x" * 400)

        assert auth.get("/settings/storage").json()["builds"]["total_bytes"] == 100
        fresh = auth.get("/settings/storage?refresh=true").json()
        assert fresh["builds"]["total_bytes"] == 500

    def test_signed_out_is_not_served(self, client, storage):
        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        r = client.get("/settings/storage", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "chromium" not in r.text

    def test_the_page_still_renders_when_measuring_is_broken(self, auth, storage,
                                                             monkeypatch):
        """The settings page carries the licence, proxy, and Notion config. A
        volume that cannot be walked costs a number, never the page."""
        from app.services import binaries, history as history_module

        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        self._run(storage)
        self._ticket(storage, files=[("photo.jpg", JPEG)])
        from app.services import uploads as uploads_module

        boom = lambda p: (_ for _ in ()).throw(OSError("disk is having a day"))
        monkeypatch.setattr(binaries, "measure_dir", boom)
        monkeypatch.setattr(history_module, "measure_dir", boom)
        monkeypatch.setattr(uploads_module, "measure_dir", boom)

        page = auth.get("/?view=settings")
        assert page.status_code == 200 and "Disk space" in page.text

        body = auth.get("/settings/storage")
        assert body.status_code == 200
        assert body.json() == {"builds": None, "history": None, "uploads": None}

    def test_the_page_renders_over_a_hostile_volume(self, auth, storage, tmp_path):
        """Dangling links, an unreadable directory, and missing roots: the page
        never walks any of it during a render, and the endpoint degrades to
        zeroes rather than an error."""
        import os
        import shutil

        (storage.cache / "chromium-broken").symlink_to(tmp_path / "never-existed",
                                                       target_is_directory=True)
        storage.jobs.evidence_root.mkdir(parents=True, exist_ok=True)
        locked = storage.jobs.evidence_root / "locked"
        locked.mkdir()
        (locked / "p.png").write_bytes(b"x" * 10)
        if os.geteuid() != 0:
            locked.chmod(0o000)
        shutil.rmtree(storage.jobs.root)  # the jobs directory itself is gone

        try:
            assert auth.get("/?view=settings").status_code == 200
            body = auth.get("/settings/storage")
            assert body.status_code == 200
            assert body.json()["builds"]["total_bytes"] == 0
            assert body.json()["history"]["runs"] == 0
            assert body.json()["uploads"]["handles"] == 0
        finally:
            locked.chmod(0o700)

    # ── the page ─────────────────────────────────────────────────────────────

    def test_the_section_asks_for_its_sizes_after_it_paints(self, auth, storage):
        body = auth.get("/?view=settings").text
        assert "'/settings/storage'" in body
        assert 'data-storage="build-inuse"' in body and 'data-storage="history"' in body
        # …and the render itself measured nothing: no size is in the HTML.
        assert "Checking what's on the disk" in shown(auth.get("/?view=settings"))

    def test_the_remove_button_is_absent_until_a_build_is_recorded(self, auth, storage):
        page = shown(auth.get("/?view=settings"))
        assert "Remove old versions" not in page
        assert "has not recorded which browser it is running" in page

        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)

        page = shown(auth.get("/?view=settings"))
        assert "Remove old versions" in page
        assert "/settings/storage/builds/prune" in page

    def test_both_destructive_buttons_confirm_first(self, auth, storage):
        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        page = shown(auth.get("/?view=settings"))
        assert "Remove the older browser versions?" in page
        assert "Clear the task history?" in page

    # ── the actions ──────────────────────────────────────────────────────────

    def test_removing_old_builds_keeps_the_one_in_use(self, auth, storage):
        keep = self._build(storage, "chromium-148.0.7778.215.5-pro", size=700, in_use=True)
        old = self._build(storage, "chromium-146.0.7680.177.3", size=300)

        r = auth.post("/settings/storage/builds/prune", follow_redirects=False)

        assert r.status_code == 200, r.text
        assert "Removed 1 older browser version" in shown(r)
        assert not old.exists() and keep.is_dir()

    def test_removing_old_builds_refuses_when_none_is_recorded(self, auth, storage):
        old = self._build(storage, "chromium-146.0.7680.177.3")

        r = auth.post("/settings/storage/builds/prune", follow_redirects=False)

        assert r.status_code == 409
        assert "Save & verify" in shown(r)
        assert old.is_dir(), "nothing may be removed when the running build is unknown"

    def test_removing_old_builds_refuses_while_a_browser_is_open(self, auth, storage,
                                                                 monkeypatch):
        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        old = self._build(storage, "chromium-146.0.7680.177.3")
        # Reserved but not yet running: the launch window a scan of `running`
        # cannot see, and the one a live Chromium's mapped binary sits in.
        monkeypatch.setattr(app.state.instances, "_profiles_opening", {"Default": 1})

        r = auth.post("/settings/storage/builds/prune", follow_redirects=False)

        assert r.status_code == 409 and "Close every browser" in shown(r)
        assert old.is_dir()

    def test_clearing_history_removes_finished_runs_and_their_evidence(self, auth, storage):
        done = self._run(storage, size=300)
        working = self._run(storage, status="working", size=100)

        r = auth.post("/settings/storage/history/clear", follow_redirects=False)

        assert r.status_code == 200, r.text
        assert "Cleared 1 run" in shown(r)
        assert storage.jobs.get(done.id) is None
        assert not (storage.jobs.evidence_root / done.id).exists()
        assert storage.jobs.get(working.id) is not None
        assert (storage.jobs.evidence_root / working.id).is_dir()

    def test_clearing_history_removes_orphaned_evidence(self, auth, storage):
        orphan = storage.jobs.evidence_root / "deadbeef1234"
        orphan.mkdir(parents=True)
        (orphan / "p.png").write_bytes(b"x" * 900)

        r = auth.post("/settings/storage/history/clear", follow_redirects=False)

        assert r.status_code == 200 and "leftover evidence folder" in shown(r)
        assert not orphan.exists()

    def test_the_routes_call_the_services_they_claim_to(self, auth, storage, monkeypatch):
        """Bind against the REAL service signatures (self stubbed) so a route
        that grows an argument the service does not take fails here instead of
        being swallowed by a **kwargs stub."""
        from app.services.binaries import BrowserBuilds, RemovedBuilds
        from app.services.history import ClearedHistory, TaskHistory

        called = []

        async def fake_remove_stale(*args, **kwargs):
            inspect.signature(BrowserBuilds.remove_stale).bind(None, *args, **kwargs)
            called.append("builds")
            return RemovedBuilds(removed=["chromium-146.0.7680.177.3"], bytes=300,
                                 kept="chromium-148.0.7778.215.5-pro")

        async def fake_clear(*args, **kwargs):
            inspect.signature(TaskHistory.clear).bind(None, *args, **kwargs)
            called.append("history")
            return ClearedHistory(runs=2, orphans=1, bytes=500, kept=1)

        monkeypatch.setattr(app.state.browser_builds, "remove_stale", fake_remove_stale)
        monkeypatch.setattr(app.state.task_history, "clear", fake_clear)

        prune = shown(auth.post("/settings/storage/builds/prune", follow_redirects=False))
        clear = shown(auth.post("/settings/storage/history/clear", follow_redirects=False))

        assert called == ["builds", "history"]
        assert "freed 300 B" in prune and "chromium-148.0.7778.215.5-pro) was kept" in prune
        assert "Cleared 2 runs" in clear and "1 leftover evidence folder" in clear
        assert "1 run still working was kept" in clear

    # ── uploaded files ───────────────────────────────────────────────────────

    def test_uploads_are_reported_alongside_the_other_two(self, auth, storage):
        self._ticket(storage, files=[("a.jpg", JPEG), ("b.jpg", JPEG + b"\x01")])

        uploads = auth.get("/settings/storage").json()["uploads"]

        assert uploads["handles"] == 1 and uploads["files"] == 2
        assert uploads["expired"] == 0

    def test_the_size_it_reports_is_what_is_actually_on_the_disk(self, auth, storage):
        """Derived from a walk of the volume, not compared against a number
        typed into this test. The row exists to explain what the disk is holding,
        so the two had better be the same thing."""
        self._ticket(storage, files=[("a.jpg", JPEG + b"\x02" * 500)])

        reported = auth.get("/settings/storage").json()["uploads"]["bytes"]

        assert reported == self._on_disk(storage.uploads.root)

    def test_the_size_includes_an_upload_still_arriving(self, auth, storage):
        """The accounting walk deliberately EXCLUDES in-flight temp files, so it
        does not charge them twice against their reservation. The display must
        not inherit that: a user asking what is on the disk wants what is on the
        disk, and a number that silently omits a part-written file understates
        the very thing they came to look at."""
        import asyncio

        ticket = self._ticket(storage)
        write = asyncio.run(storage.uploads.begin(
            ticket.handle, subject="owner", filename="big.jpg"))
        try:
            write.feed(JPEG + b"\x03" * 4096)
            reported = auth.get("/settings/storage").json()["uploads"]["bytes"]
            assert reported == self._on_disk(storage.uploads.root)
            assert reported > 4096, "the part-written file was left out"
        finally:
            write.abort()

    def test_expired_uploads_that_survive_the_sweep_are_shown_not_hidden(
        self, auth, storage
    ):
        """TaskHistory's honesty about orphans, applied here: bytes whose ticket
        is dead are still bytes on the volume.

        Reaching that state now takes a ticket the sweep will not take, because
        this endpoint sweeps on the way in — which is the point of the third
        sweep site, and it makes a non-zero `expired` MORE informative than it
        used to be: it no longer means "nobody has cleaned up yet", it means
        "something is expired and could not be removed".
        """
        import asyncio

        stale = self._ticket(storage, files=[("a.jpg", JPEG)])
        self._ticket(storage, files=[("b.jpg", JPEG + b"\x04")])
        self._expire(storage, stale)
        held = asyncio.run(storage.uploads.begin(stale.handle, subject="owner",
                                                 filename="c.jpg", now=1.0))
        try:
            uploads = auth.get("/settings/storage").json()["uploads"]

            assert uploads["handles"] == 2 and uploads["expired"] == 1
            assert uploads["bytes"] == self._on_disk(storage.uploads.root)
        finally:
            held.abort()

    def test_an_expired_upload_is_swept_by_the_page_that_reports_it(self, auth,
                                                                    storage):
        """The third sweep site. Railway forbids a timer, so a sweep happens
        when somebody asks — and the moment a human is looking at the number is
        the moment it had better be true."""
        stale = self._ticket(storage, files=[("a.jpg", JPEG + b"\x0f" * 700)])
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x10")])
        self._expire(storage, stale)

        uploads = auth.get("/settings/storage").json()["uploads"]

        assert not (storage.uploads.root / stale.handle).exists()
        assert (storage.uploads.root / live.handle).is_dir()
        assert uploads["handles"] == 1 and uploads["expired"] == 0
        assert uploads["bytes"] == self._on_disk(storage.uploads.root), (
            "the number was measured before the sweep it triggered"
        )

    def test_one_broken_measurement_does_not_cost_the_other_two(self, auth, storage,
                                                                monkeypatch):
        """Each third degrades on its own — the property the existing tuple
        already had, now with a third member that could break it."""
        from app.services import uploads as uploads_module

        self._build(storage, "chromium-148.0.7778.215.5-pro", size=700, in_use=True)
        self._run(storage, size=250)
        self._ticket(storage, files=[("a.jpg", JPEG)])
        monkeypatch.setattr(
            uploads_module, "measure_dir",
            lambda p: (_ for _ in ()).throw(OSError("the uploads volume is having a day")),
        )

        body = auth.get("/settings/storage").json()

        assert body["uploads"] is None
        assert body["builds"]["total_bytes"] == 700 and body["history"]["runs"] == 1

    def test_the_row_asks_for_its_size_after_the_page_paints(self, auth, storage):
        self._ticket(storage, files=[("a.jpg", JPEG + b"\x05" * 900)])
        page = shown(auth.get("/?view=settings"))

        assert 'data-storage="uploads"' in page
        assert "Uploaded files" in page
        # …and the render measured nothing: no size for it is in the HTML.
        assert human_size(self._on_disk(storage.uploads.root)) not in page

    def test_the_uploads_help_points_at_a_button_that_exists(self, auth, storage):
        """Copy is reviewed by a person, not a test — but one property is worth
        pinning, because it is the one the last version got wrong: the help must
        tell the reader what to DO, and the thing it names must be on the page.

        The expected label is read off the rendered button rather than typed, so
        this cannot pass by two literals agreeing, and a sentence that explains
        a cause instead of naming an action fails it.
        """
        import re

        page = shown(auth.get("/?view=settings"))
        block = re.search(r"<label>Uploaded files</label>(.*?)</div>\s*</div>",
                          page, re.S).group(1)
        help_text = re.search(r'<div class="help">(.*?)</div>', block, re.S).group(1)
        buttons = re.findall(r'<button[^>]*>([^<]+)</button>', block)

        assert buttons, "the row rendered no buttons to point at"
        assert any(label.strip() in help_text for label in buttons), (
            "the help text names no button on this row, so a reader who still "
            f"sees expired uploads is not told what to do. Buttons: {buttons}"
        )

    def test_both_upload_buttons_confirm_first_and_say_different_things(self, auth,
                                                                        storage):
        """Two scopes, two warnings. The full clear can take a file an assistant
        is part-way through using, so it must not share a sentence with the safe
        one — a confirmation that does not distinguish them is not a
        confirmation."""
        import re

        page = shown(auth.get("/?view=settings"))
        forms = [m for m in re.findall(r"<form[^>]*/settings/storage/uploads/clear.*?</form>",
                                       page, re.S)]
        assert len(forms) == 2, "expected a scoped form per clear"
        assert all("confirm(" in form for form in forms)
        warnings = [re.search(r"confirm\('([^']*)'", form).group(1) for form in forms]
        assert warnings[0] != warnings[1]
        assert sorted(re.search(r'name="scope" value="(\w+)"', f).group(1)
                      for f in forms) == ["all", "expired"]

    def test_clearing_expired_uploads_keeps_the_live_ones(self, auth, storage):
        dead = self._ticket(storage, files=[("a.jpg", JPEG + b"\x06" * 400)])
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x07" * 400)])
        self._expire(storage, dead)
        before = self._on_disk(storage.uploads.root)

        r = auth.post("/settings/storage/uploads/clear", data={"scope": "expired"},
                      follow_redirects=False)

        assert r.status_code == 200, r.text
        assert not (storage.uploads.root / dead.handle).exists()
        assert (storage.uploads.root / live.handle).is_dir()
        freed = before - self._on_disk(storage.uploads.root)
        assert human_size(freed) in shown(r), (
            "the banner must report what actually left the disk"
        )

    def test_clearing_everything_takes_the_live_ones_too(self, auth, storage):
        dead = self._ticket(storage, files=[("a.jpg", JPEG + b"\x08")])
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x09")])
        self._expire(storage, dead)
        before = self._on_disk(storage.uploads.root)

        r = auth.post("/settings/storage/uploads/clear", data={"scope": "all"},
                      follow_redirects=False)

        assert r.status_code == 200, r.text
        assert not (storage.uploads.root / dead.handle).exists()
        assert not (storage.uploads.root / live.handle).exists()
        freed = before - self._on_disk(storage.uploads.root)
        assert human_size(freed) in shown(r)
        assert "Cleared 2 uploads" in shown(r)

    def test_two_clears_at_once_do_not_both_claim_the_whole_amount(self, storage):
        """`SweptUploads` documents itself as reporting what a clear ACTUALLY
        removed, never what it set out to remove. Two clears walking the same
        volume, each taking part of it and each reporting the total, is that
        promise failing rather than two banners overlapping."""
        import asyncio

        for n in range(4):
            self._ticket(storage, files=[(f"p{n}.jpg", JPEG + bytes([n]) * 900)])
        for entry in storage.uploads.root.iterdir():
            self._expire(storage, types.SimpleNamespace(handle=entry.name))
        before = self._on_disk(storage.uploads.root)

        async def both():
            return await asyncio.gather(storage.uploads.clear(),
                                        storage.uploads.clear())

        first, second = asyncio.run(both())

        actually_freed = before - self._on_disk(storage.uploads.root)
        assert first.bytes + second.bytes == actually_freed, (
            f"two clears reported {first.bytes} + {second.bytes} = "
            f"{first.bytes + second.bytes} bytes freed; {actually_freed} left the disk"
        )
        assert first.handles + second.handles == 4

    def test_an_upload_in_flight_is_never_cleared_by_either_scope(self, auth, storage):
        """Reservations live in memory and the files they cover live on disk, so
        the two could disagree about what is there. They must not: removing a
        ticket with a write streaming into it pulls the directory out from under
        an open file handle, and on POSIX the writer only finds out at the
        rename — a 500 for something that is nobody's mistake."""
        import asyncio

        ticket = self._ticket(storage)
        write = asyncio.run(storage.uploads.begin(
            ticket.handle, subject="owner", filename="big.jpg"))
        try:
            write.feed(JPEG + b"\x0a" * 2048)

            for scope in ("expired", "all"):
                r = auth.post("/settings/storage/uploads/clear", data={"scope": scope},
                              follow_redirects=False)
                assert r.status_code == 200, r.text
                assert (storage.uploads.root / ticket.handle).is_dir(), scope
                assert "still in use" in shown(r), scope
        finally:
            write.abort()

        # …and once it finishes, the same button takes it.
        r = auth.post("/settings/storage/uploads/clear", data={"scope": "all"},
                      follow_redirects=False)
        assert not (storage.uploads.root / ticket.handle).exists()

    def test_clearing_nothing_is_reported_as_success(self, auth, storage):
        """A status and a sentence say nothing about what happened. The state
        before and after is what makes this more than a smoke test."""
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x11")])
        before = self._on_disk(storage.uploads.root)

        r = auth.post("/settings/storage/uploads/clear", follow_redirects=False)

        assert r.status_code == 200
        assert "Nothing to clear" in shown(r)
        assert (storage.uploads.root / live.handle).is_dir(), "it cleared something"
        assert self._on_disk(storage.uploads.root) == before
        assert "freed" not in shown(r), "it claimed to have freed something"

    def test_the_default_scope_is_the_safe_one(self, auth, storage):
        """A POST with no scope at all — a client that forgot the field, or a
        form that lost it — must not become a full clear.

        The first version of this discarded the response, so a 500 would have
        passed it: "the live ticket survived" is also true of a request that
        never ran. It needs a positive half — the expired one really was taken —
        or it only proves the endpoint did nothing.
        """
        stale = self._ticket(storage, files=[("a.jpg", JPEG + b"\x12")])
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x0b")])
        self._expire(storage, stale)

        r = auth.post("/settings/storage/uploads/clear", follow_redirects=False)

        assert r.status_code == 200, r.text
        assert "Cleared 1 upload" in shown(r)
        assert not (storage.uploads.root / stale.handle).exists(), "it cleared nothing"
        assert (storage.uploads.root / live.handle).is_dir(), "it cleared everything"

    def test_clearing_uploads_carries_both_csrf_layers(self, client, storage):
        live = self._ticket(storage, files=[("b.jpg", JPEG + b"\x0c")])

        signed_out = client.post("/settings/storage/uploads/clear",
                                 data={"scope": "all"}, follow_redirects=False)
        assert signed_out.status_code == 303
        assert signed_out.headers["location"] == "/login"

        client.post("/login", data={"secret": SECRET})
        foreign = client.post("/settings/storage/uploads/clear", data={"scope": "all"},
                              headers={"Origin": "https://evil.example"},
                              follow_redirects=False)
        assert foreign.status_code == 403
        assert (storage.uploads.root / live.handle).is_dir(), "a cross-site POST cleared it"

    def test_there_is_no_get_form_of_the_clear(self, auth, storage):
        """SameSite=lax carries the session on a top-level cross-site GET, so a
        state change behind GET would be reachable from any page."""
        assert auth.get("/settings/storage/uploads/clear").status_code == 405

    def test_the_clear_calls_the_service_it_claims_to(self, auth, storage, monkeypatch):
        """Bound against the REAL signature, so a route that grows an argument
        the service does not take fails here rather than being swallowed."""
        from app.services.uploads import SweptUploads, UploadService

        called = []

        async def fake_clear(*args, **kwargs):
            inspect.signature(UploadService.clear).bind(None, *args, **kwargs)
            called.append(kwargs.get("expired_only"))
            return SweptUploads(handles=2, files=3, bytes=4096, kept=1, refused=0)

        monkeypatch.setattr(app.state.uploads, "clear", fake_clear)

        expired = shown(auth.post("/settings/storage/uploads/clear",
                                  data={"scope": "expired"}, follow_redirects=False))
        everything = shown(auth.post("/settings/storage/uploads/clear",
                                     data={"scope": "all"}, follow_redirects=False))

        assert called == [True, False]
        assert human_size(4096) in expired and "Cleared 2 uploads" in expired
        assert "1 upload still in use was kept" in everything

    def test_a_clear_invalidates_the_cached_measurement(self, auth, storage):
        """The number on the page after a clear must not be the number from
        before it — that is a cache reporting a volume that no longer exists."""
        self._ticket(storage, files=[("a.jpg", JPEG + b"\x0d" * 700)])
        assert auth.get("/settings/storage").json()["uploads"]["handles"] == 1

        auth.post("/settings/storage/uploads/clear", data={"scope": "all"},
                  follow_redirects=False)

        after = auth.get("/settings/storage").json()["uploads"]
        assert after["handles"] == 0 and after["bytes"] == 0

    def test_nothing_to_do_is_reported_as_success(self, auth, storage):
        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        assert "Nothing to remove" in shown(
            auth.post("/settings/storage/builds/prune", follow_redirects=False))
        assert "no saved task history" in shown(
            auth.post("/settings/storage/history/clear", follow_redirects=False))

    # ── the gates ────────────────────────────────────────────────────────────

    def test_both_actions_are_post_only(self, auth, storage):
        assert auth.get("/settings/storage/builds/prune",
                        follow_redirects=False).status_code == 405
        assert auth.get("/settings/storage/history/clear",
                        follow_redirects=False).status_code == 405

    def test_both_actions_reject_a_foreign_origin(self, auth, storage):
        keep = self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        old = self._build(storage, "chromium-146.0.7680.177.3")
        job = self._run(storage)

        for path in ("/settings/storage/builds/prune", "/settings/storage/history/clear"):
            r = auth.post(path, headers={"Origin": "https://evil.example"},
                          follow_redirects=False)
            assert r.status_code == 403, f"{path} allowed a cross-origin POST"

        assert old.is_dir() and keep.is_dir()
        assert storage.jobs.get(job.id) is not None

    def test_signed_out_cannot_reclaim_anything(self, client, storage):
        old = self._build(storage, "chromium-146.0.7680.177.3")
        self._build(storage, "chromium-148.0.7778.215.5-pro", in_use=True)
        job = self._run(storage)

        for path in ("/settings/storage/builds/prune", "/settings/storage/history/clear"):
            r = client.post(path, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/login"

        assert old.is_dir()
        assert storage.jobs.get(job.id) is not None
