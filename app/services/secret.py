"""APP_SECRET — the one credential the user manages.

The mechanism matters. An env var alone cannot be rotated by the app: whatever
the UI wrote would revert on the next restart, when the env var is read again.
So the **volume-stored secret is authoritative** and `APP_SECRET` from the
environment only *seeds* it, on first boot.

That creates the obvious hazard — a user who rotates in the UI and then forgets
the new secret has bricked their deployment, with no terminal to fix it from.
Hence the recovery path: set `APP_SECRET_RESET=true` alongside a new
`APP_SECRET` in Railway and the next boot re-seeds from the environment.

Which raises the *opposite* hazard, and it is the subtle one. Railway variables
are sticky: `APP_SECRET_RESET=true` stays set until someone removes it, and a
non-technical user has no reason to think they must. If a set flag simply meant
"re-seed", then every restart would re-seed, the UI's rotation would silently
revert on the next deploy, and we would be back to the un-rotatable env var this
whole module exists to avoid.

So a reset is consumed *once*. We record which secret value the last reset used
(as a hash — never the value) and whether the flag was set on the previous boot.
The reset re-applies only when it is genuinely a *new* request:

  * the flag went from unset to set (the user just toggled it), or
  * the flag is set and `APP_SECRET` differs from the value the last reset
    consumed (the user asked to reset to something new).

A flag left set forever with an unchanged value is therefore inert, and a UI
rotation performed afterwards sticks — which is the property that makes leaving
the flag in place merely untidy rather than dangerous.

Encryption of the settings store is deliberately **not** keyed on this secret
(see crypto.py) precisely so that rotating it never strands the settings.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .crypto import Cipher

logger = logging.getLogger("cloakbiz.secret")

# Single-factor, internet-exposed, and it guards full browser control plus the
# user's proxy and Notion credentials. Railway's secret() mints 32 chars; this
# floor only has to stop someone rotating to "password".
MIN_SECRET_LENGTH = 16

_TRUE = {"1", "true", "yes", "on"}


class WeakSecret(ValueError):
    """Rejected: too short to be the only thing protecting the deployment."""


@dataclass
class _State:
    secret: str
    # sha256 of the env value the last reset consumed. Never the value itself:
    # this file is the one place a plaintext secret could leak into a backup.
    reset_fingerprint: str | None = None
    reset_flag_was_set: bool = False


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


class SecretService:
    """Reads and writes the authoritative secret on the volume."""

    def __init__(self, path: Path, dek_path: Path) -> None:
        self._path = path
        self._cipher = Cipher.from_volume(dek_path)
        self._lock = threading.Lock()
        self._state: _State | None = None

    # ── persistence ──────────────────────────────────────────────────────────
    def _read(self) -> _State | None:
        if not self._path.exists():
            return None
        data = json.loads(self._cipher.decrypt(self._path.read_bytes()))
        return _State(**data)

    def _write(self, state: _State) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._cipher.encrypt(json.dumps(asdict(state)).encode())
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        self._state = state

    # ── boot ─────────────────────────────────────────────────────────────────
    def bootstrap(self) -> str | None:
        """Resolve the secret at process start. Returns None when unconfigured.

        Deliberately never raises on a missing secret. A deployment with no
        APP_SECRET is useless but not broken, and crash-looping would leave the
        user staring at Railway's health-check failure with no idea why. The
        login page tells them instead.
        """
        with self._lock:
            state = self._read()
            env_secret = (os.environ.get("APP_SECRET") or "").strip()
            reset_requested = _env_flag("APP_SECRET_RESET")

            if state is None:
                if not env_secret:
                    logger.warning(
                        "APP_SECRET is not set and none is stored on the volume — "
                        "nobody can log in until it is set"
                    )
                    return None
                # Record the fingerprint when the flag is already set on the very
                # first boot, or the seed would look like an unconsumed reset and
                # revert the user's first rotation.
                state = _State(
                    secret=env_secret,
                    reset_fingerprint=_fingerprint(env_secret) if reset_requested else None,
                    reset_flag_was_set=reset_requested,
                )
                self._write(state)
                logger.info("first boot: seeded APP_SECRET from the environment")
                return state.secret

            if reset_requested and env_secret:
                fp = _fingerprint(env_secret)
                is_new_request = (not state.reset_flag_was_set) or fp != state.reset_fingerprint
                if is_new_request:
                    state.secret = env_secret
                    state.reset_fingerprint = fp
                    logger.warning(
                        "APP_SECRET_RESET honoured: the stored secret was replaced from the "
                        "environment and every existing session is now invalid. Remove "
                        "APP_SECRET_RESET when you are back in."
                    )
                else:
                    logger.info(
                        "APP_SECRET_RESET is still set but was already consumed for this "
                        "value; ignoring it so the stored secret stays authoritative"
                    )
            elif reset_requested and not env_secret:
                logger.warning(
                    "APP_SECRET_RESET is set but APP_SECRET is empty — there is nothing to "
                    "reset to; keeping the stored secret"
                )

            state.reset_flag_was_set = reset_requested
            self._write(state)
            return state.secret

    # ── use ──────────────────────────────────────────────────────────────────
    def current(self) -> str | None:
        with self._lock:
            if self._state is None:
                self._state = self._read()
            return self._state.secret if self._state else None

    def verify(self, candidate: str) -> bool:
        """Constant-time compare — this is a login check on a public endpoint."""
        secret = self.current()
        if not secret or not candidate:
            return False
        return hmac.compare_digest(secret.encode(), candidate.encode())

    def rotate(self, new_secret: str) -> str:
        """Replace the stored secret.

        Every session cookie and signed token is HMAC'd with the secret, so
        changing it invalidates them all with no revocation list to maintain.
        """
        new_secret = new_secret.strip()
        if len(new_secret) < MIN_SECRET_LENGTH:
            raise WeakSecret(
                f"The new secret must be at least {MIN_SECRET_LENGTH} characters. It is "
                f"the only credential protecting this deployment, and it is reachable "
                f"from the public internet."
            )
        with self._lock:
            state = self._state or self._read()
            if state is None:
                state = _State(secret=new_secret)
            else:
                state.secret = new_secret
            self._write(state)
        logger.info("APP_SECRET rotated in the UI; all existing sessions invalidated")
        return new_secret
