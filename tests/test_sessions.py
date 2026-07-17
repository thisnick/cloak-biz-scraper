"""Session cookies. Stateless and keyed on the secret, which is what makes
"rotating signs everyone out" true without a session table."""
from __future__ import annotations

import time

from app.services import sessions, signing, tokens

SECRET = "the-signing-secret-000001"


def test_round_trip():
    assert sessions.verify(sessions.issue(SECRET), SECRET)


def test_rotating_the_secret_invalidates_every_session():
    token = sessions.issue(SECRET)
    assert not sessions.verify(token, "a-rotated-secret-000002")


def test_expired_token_rejected():
    old = sessions.issue(SECRET, ttl_sec=-1)
    assert not sessions.verify(old, SECRET)


def test_expiry_is_checked_against_now():
    token = sessions.issue(SECRET, ttl_sec=60)
    assert sessions.verify(token, SECRET, now=time.time() + 30)
    assert not sessions.verify(token, SECRET, now=time.time() + 120)


def test_tampering_with_the_payload_breaks_the_signature():
    token = sessions.issue(SECRET)
    payload, signature = token.split(".", 1)
    forged = signing.b64e(b'{"aud":"ui","iat":1,"exp":99999999999}')
    assert not sessions.verify(f"{forged}.{signature}", SECRET)


def test_unsigned_token_rejected():
    # An attacker stripping the MAC must not be read as "no signature required".
    payload = signing.b64e(b'{"aud":"ui","iat":1,"exp":99999999999}')
    assert not sessions.verify(payload, SECRET)
    assert not sessions.verify(f"{payload}.", SECRET)


def test_a_real_cdp_token_is_not_a_session():
    """The confusion this must never permit, using the actual other token.

    Step 4 mints CDP and VNC tokens off the same APP_SECRET, so a CDP token's
    signature verifies perfectly here — `aud` is the only thing standing between
    "drive this one browser for ten minutes" and full access to the settings,
    including the licence key and the Notion token. Forging a payload by hand
    would test our idea of what a CDP token looks like; minting a real one tests
    the thing that actually exists.
    """
    assert not sessions.verify(tokens.issue("abc", SECRET), SECRET)
    assert not sessions.verify(tokens.issue("abc", SECRET, kind=tokens.VNC), SECRET)


def test_a_session_is_not_a_cdp_token():
    """And the same door in the other direction: the session cookie is the
    longest-lived bearer here (a week), and it must not open a browser."""
    assert not tokens.verify(sessions.issue(SECRET), "abc", SECRET)


def test_garbage_rejected_without_raising():
    for junk in (None, "", "....", "not-a-token", "a.b.c", "!!!.???"):
        assert not sessions.verify(junk, SECRET)


def test_no_secret_verifies_nothing():
    assert not sessions.verify(sessions.issue(SECRET), None)
