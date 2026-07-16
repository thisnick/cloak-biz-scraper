"""Process-level configuration — the little that must be known before the
settings store on the volume can be opened.

Everything a *user* configures (license, proxy, pool sizes) lives in the
encrypted settings store, not here. This module holds only the deployment
facts: where the volume is mounted, what port to bind, and APP_SECRET — the one
variable Railway sets.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("cloakbiz.config")

# Read from settings and passed to launch as arguments instead. Left in the
# process env they would silently outrank the user's settings — see purge_binary_env().
_BINARY_ENV_VARS = ("CLOAKBROWSER_LICENSE_KEY", "CLOAKBROWSER_VERSION")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    port: int
    app_secret: str | None

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def dek_path(self) -> Path:
        return self.data_dir / ".dek"

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def binary_cache_dir(self) -> Path:
        return self.data_dir / ".cloakbrowser"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            port=_int("PORT", 8000),
            app_secret=os.environ.get("APP_SECRET") or None,
        )


CONFIG = Config.from_env()


def bootstrap_binary_cache() -> Path:
    """Point cloakbrowser's binary cache at the volume.

    The package downloads the Pro Chromium on demand; without this it would land
    in ~/.cloakbrowser inside the container's ephemeral layer and be re-fetched
    (~150 MB) after every sleep or redeploy. get_cache_dir() reads this variable
    on each call, so setting it before the first launch is sufficient — but we do
    it at process start so there is one obvious place it happens.
    """
    cache = CONFIG.binary_cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["CLOAKBROWSER_CACHE_DIR"] = str(cache)
    return cache


def purge_binary_env() -> None:
    """Drop the license/pin from the process env once settings have been seeded.

    ensure_binary() falls back to these variables whenever its arguments are
    None. Since we always pass license_key=/browser_version= from settings, a
    leftover env value can only do harm: it would let a stale deploy-time pin
    silently override a version the user just changed in the UI. Call after
    first-boot seeding has already copied them into the store.
    """
    for name in _BINARY_ENV_VARS:
        if os.environ.pop(name, None) is not None:
            logger.info("%s consumed into settings; removed from process env", name)
