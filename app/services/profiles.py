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
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from . import proxy

# The one profile the interactive paths (create_instance, "+ New browser") use
# unless overridden, so cookies/logins stay consistent and casual users never
# think about it. Sweeps deliberately do NOT use it — a sweep's cookies and any
# block on its exit IP must not land in the identity the user is logged in with.
DEFAULT_PROFILE = "Default"


class ProfileError(ValueError):
    """A profile operation refused: missing, a name collision, or protected."""


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
            # A unique dir, not name-derived: a rename keeps its stored
            # user_data_dir (cookies survive), so a name-derived dir would let a
            # later profile created with the OLD name reattach to the renamed
            # profile's cookie jar. A per-profile suffix makes that impossible.
            udd = self.root / f"{_safe(name)}-{secrets.token_hex(4)}"
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

    def ensure_default(self, *, default_country: str, default_region: str) -> Profile:
        """Guarantee the DEFAULT profile exists, seeding a fresh one if not.

        If "Default" already exists, nothing changes. Otherwise a fresh empty
        "Default" is seeded. There is deliberately no migration of a legacy
        "agent" profile — a clean Default is fine, and if the old cookies are
        ever wanted the "agent" profile is still selectable in the manager.
        Called at startup; the caller keeps it non-fatal so a bad profiles file
        can never crash boot.
        """
        with self._lock:
            if DEFAULT_PROFILE in self._cache:
                return self._cache[DEFAULT_PROFILE]
            udd = self.root / f"{_safe(DEFAULT_PROFILE)}-{secrets.token_hex(4)}"
            udd.mkdir(parents=True, exist_ok=True)
            p = Profile(
                name=DEFAULT_PROFILE, country=default_country, region=default_region,
                session_token=proxy.new_session_token(),
                fingerprint_seed=secrets.randbelow(2_000_000_000) + 1,
                user_data_dir=str(udd),
            )
            self._cache[DEFAULT_PROFILE] = p
            self._flush()
            return p

    def rename(self, old: str, new: str) -> Profile:
        """Change a profile's display name (and its key), keeping everything else
        — user_data_dir, session token, fingerprint — so cookies/logins survive.
        The in-use guard lives in the route (it reads instances.running)."""
        new = new.strip()
        if not new:
            raise ProfileError("a profile name cannot be empty")
        with self._lock:
            if old not in self._cache:
                raise ProfileError(f"there is no profile named {old!r}")
            if new == old:
                return self._cache[old]
            if new in self._cache:
                raise ProfileError(f"a profile named {new!r} already exists")
            p = self._cache.pop(old)
            p.name = new
            self._cache[new] = p
            self._flush()
            return p

    def set_geo(self, name: str, *, country: str, region: str) -> Profile:
        """Update a profile's proxy exit country/region. Takes effect next launch."""
        with self._lock:
            p = self._cache.get(name)
            if p is None:
                raise ProfileError(f"there is no profile named {name!r}")
            p.country = country
            p.region = region
            self._flush()
            return p

    def delete(self, name: str) -> bool:
        """Remove a profile and DESTROY its cookie jar (rmtree its user_data_dir).

        The DEFAULT profile is refused here as a hard invariant. The in-use guard
        lives in the route (it needs instances.running). Returns False if there was
        no such profile."""
        if name == DEFAULT_PROFILE:
            raise ProfileError("the Default profile cannot be deleted")
        with self._lock:
            p = self._cache.pop(name, None)
            if p is None:
                return False
            self._flush()
        # rmtree after dropping the cache entry (and outside the lock): the profile
        # is already unreachable, so no launch can reattach to the dir we delete.
        shutil.rmtree(p.user_data_dir, ignore_errors=True)
        return True

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
