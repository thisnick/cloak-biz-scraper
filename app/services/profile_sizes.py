"""How much disk a profile's saved browser data is using.

Each profile owns one user_data_dir, so its size is a walk of one directory —
and a warm Chromium profile is hundreds of megabytes across thousands of small
files. That is far too slow to do while rendering the settings page, which also
carries the licence, proxy, and Notion configuration and must always render.

So the measurement is never taken during a render. It runs off the event loop in
a worker thread, is cached with the time it was taken, and the page fetches it
after it paints. Everything here is best effort by design: the directory being
measured may belong to a browser that is open and writing to it.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .profiles import ProfileStore


def measure_dir(path: str | Path) -> tuple[int, int]:
    """Total bytes and file count for one directory tree.

    The count is the diagnostic that bytes alone hide: "180 MB across 240,000
    files" is a cache that wants clearing, not a big download.

    Never raises. An open profile is written to WHILE this walks it, so files
    disappear between being listed and being stat'd; those are skipped and the
    walk continues. A missing directory is (0, 0), not an error — the settings
    page must render regardless.

    Symlinks are never followed: not into a directory (``followlinks=False``)
    and not through a file (``follow_symlinks=False`` on the stat), so a link
    pointing out of the profiles root can neither be walked nor counted.
    """
    total = 0
    files = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=None, followlinks=False):
        for filename in filenames:
            try:
                size = os.stat(
                    os.path.join(dirpath, filename), follow_symlinks=False
                ).st_size
            except OSError:  # FileNotFoundError included: it vanished mid-walk
                continue
            total += size
            files += 1
    return total, files


def _contained(root: Path, user_data_dir: str) -> bool:
    """Is this stored path really inside the profiles root?

    Measuring is read-only, so this is not the destructive path's guard — but a
    tampered profiles.json should not be able to point the walk at ``/``, and
    the honest answer for a path we decline to walk is zero, not an exception.
    """
    try:
        Path(user_data_dir).resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class ProfileSize:
    """One measurement of one profile, and when it was taken (unix seconds)."""

    name: str
    bytes: int
    files: int
    measured_at: float


class ProfileSizes:
    """Cached, off-the-event-loop disk usage for every profile.

    The store is reached through a callable rather than captured: the profile
    store is swapped at runtime (and under tests), and a captured one would
    quietly report sizes for profiles nobody can see any more.
    """

    def __init__(self, store: Callable[[], "ProfileStore"]) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._cache: dict[str, ProfileSize] = {}

    def invalidate(self, name: str) -> None:
        """Forget a cached measurement — the profile was cleared or deleted, so
        the number the page is showing is now a lie."""
        with self._lock:
            self._cache.pop(name, None)

    def cached(self, name: str) -> ProfileSize | None:
        """The stored measurement, if this profile has one. Never measures."""
        with self._lock:
            return self._cache.get(name)

    async def snapshot(self, *, refresh: bool = False) -> list[ProfileSize]:
        """Every profile's size, walking only what is not already cached.

        Repeat visits to the settings page therefore cost nothing. ``refresh``
        re-measures everything, for callers that need a figure they know is
        current. Two overlapping calls may both walk the same tree; that is
        wasted work, never a wrong answer, and it is not worth an asyncio lock
        whose event-loop affinity would outlive the request that created it.
        """
        store = self._store()
        profiles = {p.name: p.user_data_dir for p in store.all()}
        with self._lock:
            # Without this, a deleted profile's entry would live forever.
            for gone in set(self._cache) - set(profiles):
                self._cache.pop(gone, None)
            pending = {
                name: udd for name, udd in profiles.items()
                if refresh or name not in self._cache
            }
        if pending:
            measured = await asyncio.to_thread(self._measure, store.root, pending)
            with self._lock:
                self._cache.update(measured)
        with self._lock:
            return [self._cache[n] for n in sorted(profiles) if n in self._cache]

    @staticmethod
    def _measure(root: Path, pending: dict[str, str]) -> dict[str, ProfileSize]:
        """Walk each pending profile. Runs in a worker thread, never the loop."""
        now = time.time()
        measured: dict[str, ProfileSize] = {}
        for name, user_data_dir in pending.items():
            total, files = (
                measure_dir(user_data_dir) if _contained(root, user_data_dir) else (0, 0)
            )
            measured[name] = ProfileSize(
                name=name, bytes=total, files=files, measured_at=now
            )
        return measured
