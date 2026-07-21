"""Persistent profile store.

Ported from browserd (app/profiles.py). The only change: geo defaults are passed
in from settings rather than read from the environment.

A profile is a durable browser identity: a name, optional proxy geo pin
(country/region), sticky Evomi session token, and fingerprint seed — all paired
for the life of the profile per CloakBrowser guidance ("one profile, one seed").
Cookies live in the profile's user-data dir. Stored as one JSON file under the
profiles dir so a later instance reattaches warm after the container restarts.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..models import ProfileDeleteResult, ProfileView
from . import proxy

if TYPE_CHECKING:
    from .instances import InstanceManager
    from .settings import SettingsService

# The one profile the interactive paths (create_instance, "+ New browser") use
# unless overridden, so cookies/logins stay consistent and casual users never
# think about it. Sweeps deliberately do NOT use it — a sweep's cookies and any
# block on its exit IP must not land in the identity the user is logged in with.
DEFAULT_PROFILE = "Default"


class ProfileError(ValueError):
    """A profile operation refused: missing, a name collision, or protected."""


class ProfileNotFound(ProfileError):
    """The named profile does not exist."""


class ProfileInUse(ProfileError):
    """A queued, opening, open, or closing browser owns this profile."""


class ProfileConflict(ProfileError):
    """The requested change conflicts with profile or proxy state."""


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

    def _create_unlocked(
        self, name: str, *, default_country: str, default_region: str,
        country: str | None = None, region: str | None = None,
    ) -> Profile:
        udd = self.root / f"{_safe(name)}-{secrets.token_hex(4)}"
        udd.mkdir(parents=True, exist_ok=True)
        profile = Profile(
            name=name,
            country=country or default_country,
            region=region or default_region,
            session_token=proxy.new_session_token(),
            fingerprint_seed=secrets.randbelow(2_000_000_000) + 1,
            user_data_dir=str(udd),
        )
        self._cache[name] = profile
        self._flush()
        return profile

    def create(
        self,
        name: str,
        *,
        default_country: str,
        default_region: str,
        country: str | None = None,
        region: str | None = None,
    ) -> Profile:
        """Create explicitly; unlike launch-time get_or_create, collisions fail."""
        name = name.strip()
        if not name:
            raise ProfileError("a profile name cannot be empty")
        with self._lock:
            if name in self._cache:
                raise ProfileConflict(f"a profile named {name!r} already exists")
            return self._create_unlocked(
                name,
                default_country=default_country,
                default_region=default_region,
                country=country,
                region=region,
            )

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
            return self._create_unlocked(
                name,
                default_country=default_country,
                default_region=default_region,
                country=country,
                region=region,
            )

    def get(self, name: str) -> Profile | None:
        """Return a detached snapshot, never the mutable cached identity."""
        with self._lock:
            profile = self._cache.get(name)
            return Profile(**asdict(profile)) if profile is not None else None

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
        The application-level service owns the in-use guard."""
        return self.update(old, new_name=new)

    def update(
        self,
        name: str,
        *,
        new_name: str | None = None,
        country: str | None = None,
        region: str | None = None,
    ) -> Profile:
        """Atomically rename and/or change geography with one index rewrite."""
        target_name = new_name.strip() if new_name is not None else name
        if not target_name:
            raise ProfileError("a profile name cannot be empty")
        with self._lock:
            profile = self._cache.get(name)
            if profile is None:
                raise ProfileNotFound(f"there is no profile named {name!r}")
            if name == DEFAULT_PROFILE and target_name != name:
                raise ProfileConflict("the Default profile cannot be renamed")
            if target_name != name and target_name in self._cache:
                raise ProfileConflict(f"a profile named {target_name!r} already exists")
            if target_name != name:
                self._cache.pop(name)
                profile.name = target_name
                self._cache[target_name] = profile
            if country is not None:
                profile.country = country
            if region is not None:
                profile.region = region
            self._flush()
            return profile

    def set_geo(self, name: str, *, country: str, region: str) -> Profile:
        """Update a profile's proxy exit country/region. Takes effect next launch."""
        return self.update(name, country=country, region=region)

    def delete(self, name: str) -> bool:
        """Remove a profile and DESTROY its cookie jar (rmtree its user_data_dir).

        The DEFAULT profile is refused here as a hard invariant. The application
        service owns the in-use guard. Returns False if there was no such profile.
        A corrupt stored path is refused before changing the index:
        deletion may only target a strict descendant of the profiles root."""
        if name == DEFAULT_PROFILE:
            raise ProfileError("the Default profile cannot be deleted")
        with self._lock:
            p = self._cache.get(name)
            if p is None:
                return False
            try:
                root = self.root.resolve()
                target = Path(p.user_data_dir).resolve()
                target.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ProfileError(
                    f"refusing to delete {name!r}: its data directory is outside "
                    "the profiles root"
                ) from exc
            if target == root:
                raise ProfileError(
                    f"refusing to delete {name!r}: its data directory is the "
                    "profiles root"
                )
            self._cache.pop(name)
            self._flush()
        # rmtree after dropping the cache entry (and outside the lock): the profile
        # is already unreachable, so no launch can reattach to the dir we delete.
        # Delete the already-checked canonical path, never the untrusted stored path.
        shutil.rmtree(target, ignore_errors=True)
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
            # Never hand live mutable cache objects to a serializer. A rotate or
            # update on another thread must not change a supposedly captured
            # safe view between two fields being read.
            return [Profile(**asdict(profile)) for profile in self._cache.values()]


