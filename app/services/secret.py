"""APP_SECRET — the one credential the user manages.

**It is the `APP_SECRET` environment variable, read fresh every boot. That is the
whole model.** Railway's Variables tab is the single source of truth; there is no
copy stored on the volume, nothing to rotate in the app, and no "which one wins".

It used to be more than this. The secret was seeded from the environment onto the
volume and the volume copy was authoritative, so the app could rotate it in its
own UI — which then needed an `APP_SECRET_RESET` escape hatch for a forgotten
rotation, which needed careful once-only-consumption logic so a left-set flag
didn't silently revert things. Two places to hold one value, and a page of
reasoning about which was true. A non-technical user asked, reasonably, why —
and the answer was a hazard that turned out not to exist.

The volume-authoritative design existed to guard two unknowns about Railway's
`secret()`. Step 5 measured both away: `secret()` is **stable across redeploys**
(sha256-identical over three redeploy triggers) and **readable** (not sealed), so
"copy it from Railway, log in" is safe and a redeploy will not silently change it
underneath the user. With the hazards gone, the volume copy bought only
in-app rotation — which, on a single-user server, is the same one action as
editing the Railway variable, minus a page of explanation.

So: to change the secret, edit `APP_SECRET` in Railway and redeploy. The new value
is in force on the next boot and every existing session and signed token — all
HMAC'd with the old value — is invalidated with no revocation list to keep. To
recover a forgotten one, read it from Railway → Variables → `APP_SECRET`; it is
right there, which is the point.

Encryption of the settings store is **not** keyed on this secret (see crypto.py),
so changing `APP_SECRET` never strands the settings — the same property that made
rotation safe before, now doing nothing but staying true.
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
