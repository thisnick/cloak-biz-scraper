"""The settings store — the app's source of truth for everything a user configures.

The core UX bet: Railway sets exactly one variable (APP_SECRET) and everything
else is filled into a web form and persisted here, on the volume. So this store,
not the environment, is authoritative.

Env vars only ever *seed* the store, and only on first boot, as a convenience for
local dev and CI. Once settings.json exists the environment is ignored — which is
what makes the settings editable at all. (An env var that won re-reads would
silently revert every UI change on the next restart.)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .crypto import Cipher

logger = logging.getLogger("cloakbiz.settings")

# Above this we warn and still obey. The ceiling here is cost, not memory —
# Railway's Hobby plan allows 48 GB per service, so a pool of 12 fits fine and
# simply bills more. A cap we invented would be us guessing at someone else's
# budget; a warning tells them what they are choosing and lets them choose it.
POOL_WARN_ABOVE = 8


class Settings(BaseModel):
    """Everything the user configures. Extended in later steps (Notion, etc.)."""

    # CloakBrowser. The binary is downloaded on demand by the cloakbrowser
    # package; we only supply these two as launch arguments.
    cloakbrowser_license_key: str = ""
    cloakbrowser_version: str = Field(
        default="",
        description="Optional exact Chromium pin, e.g. '148.0.7778.215.2'. Empty = track latest.",
    )

    # Evomi residential proxy.
    proxy_user: str = ""
    proxy_password: str = ""
    proxy_host: str = ""
    proxy_port: str = ""
    proxy_country: str = "US"
    proxy_region: str = "california"

    # Notion. The database is chosen or created explicitly in the UI and its id
    # stored here — never discovered, never created on the fly (decision #5).
    notion_api_token: str = ""
    notion_db_id: str = ""

    # Pool budget. Task budget = max_instances - interactive_reserve; interactive
    # sessions are never starved by a running sweep.
    max_instances: int = Field(default=4, ge=1)
    interactive_reserve: int = Field(default=1, ge=0)

    @field_validator("cloakbrowser_version")
    @classmethod
    def _check_pin(cls, v: str) -> str:
        """Reject a malformed pin here rather than at first launch.

        Defers to the package's own validator so our idea of a valid pin can
        never drift from the one that interpolates it into download URLs.
        """
        from cloakbrowser.config import normalize_requested_version

        v = v.strip()
        if not v:
            return ""
        try:
            normalize_requested_version(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return v

    @model_validator(mode="after")
    def _reserve_fits(self) -> "Settings":
        if self.interactive_reserve >= self.max_instances:
            raise ValueError(
                f"interactive_reserve ({self.interactive_reserve}) must be less than "
                f"max_instances ({self.max_instances}), otherwise no slot is left for "
                f"scraping and every sweep would wait forever."
            )
        return self

    @property
    def task_budget(self) -> int:
        """Slots all tasks combined may hold; the rest is the interactive floor."""
        return max(1, self.max_instances - self.interactive_reserve)

    def pool_warning(self) -> str | None:
        """Advice for an unusually large pool, or None. Never a refusal."""
        if self.max_instances <= POOL_WARN_ABOVE:
            return None
        return (
            f"{self.max_instances} browsers is a lot — roughly "
            f"{self.max_instances // 2}–{self.max_instances} GB while a sweep runs. That "
            f"is allowed and will work; it just costs more per minute of sweeping. Most "
            f"people find 4 is plenty."
        )

    def proxy_configured(self) -> bool:
        return bool(self.proxy_user and self.proxy_password and self.proxy_host and self.proxy_port)

    def notion_configured(self) -> bool:
        """A token alone is not enough: without a chosen database there is
        nowhere to sync, and we will not pick one for the user."""
        return bool(self.notion_api_token and self.notion_db_id)

    def redacted(self) -> dict[str, Any]:
        """A view safe to log or return over the wire."""
        data = self.model_dump()
        for secret in ("cloakbrowser_license_key", "proxy_password", "notion_api_token"):
            data[secret] = "***" if data[secret] else ""
        return data


# Each setting may be seeded from an env var of the same name. The extra
# candidates keep the documented Evomi/CloakBrowser names working, so the same
# .env drives this app and browserd.
_ENV_SEEDS: dict[str, tuple[str, ...]] = {
    "cloakbrowser_license_key": ("CLOAKBROWSER_LICENSE_KEY",),
    "cloakbrowser_version": ("CLOAKBROWSER_VERSION",),
    "proxy_user": ("PROXY_USER", "EVOMI_PROXY_USER"),
    "proxy_password": ("PROXY_PASSWORD", "EVOMI_PROXY_PASSWORD"),
    "proxy_host": ("PROXY_HOST", "EVOMI_PROXY_HOST"),
    "proxy_port": ("PROXY_PORT", "EVOMI_PROXY_PORT"),
    "proxy_country": ("PROXY_COUNTRY", "EVOMI_DEFAULT_COUNTRY"),
    "proxy_region": ("PROXY_REGION", "EVOMI_DEFAULT_REGION"),
    "notion_api_token": ("NOTION_API_TOKEN",),
    "notion_db_id": ("NOTION_DB_ID",),
    "max_instances": ("MAX_INSTANCES",),
    "interactive_reserve": ("INTERACTIVE_RESERVE",),
}


def _seed_from_env() -> dict[str, Any]:
    seeded: dict[str, Any] = {}
    for field, candidates in _ENV_SEEDS.items():
        for name in candidates:
            value = os.environ.get(name, "").strip()
            if value:
                seeded[field] = value
                break
    return seeded


class SettingsService:
    """Reads and writes the encrypted settings file on the volume."""

    def __init__(self, path: Path, dek_path: Path) -> None:
        self._path = path
        self._cipher = Cipher.from_volume(dek_path)
        self._lock = threading.Lock()
        self._cache: Settings | None = None

    def load(self) -> Settings:
        """Current settings, seeding from the environment on first boot only."""
        with self._lock:
            if self._cache is not None:
                return self._cache
            if self._path.exists():
                self._cache = self._read()
            else:
                self._cache = self._first_boot()
            return self._cache

    def _read(self) -> Settings:
        plaintext = self._cipher.decrypt(self._path.read_bytes())
        try:
            return Settings.model_validate(json.loads(plaintext))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError(
                f"Stored settings at {self._path} are not valid: {exc}"
            ) from exc

    def _first_boot(self) -> Settings:
        seeded = _seed_from_env()
        try:
            settings = Settings.model_validate(seeded)
        except ValidationError as exc:
            raise RuntimeError(
                f"Cannot seed settings from the environment: {exc}"
            ) from exc
        self._write(settings)
        if seeded:
            logger.info(
                "first boot: seeded %s from the environment; the volume is authoritative from now on",
                ", ".join(sorted(seeded)),
            )
        else:
            logger.info("first boot: no seed variables set; starting with defaults")
        return settings

    def _write(self, settings: Settings) -> None:
        """Encrypt and replace atomically — a torn write would lose every setting."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._cipher.encrypt(settings.model_dump_json().encode())
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def save(self, settings: Settings) -> Settings:
        with self._lock:
            self._write(settings)
            self._cache = settings
            return settings

    def update(self, **changes: Any) -> Settings:
        """Apply a partial change, validating the merged result."""
        current = self.load()
        with self._lock:
            merged = Settings.model_validate({**current.model_dump(), **changes})
            self._write(merged)
            self._cache = merged
            return merged