class ProfileService:
    """The one profile-management policy used by UI, REST, and MCP.

    InstanceManager owns the lifecycle lock because it knows about browsers
    before they reach ``running``. Destructive operations take that same lock,
    closing the launch-time race that a route-level scan cannot see.
    """

    def __init__(
        self,
        instances: "InstanceManager",
        settings: "SettingsService | Callable[[], SettingsService]",
    ) -> None:
        self._instances = instances
        self._get_settings = settings if callable(settings) else lambda: settings

    def _view(self, profile: Profile, *, in_use: bool) -> ProfileView:
        return ProfileView(
            name=profile.name,
            country=profile.country,
            region=profile.region,
            is_default=profile.name == DEFAULT_PROFILE,
            in_use=in_use,
            proxy_configured=self._get_settings().load().proxy_configured(),
        )

    async def list_profiles(self) -> list[ProfileView]:
        async with self._instances.profile_guard():
            busy = self._instances.profile_names_in_use()
            return [
                self._view(profile, in_use=profile.name in busy)
                for profile in sorted(self._instances.profiles.all(), key=lambda p: p.name)
            ]

    async def create_profile(
        self, name: str, *, country: str | None = None, region: str | None = None,
    ) -> ProfileView:
        settings = self._get_settings().load()
        async with self._instances.profile_guard(name.strip(), require_idle=True):
            profile = self._instances.profiles.create(
                name,
                default_country=settings.proxy_country,
                default_region=settings.proxy_region,
                country=country,
                region=region,
            )
            return self._view(profile, in_use=False)

    async def ensure_profile(
        self, name: str, *, country: str | None = None, region: str | None = None,
    ) -> ProfileView:
        """Idempotently ensure a profile for the existing settings-page flow.

        Public programmatic create is deliberately collision-sensitive. The UI
        predates that contract and treats submitting an existing name as a
        successful selection, so it keeps launch-time ``get_or_create``
        semantics while still going through the lifecycle coordinator.
        """
        name = name.strip()
        if not name:
            raise ProfileError("a profile name cannot be empty")
        settings = self._get_settings().load()
        async with self._instances.profile_guard(name):
            profile = self._instances.profiles.get_or_create(
                name,
                default_country=settings.proxy_country,
                default_region=settings.proxy_region,
                country=country,
                region=region,
            )
            return self._view(
                profile, in_use=profile.name in self._instances.profile_names_in_use()
            )

    async def update_profile(
        self,
        name: str,
        *,
        new_name: str | None = None,
        country: str | None = None,
        region: str | None = None,
    ) -> ProfileView:
        if new_name is None and country is None and region is None:
            raise ProfileError("provide new_name, country, or region to update the profile")
        renaming = new_name is not None and new_name.strip() != name
        guarded_names = (name, new_name.strip()) if renaming and new_name is not None else (name,)
        async with self._instances.profile_guard(*guarded_names, require_idle=renaming):
            profile = self._instances.profiles.update(
                name, new_name=new_name, country=country, region=region,
            )
            return self._view(
                profile, in_use=profile.name in self._instances.profile_names_in_use()
            )

    async def new_proxy_session(self, name: str) -> ProfileView:
        async with self._instances.profile_guard(name):
            # Missing stays explicit even in direct mode. Otherwise the same
            # bad name would appear to mean two different things depending on
            # an unrelated server setting.
            if self._instances.profiles.get(name) is None:
                raise ProfileNotFound(f"there is no profile named {name!r}")
            settings = self._get_settings().load()
            if not settings.proxy_configured():
                if settings.proxy_status() == "direct":
                    raise ProfileConflict(
                        "no proxy is configured; this server is using direct mode, so there "
                        "is no proxy session to replace"
                    )
                raise ProfileConflict(
                    "proxy settings are incomplete; complete or clear them before requesting "
                    "a new proxy session"
                )
            profile = self._instances.profiles.rotate_session(name)
            # The profile cannot disappear while profile_guard owns the same
            # lifecycle lock used by deletion; keep the check as a hard guard
            # against a future store implementation violating that invariant.
            if profile is None:  # pragma: no cover - defensive invariant
                raise ProfileNotFound(f"there is no profile named {name!r}")
            return self._view(
                profile, in_use=profile.name in self._instances.profile_names_in_use()
            )

    async def delete_profile(self, name: str) -> ProfileDeleteResult:
        async with self._instances.profile_guard(name, require_idle=True):
            if not self._instances.profiles.delete(name):
                raise ProfileNotFound(f"there is no profile named {name!r}")
            return ProfileDeleteResult(name=name)
