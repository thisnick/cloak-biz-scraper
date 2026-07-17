"""Ephemeral CDP tokens, and the instance payload they ride on.

A CDP token is full control of a browser holding the user's residential proxy
and its cookies, handed out in a URL. Everything here is about keeping the blast
radius of that to "this one browser, for ten minutes".
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.services import tokens
from app.services.views import instance_view

SECRET = "a-secret-long-enough-to-be-real"
OTHER = "a-different-secret-entirely"


class TestIssueAndVerify:
    def test_a_fresh_token_verifies_for_its_own_instance(self):
        token = tokens.issue("inst-1", SECRET)
        assert tokens.verify(token, "inst-1", SECRET)

    def test_a_token_is_scoped_to_one_instance(self):
        """The check that stops a token for a browser you may drive from opening
        one you may not."""
        token = tokens.issue("inst-1", SECRET)
        assert not tokens.verify(token, "inst-2", SECRET)

    def test_an_expired_token_is_refused(self):
        token = tokens.issue("inst-1", SECRET, now=time.time() - 3600)
        assert not tokens.verify(token, "inst-1", SECRET)

    def test_expiry_is_the_point_of_the_ttl(self):
        past = time.time() - tokens.TTL_SEC - 1
        assert not tokens.verify(tokens.issue("inst-1", SECRET, now=past), "inst-1", SECRET)
        recent = time.time() - tokens.TTL_SEC + 30
        assert tokens.verify(tokens.issue("inst-1", SECRET, now=recent), "inst-1", SECRET)

    def test_a_token_signed_with_another_secret_is_refused(self):
        assert not tokens.verify(tokens.issue("inst-1", OTHER), "inst-1", SECRET)

    def test_rotating_the_secret_invalidates_every_outstanding_token(self):
        """Revocation for free — the reason this is signed with APP_SECRET."""
        token = tokens.issue("inst-1", SECRET)
        assert tokens.verify(token, "inst-1", SECRET)
        assert not tokens.verify(token, "inst-1", "the-rotated-secret-value")

    def test_a_tampered_payload_is_refused(self):
        """Forge a token for another instance and re-use the old signature."""
        import base64, json

        forged = base64.urlsafe_b64encode(
            json.dumps({"aud": "cdp:inst-2", "sub": tokens.OWNER,
                        "exp": int(time.time() + 600)}).encode()
        ).decode().rstrip("=")
        signature = tokens.issue("inst-1", SECRET).split(".", 1)[1]
        assert not tokens.verify(f"{forged}.{signature}", "inst-2", SECRET)


class TestWatchingIsNotDriving:
    """CDP and VNC are separate grants, signed with the same secret.

    The VNC URL is built to sit in an `iframe src`, where it reaches the DOM,
    the referrer, and the browser history of anyone who opens the dashboard —
    far leakier than an agent's tool call. If one token opened both doors, the
    leakiest URL in the system would also be the most powerful.
    """

    def test_a_vnc_token_does_not_drive(self):
        watch = tokens.issue("inst-1", SECRET, kind=tokens.VNC)
        assert tokens.verify(watch, "inst-1", SECRET, kind=tokens.VNC)
        assert not tokens.verify(watch, "inst-1", SECRET, kind=tokens.CDP)

    def test_a_cdp_token_is_not_a_viewer_token_either(self):
        """Symmetry is not the point — being unable to swap them is."""
        drive = tokens.issue("inst-1", SECRET, kind=tokens.CDP)
        assert not tokens.verify(drive, "inst-1", SECRET, kind=tokens.VNC)

    def test_each_grant_is_still_scoped_to_one_instance(self):
        watch = tokens.issue("inst-1", SECRET, kind=tokens.VNC)
        assert not tokens.verify(watch, "inst-2", SECRET, kind=tokens.VNC)


class TestTakingControl:
    """Watching and driving-over-VNC are the same audience split by a signed
    claim. A control token lifts the view-only floor; a viewer token never does,
    which is what keeps a leaked viewer URL from becoming a way to type into the
    user's browser."""

    def test_a_plain_vnc_token_grants_no_control(self):
        watch = tokens.issue("inst-1", SECRET, kind=tokens.VNC)
        assert not tokens.grants_control(watch, "inst-1", SECRET)

    def test_a_control_token_grants_control(self):
        drive = tokens.issue("inst-1", SECRET, kind=tokens.VNC, control=True)
        assert tokens.grants_control(drive, "inst-1", SECRET)

    def test_a_control_token_is_still_only_a_viewer_for_other_purposes(self):
        """It carries input over VNC; it is not a CDP token and does not become
        one by carrying `ctl`."""
        drive = tokens.issue("inst-1", SECRET, kind=tokens.VNC, control=True)
        assert tokens.verify(drive, "inst-1", SECRET, kind=tokens.VNC)
        assert not tokens.verify(drive, "inst-1", SECRET, kind=tokens.CDP)

    def test_a_cdp_token_does_not_grant_vnc_control(self):
        drive = tokens.issue("inst-1", SECRET, kind=tokens.CDP, control=True)
        assert not tokens.grants_control(drive, "inst-1", SECRET)

    def test_control_is_still_scoped_to_one_instance(self):
        drive = tokens.issue("inst-1", SECRET, kind=tokens.VNC, control=True)
        assert not tokens.grants_control(drive, "inst-2", SECRET)

    def test_control_is_still_bound_to_its_subject(self):
        drive = tokens.issue("inst-1", SECRET, kind=tokens.VNC, subject="alice", control=True)
        assert tokens.grants_control(drive, "inst-1", SECRET, subject="alice")
        assert not tokens.grants_control(drive, "inst-1", SECRET, subject="bob")

    def test_the_control_claim_cannot_be_added_without_the_secret(self):
        """The escalation a leaked viewer token would attempt: forge `ctl` onto
        it. The claim is inside the MAC, so re-signing needs the secret."""
        import base64
        import json

        watch = tokens.issue("inst-1", SECRET, kind=tokens.VNC)
        payload, signature = watch.split(".", 1)
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["ctl"] = 1
        forged = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).decode().rstrip("=")
        assert not tokens.grants_control(f"{forged}.{signature}", "inst-1", SECRET)

    def test_junk_grants_no_control(self):
        for junk in (None, "", "x", "a.b.c"):
            assert not tokens.grants_control(junk, "inst-1", SECRET)


