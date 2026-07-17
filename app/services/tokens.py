"""Ephemeral, instance-scoped tokens for the CDP endpoint.

CDP is **full control** of a browser holding the user's residential proxy and
whatever cookies it has collected. So the token that opens it is minted by the
machine, never handled by the user, scoped to one instance, and dies in minutes.

**Why the token goes in the URL.** WebSocket clients frequently cannot set
headers — that is the whole reason this is not simply a Bearer token. A URL is a
leaky place for a credential (proxy logs, history, agent transcripts), which is
exactly why the thing we put there is not the credential that matters: this
grants "drive this one browser for ten minutes", not "do anything to this
deployment". `Authorization: Bearer` is still accepted for clients that can.

Signed with the same APP_SECRET the UI session uses, which buys revocation for
free: rotating the secret invalidates every outstanding token immediately.

**Step 4 adds `sub`** (the OAuth subject) so a token minted for one user cannot
be replayed by another. Until OAuth exists there are no subjects to bind to, and
inventing a placeholder would be a check that looks like it is doing something
and is not. What is enforced today — signature, expiry, and instance scope — is
enforced for real.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

# Ten minutes: long enough to attach a debugger and work, short enough that a
# token found in a log later is worthless.
TTL_SEC = 10 * 60

_PREFIX = "instance:"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def issue(instance_id: str, secret: str, *, ttl_sec: int = TTL_SEC,
          now: float | None = None) -> str:
    """A fresh token for one instance. Minted per call — never cached, never reused."""
    now = time.time() if now is None else now
    claims = {"aud": f"{_PREFIX}{instance_id}", "iat": int(now), "exp": int(now + ttl_sec)}
    payload = _b64e(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload, secret)}"


def verify(token: str | None, instance_id: str, secret: str | None,
           *, now: float | None = None) -> bool:
    """True only for a live token minted for *this* instance.

    The audience check is what stops a token for a browser the caller is allowed
    to drive from opening one they are not.
    """
    if not token or not secret or not instance_id:
        return False
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return False
    # Verify before parsing: until the MAC agrees, the payload is attacker-chosen
    # input and json.loads on it is a wider surface than a constant-time compare.
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return False
    try:
        claims = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return False
    if claims.get("aud") != f"{_PREFIX}{instance_id}":
        return False
    now = time.time() if now is None else now
    return isinstance(claims.get("exp"), int) and claims["exp"] > now
