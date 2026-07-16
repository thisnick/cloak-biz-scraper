"""Persistent profile store.

Ported from browserd (app/profiles.py). The only change: geo defaults are passed
in from settings rather than read from the environment.

A profile is a durable browser identity: a name, its geo pin (country/region),
its sticky Evomi session token, and a fingerprint seed — all paired for the life
of the profile per CloakBrowser guidance ("one profile, one seed"). Cookies live
in the profile's user-data dir. Stored as one JSON file under the profiles dir so
a later instance reattaches warm + geo-coherent even after the container restarts.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from . import proxy


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-")[:80] or "profile"


@dataclass
class Profile:
    name: str
    country: str
    region: str
    session_token: str
    fingerprint_seed: int
    user_data_dir: str


class ProfileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index = self.root / "profiles.json"
        self._lock = threading.Lock()
        self._cache: dict[str, Profile] = {}
        self._load()

    def _load(self) -> None:
        if self._index.exists():
            data = json.loads(self._index.read_text())
            self._cache = {k: Profile(**v) for k, v in data.items()}

    def _flush(self) -> None:
        tmp = self._index.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self._cache.items()}, indent=2))
        tmp.replace(self._index)

    def get_or_create(
        self,
        name: str,
        *,
        default_country: str,
        default_region: str,
        country: str | None = None,
        region: str | None = None,
    ) -> Profile:
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            udd = self.root / _safe(name)
            udd.mkdir(parents=True, exist_ok=True)
            p = Profile(
                name=name,
                country=country or default_country,
                region=region or default_region,
                session_token=proxy.new_session_token(),
                fingerprint_seed=secrets.randbelow(2_000_000_000) + 1,
                user_data_dir=str(udd),
            )
            self._cache[name] = p
            self._flush()
            return p

    def rotate_session(self, name: str) -> Profile | None:
        """Give the profile a fresh Evomi sticky-session token → a new exit IP on
        the next launch. Used to retry past a block (the old IP got flagged) while
        keeping the profile's cookies/warmth."""
        with self._lock:
            p = self._cache.get(name)
            if p is None:
                return None
            p.session_token = proxy.new_session_token()
            self._flush()
            return p

    def all(self) -> list[Profile]:
        with self._lock:
            return list(self._cache.values())
