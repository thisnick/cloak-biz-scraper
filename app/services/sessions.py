"""Cookie sessions for the settings UI.

Stateless and HMAC'd with the current APP_SECRET, which buys the invalidation
requirement for free: rotating the secret changes the signing key, so every
cookie minted under the old one stops verifying. No session table, no
revocation list, nothing to garbage-collect after a rotation.

The signing itself lives in services/signing.py, which every bearer in this app
now shares. This module had its own copy, byte-identical to the CDP token's, and
the audience (`ui`) is what keeps a session cookie from being a browser-control
token — see that module on why `aud` is the type system here.
"""
from __future__ import annotations

from . import signing

COOKIE_NAME = "cbs_session"
# Long enough not to nag a self-hosted operator, short enough that a stolen
# cookie is not indefinite. Re-login is a paste from Railway's Variables tab.
SESSION_TTL_SEC = 7 * 24 * 3600

_AUD = "ui"


def issue(secret: str, *, ttl_sec: int = SESSION_TTL_SEC, now: float | None = None) -> str:
    return signing.issue({"aud": _AUD}, secret, ttl_sec=ttl_sec, now=now)


def verify(token: str | None, secret: str | None, *, now: float | None = None) -> bool:
    """True only for a well-formed, correctly signed, unexpired UI session."""
    return signing.verify(token, secret, audience=_AUD, now=now) is not None
