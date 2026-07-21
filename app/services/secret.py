"""APP_SECRET — the one credential the user manages.

The `APP_SECRET` environment variable is the single source of truth and is read
directly whenever it is needed. On Railway, the value lives in the service's
Variables tab; changing it takes effect when the service redeploys and
invalidates every session and signed token made with the old value.

The settings store is encrypted with its own volume-local data key (see
crypto.py), not APP_SECRET, so changing the login/signing secret never strands
saved browser, proxy, or Notion settings.
"""
from __future__ import annotations

import hmac
import logging
import os

logger = logging.getLogger("cloakbiz.secret")


class SecretService:
    """The APP_SECRET, straight from the environment.

    No state, no file. `current()` reads `os.environ` every call, so a value
    changed in Railway takes effect on the redeploy that restarts the process —
    which is the only way it can change.
    """

    @staticmethod
    def _from_env() -> str | None:
        value = (os.environ.get("APP_SECRET") or "").strip()
        return value or None

    def bootstrap(self) -> str | None:
        """Resolve the secret at process start. Returns None when unconfigured.

        Never raises on a missing secret: a deployment with no `APP_SECRET` is
        useless but not broken, and crash-looping would leave the user staring at
        a Railway health-check failure with no idea why. The login page tells them
        to set it instead.
        """
        secret = self._from_env()
        if secret is None:
            logger.warning(
                "APP_SECRET is not set — nobody can log in until it is set in "
                "Railway's Variables tab"
            )
        return secret

    def current(self) -> str | None:
        return self._from_env()

    def verify(self, candidate: str) -> bool:
        """Constant-time compare — this is a login check on a public endpoint."""
        secret = self._from_env()
        if not secret or not candidate:
            return False
        return hmac.compare_digest(secret.encode(), candidate.encode())
