"""Session cookies. Stateless and keyed on the secret, which is what makes
"rotating signs everyone out" true without a session table."""
from __future__ import annotations

import time

from app.services import sessions

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
    forged = sessions._b64e(b'{"aud":"ui","iat":1,"exp":99999999999}')
    assert not sessions.verify(f"{forged}.{signature}", SECRET)


def test_unsigned_token_rejected():
    # An attacker stripping the MAC must not be read as "no signature required".
    payload = sessions._b64e(b'{"aud":"ui","iat":1,"exp":99999999999}')
    assert not sessions.verify(payload, SECRET)
    assert not sessions.verify(f"{payload}.", SECRET)


def test_wrong_audience_rejected():
    # Step 4 mints CDP/VNC tokens off the same secret. One of those must never
    # be accepted as a UI session — that would turn "drive this one browser for
    # ten minutes" into full access to the settings.
    import json

    payload = sessions._b64e(
        json.dumps({"aud": "instance:abc", "exp": int(time.time() + 600)}).encode()
    )
    token = f"{payload}.{sessions._sign(payload, SECRET)}"
    assert not sessions.verify(token, SECRET)


def test_garbage_rejected_without_raising():
    for junk in (None, "", "....", "not-a-token", "a.b.c", "!!!.???"):
        assert not sessions.verify(junk, SECRET)


def test_no_secret_verifies_nothing():
    assert not sessions.verify(sessions.issue(SECRET), None)