class TestSubjectBinding:
    """The binding Step 3 could not have, because no subjects existed.

    With one APP_SECRET there is exactly one subject today, so this cannot fire
    in production — it is enforced and real, but it is defence in depth for a
    future with more than one subject rather than a wall between two users who
    exist now.
    """

    def test_a_token_verifies_for_the_subject_it_was_minted_for(self):
        token = tokens.issue("inst-1", SECRET, subject="alice")
        assert tokens.verify(token, "inst-1", SECRET, subject="alice")

    def test_another_subjects_token_is_refused(self):
        token = tokens.issue("inst-1", SECRET, subject="alice")
        assert not tokens.verify(token, "inst-1", SECRET, subject="bob")

    def test_the_default_subject_is_the_one_owner(self):
        assert tokens.verify(tokens.issue("inst-1", SECRET), "inst-1", SECRET,
                             subject=tokens.OWNER)

    def test_a_subject_cannot_be_swapped_without_breaking_the_signature(self):
        """The sub claim is inside the MAC, not beside it."""
        token = tokens.issue("inst-1", SECRET, subject="alice")
        payload, signature = token.split(".", 1)
        import base64, json

        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["sub"] = "bob"
        forged = base64.urlsafe_b64encode(
            json.dumps(claims).encode()
        ).decode().rstrip("=")
        assert not tokens.verify(f"{forged}.{signature}", "inst-1", SECRET, subject="bob")

    def test_junk_is_refused_rather_than_raising(self):
        for junk in (None, "", "x", "a.b.c", "....", "notbase64!.sig"):
            assert not tokens.verify(junk, "inst-1", SECRET)

    def test_no_secret_means_no_access(self):
        assert not tokens.verify(tokens.issue("inst-1", SECRET), "inst-1", None)


