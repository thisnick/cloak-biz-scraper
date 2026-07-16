"""Cookie sessions for the settings UI.

Stateless and HMAC'd with the current APP_SECRET, which buys the invalidation
requirement for free: rotating the secret changes the signing key, so every
cookie minted under the old one stops verifying. No session table, no
revocation list, nothing to garbage-collect after a rotation.

Step 4 signs ephemeral CDP/VNC tokens off the same secret. The claims here are
deliberately a superset-friendly shape (aud/exp) so that code can reuse this
module rather than growing a second, subtly different signer.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

COOKIE_NAME = "cbs_session"
# Long enough not to nag a self-hosted operator, short enough that a stolen
# cookie is not indefinite. Re-login is a paste from Railway's Variables tab.
SESSION_TTL_SEC = 7 * 24 * 3600

_AUD = "ui"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def issue(secret: str, *, ttl_sec: int = SESSION_TTL_SEC, now: float | None = None) -> str:
    now = time.time() if now is None else now
    claims = {"aud": _AUD, "iat": int(now), "exp": int(now + ttl_sec)}
    payload = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload, secret)}"


def verify(token: str | None, secret: str | None, *, now: float | None = None) -> bool:
    """True only for a well-formed, correctly signed, unexpired UI session."""
    if not token or not secret:
        return False
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False
    # Compare before parsing: the payload is attacker-supplied until the MAC says
    # otherwise, and json.loads on unauthenticated input is a wider surface.
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return False
    try:
        claims = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    if claims.get("aud") != _AUD:
        return False
    now = time.time() if now is None else now
    return isinstance(claims.get("exp"), int) and claims["exp"] > now
