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
):
    os.environ.pop(_leak, None)
