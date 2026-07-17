"""One HMAC signer, for every bearer this app mints.

There are now five kinds of signed token here — the UI session cookie, the CDP
token, the VNC token, and OAuth's access and refresh tokens — and **all five are
keyed on the same APP_SECRET**. That is a deliberate simplification (one secret,
rotatable, revoking everything at once) with one sharp consequence:

    a valid signature proves the token came from us. It proves NOTHING about
    what the token is FOR.

So `aud` is not decoration, it is the type system. Without it, the bytes of a
VNC token — the one that rides in an iframe URL and is meant to grant *watch* —
would verify perfectly as a CDP token and grant *drive*. A refresh token would
pass as an access token. The session cookie would open a browser. Every caller
here therefore names the audience it expects, and `verify` refuses anything
else. There is no "just check the signature" entry point on purpose.

Sessions and the CDP token each grew their own copy of this code, byte-identical
and independently maintained. sessions.py's own docstring predicted where that
ends ("a second, subtly different signer") — three copies is where it becomes
true, so they now delegate here instead.

Claims are checked in a fixed order, and the order is the point: verify the MAC
**before** parsing the payload. Until the MAC agrees, the payload is
attacker-chosen bytes, and json.loads on attacker-chosen bytes is a wider
surface than a constant-time compare.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return b64e(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def issue(claims: dict, secret: str, *, ttl_sec: int, now: float | None = None) -> str:
    """A signed token carrying `claims`, valid for `ttl_sec`.

    `aud` is expected in claims; `iat`/`exp` are stamped here so no caller can
    forget the expiry.
    """
    now = time.time() if now is None else now
    payload = b64e(
        json.dumps({**claims, "iat": int(now), "exp": int(now + ttl_sec)},
                   separators=(",", ":"), sort_keys=True).encode()
    )
    return f"{payload}.{_sign(payload, secret)}"


def verify(token: str | None, secret: str | None, *, audience: str,
           now: float | None = None) -> dict | None:
    """The claims of a live token minted by us *for this audience*, else None.

    `audience` is mandatory and has no default. A default would eventually be
    accepted by a caller that meant something else, which is exactly the
    confusion this module exists to prevent.
    """
    if not token or not secret or not audience:
        return None
    try:
        payload, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload, secret), signature):
        return None
    try:
        claims = json.loads(b64d(payload))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    if claims.get("aud") != audience:
        return None
    now = time.time() if now is None else now
    if not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
        return None
    return claims