@dataclass
class FakeInstance:
    id: str = "abc123"
    profile: str = "agent"
    origin: str = "interactive"
    proxy_ip: str | None = "203.0.113.7"
    timezone: str | None = "America/Los_Angeles"
    locale: str | None = "en-US"
    geoip: bool = True
    humanize: bool = True
    ttl_min: int = 60
    created_wall: float = 1_700_000_000.0

    def age_sec(self) -> float:
        return 12.0

    def idle_sec(self) -> float:
        return 3.0


class TestInstanceView:
    def test_an_unmeasured_timezone_stays_absent(self):
        """The Step 1 carry-forward, at the exact place it would resurface.

        Step 1 reported America/Los_Angeles for instances whose geo never
        resolved — a value nobody measured, presented as fact, on a browser whose
        proxy could not even route. Step 2 deleted the fallback. This is the
        first step where an agent can see the field, so it is the first step
        where a default would be believed.
        """
        view = instance_view(FakeInstance(timezone=None, locale=None))
        assert view.timezone is None, "never substitute a plausible timezone"
        assert view.locale is None

    def test_a_measured_timezone_is_reported(self):
        assert instance_view(FakeInstance()).timezone == "America/Los_Angeles"

    def test_the_cdp_url_is_freshly_minted_every_call(self, monkeypatch):
        """Never cached or stored on the instance.

        Two calls in the same second legitimately produce identical bytes (same
        iat, same exp), so comparing the strings would prove nothing either way.
        What matters is that a token is minted per call rather than reused, and
        that its expiry therefore always counts from now — so this counts the
        mintings.
        """
        minted: list[str] = []
        real_issue = tokens.issue

        def counting_issue(instance_id, secret, **kw):
            minted.append(instance_id)
            return real_issue(instance_id, secret, **kw)

        monkeypatch.setattr("app.services.views.tokens.issue", counting_issue)
        inst = FakeInstance()
        first = instance_view(inst, secret=SECRET, base_url="https://app.example/")
        instance_view(inst, secret=SECRET, base_url="https://app.example/")

        assert minted == ["abc123", "abc123"], "a token per call, not one cached on the instance"
        assert tokens.verify(first.cdp_url.split("t=")[1], inst.id, SECRET)

    def test_a_token_minted_now_expires_later_than_one_minted_earlier(self):
        """The freshness that matters: each call restarts the ten minutes."""
        old = tokens.issue("abc123", SECRET, now=time.time() - 300)
        new = tokens.issue("abc123", SECRET)
        assert old != new
        assert tokens.verify(old, "abc123", SECRET) and tokens.verify(new, "abc123", SECRET)

    def test_the_cdp_url_is_a_websocket_url_for_this_instance(self):
        view = instance_view(FakeInstance(), secret=SECRET, base_url="https://app.example/")
        assert view.cdp_url.startswith("wss://app.example/instances/abc123/cdp?t=")

    def test_http_becomes_ws_not_wss(self):
        view = instance_view(FakeInstance(), secret=SECRET, base_url="http://localhost:8000/")
        assert view.cdp_url.startswith("ws://localhost:8000/instances/abc123/cdp?t=")

    def test_without_a_secret_there_is_no_url_rather_than_a_fake_one(self):
        """A URL nobody can open is worse than none: it sends the reader
        debugging their client for a server that never signed anything."""
        assert instance_view(FakeInstance(), secret=None, base_url="https://app.example/").cdp_url is None

    def test_the_view_exposes_the_plan_fields(self):
        view = instance_view(FakeInstance(), secret=SECRET, base_url="https://app.example/")
        assert view.instance_id == "abc123"
        assert view.proxy_ip == "203.0.113.7"
        assert view.expires_at == 1_700_000_000.0 + 3600
        assert view.age_sec == 12.0
