"""Old browser builds on the volume: what they cost, and removing them.

The cloakbrowser package caches one directory **per version** —
``<cache>/chromium-<version>`` for the public build, ``…-pro`` for Pro — and
nothing ever removes one. The version is normally unpinned ("track latest"), so
every CloakBrowser release downloads a new directory next to the last one and
the previous build stays forever. One installed Pro build measured 747 MB
against a 5 GB volume, which is how a server fills up without anybody doing
anything: the leak is a feature of tracking latest.

Removing them is safe — the package downloads whatever it needs on the next
launch — but only if we remove the right ones, so **which build is in use is
read, never guessed**. ``settings.binary_last_path`` is the absolute path
``ensure_binary()`` last resolved, persisted across restarts; its parent is the
directory in use. When that is not readable this refuses to delete anything at
all rather than reason about which of several directories is probably current.
Measuring stays available in every case: a walk is read-only, and a user staring
at a full volume should still be told what is on it.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import reclaim
from .profile_sizes import measure_dir

if TYPE_CHECKING:
    from .instances import InstanceManager
    from .settings import SettingsService

logger = logging.getLogger("cloakbiz.binaries")

# The package's own layout (cloakbrowser/config.py get_binary_dir): one
# directory per version, named chromium-<version>, with "-pro" appended for the
# licensed build. Both facts are read back out of the directory name here — the
# same convention license.is_pro() relies on.
PREFIX = "chromium-"

# Why we will not delete anything. Each is shown to the user as-is, so each says
# what to do next rather than only what went wrong.
_UNRECORDED = (
    "This server has not recorded which browser build it is running yet, so nothing will "
    "be removed — open Browser licence above, click Save & verify, then come back."
)
_ELSEWHERE = (
    "The browser build this server recorded is not in this cache directory, so nothing "
    "here can be identified as the one in use — and nothing will be removed."
)
_MISSING = (
    "The browser build this server recorded is no longer on the volume, so the build in "
    "use cannot be identified — and nothing will be removed. Click Save & verify under "
    "Browser licence to resolve one again."
)
_UNREADABLE = (
    "This server's saved settings could not be read, so the browser build in use cannot "
    "be identified — and nothing will be removed."
)


class BuildsError(RuntimeError):
    """A reclaim of old browser builds refused."""


class BuildsUnknown(BuildsError):
    """We cannot say which build is in use, so nothing may be removed."""


class BuildsBusy(BuildsError):
    """A browser is running: a live Chromium has its binary mapped."""


@dataclass(frozen=True)
class BrowserBuild:
    """One cached ``chromium-*`` directory."""

    name: str
    version: str
    pro: bool
    bytes: int
    files: int
    in_use: bool


@dataclass(frozen=True)
class BuildsView:
    """Every cached build, and whether we know which one is running.

    ``stale`` is deliberately empty whenever the in-use build is unknown: the
    page must never offer to remove "the other ones" when we cannot say which
    one that excludes.
    """

    cache_dir: str
    builds: list[BrowserBuild]
    in_use_known: bool
    reason: str  # why the in-use build is unknown; "" when it is known
    measured_at: float

    @property
    def in_use(self) -> BrowserBuild | None:
        return next((b for b in self.builds if b.in_use), None)

    @property
    def stale(self) -> list[BrowserBuild]:
        return [b for b in self.builds if not b.in_use] if self.in_use_known else []

    @property
    def stale_bytes(self) -> int:
        return sum(b.bytes for b in self.stale)

    @property
    def total_bytes(self) -> int:
        return sum(b.bytes for b in self.builds)


@dataclass(frozen=True)
class RemovedBuilds:
    """What a reclaim actually did — never what it intended to do."""

    removed: list[str]
    bytes: int
    kept: str


class BrowserBuilds:
    """Cached, off-the-event-loop disk usage for the browser cache, and the
    reclaim that follows from it.

    Settings are reached through a callable for the same reason ProfileSizes
    reaches the profile store through one: the service is swapped at runtime and
    under tests, and a captured one would report on a store nobody is using.
    """

    def __init__(
        self,
        cache_dir: Path | str,
        settings: "SettingsService | Callable[[], SettingsService]",
        instances: "InstanceManager",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self._get_settings = settings if callable(settings) else lambda: settings
        self._instances = instances
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[int, int]] = {}  # dir name -> (bytes, files)
        self._measured_at = 0.0

    # ── what is in use ───────────────────────────────────────────────────────

    def in_use(self) -> tuple[str, str]:
        """``(directory name of the running build, why it is unknown)``.

        One source and no fallback: the path ``ensure_binary()`` last resolved.
        Everything else about a cached directory — its version, its mtime, how
        new it looks — is a guess, and the cost of guessing wrong is deleting
        the binary the server runs on.

        The recorded path is untrusted input like any other stored path, so it
        is resolved and required to sit inside this cache. What is returned is
        the FIRST component below the cache root — not the binary's parent —
        because how deep the executable sits is the package's business and it
        varies by platform: ``chromium-<v>/chrome`` on Linux, but
        ``chromium-<v>/Chromium.app/Contents/MacOS/Chromium`` on macOS. The
        version directory is the top of that path either way.

        A path pointing somewhere else does not make every directory here stale;
        it makes the question unanswerable, which is a refusal.
        """
        try:
            stored = self._get_settings().load().binary_last_path
        except Exception:  # noqa: BLE001 — an unreadable store is a refusal, not a crash
            logger.warning("could not read the recorded browser build", exc_info=True)
            return "", _UNREADABLE
        if not stored:
            return "", _UNRECORDED
        try:
            root = self.cache_dir.resolve()
            relative = Path(stored).resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return "", _ELSEWHERE
        if not relative.parts or not relative.parts[0].startswith(PREFIX):
            return "", _ELSEWHERE
        name = relative.parts[0]
        if not (root / name).is_dir():
            return "", _MISSING
        return name, ""

    # ── measuring ────────────────────────────────────────────────────────────

    def _names(self) -> list[str]:
        """Every cached build directory, by name. Anything that is not a
        ``chromium-*`` directory is not a build and is never a candidate."""
        return [
            entry.name
            for entry in reclaim.children(self.cache_dir)
            if entry.name.startswith(PREFIX) and (entry.is_symlink() or entry.is_dir())
        ]

    def _size(self, name: str) -> tuple[int, int]:
        entry = self.cache_dir / name
        # os.walk(followlinks=False) still walks a link handed to it as the top
        # of the tree, so a symlinked entry would measure somebody else's disk.
        return (0, 0) if entry.is_symlink() else measure_dir(entry)

    def _measure(self, pending: list[str]) -> dict[str, tuple[int, int]]:
        """Walk each pending directory. Runs in a worker thread, never the loop."""
        return {name: self._size(name) for name in pending}

    async def snapshot(self, *, refresh: bool = False) -> BuildsView:
        """Every cached build with its size, walking only what is not cached.

        A build directory never changes after it is unpacked, so the cache here
        is not a staleness trade-off the way a live profile's is: a repeat visit
        to the settings page costs one directory listing.
        """
        in_use, reason = self.in_use()
        names = await asyncio.to_thread(self._names)
        with self._lock:
            for gone in set(self._cache) - set(names):
                self._cache.pop(gone, None)
            pending = [n for n in names if refresh or n not in self._cache]
        if pending:
            measured = await asyncio.to_thread(self._measure, pending)
            with self._lock:
                self._cache.update(measured)
                self._measured_at = time.time()
        with self._lock:
            sizes = dict(self._cache)
            measured_at = self._measured_at or time.time()
        builds = [
            BrowserBuild(
                name=name,
                version=_version(name),
                pro=name.endswith("-pro"),
                bytes=sizes.get(name, (0, 0))[0],
                files=sizes.get(name, (0, 0))[1],
                in_use=name == in_use,
            )
            for name in names
        ]
        # The one in use first: it is the row that explains the others.
        builds.sort(key=lambda b: (not b.in_use, b.name))
        return BuildsView(
            cache_dir=str(self.cache_dir),
            builds=builds,
            in_use_known=bool(in_use),
            reason=reason,
            measured_at=measured_at,
        )

    # ── reclaiming ───────────────────────────────────────────────────────────

    async def remove_stale(self) -> RemovedBuilds:
        """DESTROY every cached build except the one in use.

        Three guards, in the order that makes each of them cheap to trust:

        1. The in-use build must be known, or nothing is removed at all.
        2. No browser may be queued, opening, open, or closing — a live Chromium
           has its binary mapped, and the reclaim is taken under the same
           lifecycle lock a launch takes, so one cannot start mid-rmtree.
        3. Only a direct ``chromium-*`` child of the resolved cache directory is
           ever passed to the remover, which re-checks containment itself.
        """
        in_use, reason = self.in_use()
        if not in_use:
            raise BuildsUnknown(reason)
        async with self._instances.profile_guard():
            if self._instances.running or self._instances.profile_names_in_use():
                raise BuildsBusy(
                    "A browser is open or opening right now and is running from one of "
                    "these builds. Close every browser first, then remove the old versions."
                )
            removed, freed = await asyncio.to_thread(self._remove, in_use)
        with self._lock:
            for name in removed:
                self._cache.pop(name, None)
        if removed:
            logger.info(
                "removed %d stale browser build(s), freeing %d bytes; kept %s",
                len(removed), freed, in_use,
            )
        return RemovedBuilds(removed=removed, bytes=freed, kept=in_use)

    def _remove(self, in_use: str) -> tuple[list[str], int]:
        """The rmtree itself, in a worker thread. Never touches ``in_use``."""
        removed: list[str] = []
        freed = 0
        for name in self._names():
            if name == in_use:
                continue
            entry = self.cache_dir / name
            size = self._size(name)[0]
            try:
                gone = reclaim.remove_child(self.cache_dir, entry)
            except reclaim.Unsafe as exc:
                logger.warning("left %s alone: %s", name, exc)
                continue
            if not gone:
                logger.warning("could not fully remove the browser build %s", name)
                continue
            removed.append(name)
            freed += size
        return removed, freed


def _version(name: str) -> str:
    """The version out of a cache directory name, best effort — the layout is
    the package's business, and a version we cannot read is worth degrading
    over, never failing over."""
    from .license import _version_from_path

    return _version_from_path(name)
