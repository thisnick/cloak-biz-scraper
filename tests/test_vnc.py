"""Who is allowed to watch a browser, and what watching is allowed to do.

Step 3 left `vnc_url` always null. Now that it is real, "live view" needs the
same paranoia as CDP: the pixels are the user's logged-in sessions, and RFB
carries pointer and key events, so an unfiltered viewer is a person typing into
the user's authenticated browser rather than someone looking at it.
"""
from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import isolate_auth

from app.main import app
from app.services import rfb, tokens
from app.services.views import instance_view

SECRET = "test-secret-value-long-enough"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    monkeypatch.delenv("APP_SECRET_RESET", raising=False)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        yield c


class FakeInstance:
    def __init__(self, iid="inst1", origin="interactive", vnc_port=6100, owner=tokens.OWNER):
        self.id = iid
        self.origin = origin
        self.vnc_port = vnc_port
        self.cdp_port = 9222
        self.owner = owner
        self.profile = "agent"
        self.proxy_ip = "203.0.113.7"
        self.timezone = "America/Los_Angeles"
        self.locale = "en-US"
        self.geoip = True
        self.humanize = True
        self.ttl_min = 60
        self.created_wall = 1_700_000_000.0

    def touch(self):
        pass

    def age_sec(self):
        return 1.0

    def idle_sec(self):
        return 1.0


def refuses(client, url) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url):
            pass
    return exc.value.code


class TestUpgradeIsRefusedBeforeAccept:
    def test_no_token_at_all(self, client):
        assert refuses(client, "/instances/inst1/vnc") == 4401

    def test_a_forged_token(self, client):
        assert refuses(client, "/instances/inst1/vnc?t=not.a.token") == 4401

    def test_a_token_signed_with_another_secret(self, client):
        bad = tokens.issue("inst1", "a-completely-different-secret", kind=tokens.VNC)
        assert refuses(client, f"/instances/inst1/vnc?t={bad}") == 4401

    def test_an_expired_token(self, client):
        import time

        old = tokens.issue("inst1", SECRET, kind=tokens.VNC, now=time.time() - 3600)
        assert refuses(client, f"/instances/inst1/vnc?t={old}") == 4401

    def test_a_token_for_a_different_instance(self, client):
        other = tokens.issue("inst2", SECRET, kind=tokens.VNC)
        assert refuses(client, f"/instances/inst1/vnc?t={other}") == 4401

    def test_a_cdp_token_cannot_open_the_viewer(self, client):
        """Not symmetry for its own sake: it proves the two grants are actually
        distinct rather than one grant with two names."""
        drive = tokens.issue("inst1", SECRET, kind=tokens.CDP)
        assert refuses(client, f"/instances/inst1/vnc?t={drive}") == 4401

    def test_a_vnc_token_cannot_open_cdp(self, client, monkeypatch):
        """The one that matters most. This URL is designed to be pasted into an
        iframe; it must never be a browser-control token."""
        monkeypatch.setattr(app.state.instances, "get", lambda iid: FakeInstance(iid))
        watch = tokens.issue("inst1", SECRET, kind=tokens.VNC)
        assert refuses(client, f"/instances/inst1/cdp?t={watch}") == 4401

    def test_another_subjects_token_is_refused(self, client, monkeypatch):
        """Cross-subject. Enforced and real; with one APP_SECRET there is one
        subject, so it cannot fire in production today."""
        monkeypatch.setattr(
            app.state.instances, "get", lambda iid: FakeInstance(iid, owner="alice")
        )
        someone_else = tokens.issue("inst1", SECRET, kind=tokens.VNC, subject="mallory")
        assert refuses(client, f"/instances/inst1/vnc?t={someone_else}") == 4401

    def test_the_owners_token_is_accepted_far_enough_to_try_connecting(self, client, monkeypatch):
        """The positive control. Without it, every refusal above could be a
        route that rejects everything — which would pass just as well.

        There is no Xvnc behind port 1 (deliberately closed), so this cannot
        reach a live view; what it proves is that authorization *passed* and the
        socket was accepted, because the failure now comes from the upstream
        dial rather than from a 4401.
        """
        monkeypatch.setattr(
            app.state.instances, "get", lambda iid: FakeInstance(iid, owner="alice", vnc_port=1)
        )
        mine = tokens.issue("inst1", SECRET, kind=tokens.VNC, subject="alice")
        # Reaching the body of this `with` at all is the assertion: a refused
        # upgrade raises WebSocketDisconnect out of __enter__, which is exactly
        # how every negative test above detects a refusal.
        with client.websocket_connect(f"/instances/inst1/vnc?t={mine}"):
            pass

    def test_an_instance_with_no_live_view_says_so(self, client, monkeypatch):
        """A browser on the Xvfb fallback has no framebuffer to serve."""
        monkeypatch.setattr(
            app.state.instances, "get", lambda iid: FakeInstance(iid, vnc_port=None)
        )
        good = tokens.issue("inst1", SECRET, kind=tokens.VNC)
        assert refuses(client, f"/instances/inst1/vnc?t={good}") == 4004

    def test_a_cross_origin_upgrade_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(app.state.instances, "get", lambda iid: FakeInstance(iid))
        good = tokens.issue("inst1", SECRET, kind=tokens.VNC)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/instances/inst1/vnc?t={good}", headers={"Origin": "https://evil.example"}
            ):
                pass
        assert exc.value.code == 4403


