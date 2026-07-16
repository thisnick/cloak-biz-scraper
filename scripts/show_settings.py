"""Print the settings currently stored on the volume, with secrets redacted.

  docker compose exec app python scripts/show_settings.py

Reads through the same service the app uses, so what you see here is exactly
what a launch will use — including whether the environment was ignored.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/app")

from app.config import CONFIG  # noqa: E402
from app.services.settings import SettingsService  # noqa: E402

settings = SettingsService(CONFIG.settings_path, CONFIG.dek_path).load()
print(json.dumps({
    "settings_file": str(CONFIG.settings_path),
    "exists": CONFIG.settings_path.exists(),
    "settings": settings.redacted(),
    "task_budget": settings.task_budget,
    # Shown so a mismatch between env and store is obvious rather than mystifying:
    # after first boot the store wins, always.
    "env_right_now": {
        name: ("<set>" if os.environ.get(name) else None)
        for name in ("CLOAKBROWSER_LICENSE_KEY", "CLOAKBROWSER_VERSION",
                     "EVOMI_PROXY_USER", "MAX_INSTANCES")
    },
}, indent=2))
