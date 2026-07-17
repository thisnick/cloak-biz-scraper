"""Who is allowed to drive a browser.

Every test here is about refusing *before* the socket is accepted. A CDP session
is total control of a browser holding the user's proxy credentials and cookies,
so "we check the token once you're connected" is not a check.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import isolate_auth

from app.main import app
from app.services import tokens

SECRET = "test-secret-value-long-enough"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        yield c


class FakeInstance:
    def __init__(self, iid="inst1", origin="interactive"):
        self.id = iid
        self.origin = origin
        self.cdp_port = 9222

    def touch(self):
        pass


def refuses(client, url) -> int:
    """The close code of a refused upgrade."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    return exc.value.code


class TestUpgradeIsRefusedBeforeAccept:
    def test_no_token_at_all(self, client):
        assert refuses(client, "/instances/inst1/cdp") == 4401

    def test_a_forged_token(self, client):
        assert refuses(client, "/instances/inst1/cdp?t=not.a.token") == 4401

    def test_a_token_signed_with_another_secret(self, client):
        bad = tokens.issue("inst1", "a-completely-different-secret")
        assert refuses(client, f"/instances/inst1/cdp?t={bad}") == 4401

    def test_an_expired_token(self, client):
        import time

        old = tokens.issue("inst1", SECRET, now=time.time() - 3600)
        assert refuses(client, f"/instances/inst1/cdp?t={old}") == 4401

    def test_a_token_for_a_different_instance(self, client):
        """The one that matters most: a valid token, wrong browser."""
        other = tokens.issue("inst2", SECRET)
        assert refuses(client, f"/instances/inst1/cdp?t={other}") == 4401

    def test_a_valid_token_for_an_instance_that_is_gone(self, client):
        good = tokens.issue("nosuch", SECRET)
        assert refuses(client, f"/instances/nosuch/cdp?t={good}") == 4004

    def test_a_sweeps_browser_is_never_drivable(self, client, monkeypatch):
        """Valid token, real instance, still refused: attaching a debugger to a
        running sweep would corrupt it."""
        monkeypatch.setattr(
            app.state.instances, "get",
            lambda iid: FakeInstance(iid, origin="task"),
        )
        good = tokens.issue("inst1", SECRET)
        assert refuses(client, f"/instances/inst1/cdp?t={good}") == 4003

    def test_a_cross_origin_upgrade_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(app.state.instances, "get", lambda iid: FakeInstance(iid))
        good = tokens.issue("inst1", SECRET)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/instances/inst1/cdp?t={good}", headers={"Origin": "https://evil.example"}
            ):
                pass
        assert exc.value.code == 4403


class TestVersionEndpoint:
    def test_it_needs_a_token_too(self, client):
        """The version document names the socket; handing it out unauthenticated
        would advertise the endpoint to anyone who asked."""
        assert client.get("/instances/inst1/cdp/json/version").status_code == 403

    def test_an_expired_token_is_refused(self, client):
        import time

        old = tokens.issue("inst1", SECRET, now=time.time() - 3600)
        assert client.get(f"/instances/inst1/cdp/json/version?t={old}").status_code == 403
