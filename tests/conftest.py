"""app.config reads DATA_DIR at import, so it has to be set before anything
imports it — conftest is the only module guaranteed to run first.
"""
from __future__ import annotations

import os
import tempfile

# Forced, not setdefault. setdefault lets an ambient DATA_DIR win, so running the
# suite anywhere one is already set — inside the container, most obviously — points
# the tests at a REAL volume: they then read live settings, and
# "healthz reports nothing configured" fails because the deployment is, in fact,
# configured. Worse than a false failure, it is a suite that would write to a
# real /data. Tests get their own directory, always.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cloakbiz-test-")
# The store must seed from a clean slate, not from whoever ran pytest.
for _leak in (
    "CLOAKBROWSER_LICENSE_KEY", "CLOAKBROWSER_VERSION",
    "PROXY_USER", "PROXY_PASSWORD", "PROXY_HOST", "PROXY_PORT",
    "PROXY_COUNTRY", "PROXY_REGION",
    "EVOMI_PROXY_USER", "EVOMI_PROXY_PASSWORD", "EVOMI_PROXY_HOST",
    "EVOMI_PROXY_PORT", "EVOMI_DEFAULT_COUNTRY", "EVOMI_DEFAULT_REGION",
    "NOTION_API_TOKEN", "NOTION_DB_ID",
    "MAX_INSTANCES", "INTERACTIVE_RESERVE",
    # A real secret in the ambient environment would seed the store and make the
    # login tests pass against the wrong value — or, worse, quietly pass.
    "APP_SECRET", "APP_SECRET_RESET",
    # Would widen the Origin rule under the tests' feet.
    "MCP_ALLOWED_ORIGINS",
):
    os.environ.pop(_leak, None)


def mint_access(app, *, subject: str = "owner", client_id: str = "test-client",
                ttl_sec: int | None = None) -> str:
    """A real OAuth access token, minted by the app's own provider.

    Deliberately not a hand-assembled token: hand-assembling one would test our
    idea of what the server mints, and the interesting failures are exactly the
    cases where those two drift. This runs the production minting path and the
    production verification path against each other.
    """
    from app.services import oauth as oauth_service, signing

    if ttl_sec is None:
        return app.state.oauth._mint(
            subject=subject, client_id=client_id, scopes=oauth_service.SCOPES, resource=None
        ).access_token
    # Only the expiry cases need to reach past _mint, because a TTL is the one
    # thing it does not take.
    return signing.issue(
        {"aud": oauth_service._AUD_ACCESS, "sub": subject, "cid": client_id,
         "scopes": oauth_service.SCOPES, "res": None},
        app.state.secret.current(),
        ttl_sec=ttl_sec,
    )