class TestTaskBrowsersAreWatchableButNotTouchable:
    def test_a_sweeps_browser_is_still_never_drivable(self, client, monkeypatch):
        """The Step 3 property. VNC being more permissive must not have loosened
        this by accident."""
        monkeypatch.setattr(
            app.state.instances, "get", lambda iid: FakeInstance(iid, origin="task")
        )
        good = tokens.issue("inst1", SECRET, kind=tokens.CDP)
        assert refuses(client, f"/instances/inst1/cdp?t={good}") == 4003

    def test_input_is_stripped_for_a_task_browser(self):
        """What makes watching a sweep safe. A KeyEvent and a PointerEvent are
        typing and clicking; the sweep is mid-navigation on its own schedule."""
        key = struct.pack(">BBxxI", 4, 1, 0x41)
        pointer = struct.pack(">BBHH", 5, 1, 100, 200)
        stream = key + pointer

        assert rfb.filter_client_messages(stream, view_only=False) != b""
        assert rfb.filter_client_messages(stream, view_only=True) == b""

    def test_watching_still_works_when_input_is_stripped(self):
        """View-only must not mean "nothing gets through": the viewer still has
        to ask for framebuffer updates or it renders a blank rectangle."""
        update_request = struct.pack(">BBHHHH", 3, 0, 0, 0, 800, 600)
        out = rfb.filter_client_messages(update_request, view_only=True)
        assert out and out[0] == 3

    def test_an_input_message_does_not_desynchronise_the_stream(self):
        """The subtle one. Dropping a message means knowing its length; a filter
        that guessed would pass the rest of the frame through as garbage — which
        is how a "read-only" viewer sends a click.
        """
        key = struct.pack(">BBxxI", 4, 1, 0x41)
        update_request = struct.pack(">BBHHHH", 3, 0, 0, 0, 800, 600)
        out = rfb.filter_client_messages(key + update_request, view_only=True)
        assert out == update_request, "the message after a dropped one must survive intact"


class TestTheViewerUrl:
    def test_it_is_offered_when_the_browser_has_a_live_view(self):
        view = instance_view(FakeInstance(), secret=SECRET, base_url="https://app.example/")
        assert view.vnc_url and view.vnc_url.startswith("https://app.example/novnc/vnc.html")

    def test_it_carries_a_vnc_token_not_a_cdp_one(self):
        view = instance_view(FakeInstance(), secret=SECRET, base_url="https://app.example/")
        token = view.vnc_url.split("t%3D")[1].split("&")[0]
        assert tokens.verify(token, "inst1", SECRET, kind=tokens.VNC)
        assert not tokens.verify(token, "inst1", SECRET, kind=tokens.CDP)

    def test_it_is_omitted_when_there_is_no_live_view(self):
        """Rather than a URL that loads a page which spins forever."""
        assert instance_view(
            FakeInstance(vnc_port=None), secret=SECRET, base_url="https://app.example/"
        ).vnc_url is None

    def test_it_is_omitted_without_a_secret(self):
        assert instance_view(
            FakeInstance(), secret=None, base_url="https://app.example/"
        ).vnc_url is None

    def test_the_two_urls_carry_different_tokens(self):
        """If these were ever the same string, the iframe URL would drive."""
        view = instance_view(FakeInstance(), secret=SECRET, base_url="https://app.example/")
        assert view.cdp_url.split("t=")[1] != view.vnc_url.split("t%3D")[1].split("&")[0]
