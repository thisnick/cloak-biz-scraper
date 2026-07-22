"""A lease pool of reusable task browser identities.

The problem this fixes: a sweep used to launch on ``serp-<url-path>`` — a fresh
durable profile per unique URL path, minted at launch time and never cleaned up.
Scrape a hundred different searches and you have a hundred profiles on the volume
forever, each with its own cookie jar warming up from cold every time.

Instead, sweeps draw from a small, bounded set of pooled identities — ``task-1``,
``task-2``, … — leased for the life of one sweep and returned when it ends, then
handed to the next sweep warm. The pool self-limits without any garbage
collection: the instance pool already caps concurrent tasks at ``task_budget``
(max_instances − interactive_reserve), so at most that many leases are ever held
at once, so the pool never mints more than ``task_budget`` profiles. A released
profile is reused, not deleted — its cookies and warmth are the point.

Leases are in-memory: they record which profile is *busy right now*, nothing
more. A process restart resets them all to free, which is correct — nothing is
running then. The profiles themselves are durable (ProfileStore, on the volume).

The pool holds its OWN lease authority. It reads nothing from the instance
manager's reservation set; two pooled profiles can never collide because the
pool alone decides which name each sweep gets.
"""
from __future__ import annotations

import threading

from .profiles import ProfileStore

# Pooled identities are ``task-1``, ``task-2``, … . The prefix is the one place
# the naming lives; the Profiles UI keys its "auto" tag off it, and acquire mints
# and reuses against it.
TASK_PROFILE_PREFIX = "task-"


def is_task_profile(name: str) -> bool:
    """True for a name the pool mints (``task-1``, ``task-2``, …).

    Deliberately strict: the suffix must be a canonical positive ASCII integer,
    so a user's own profile that merely starts with "task-" (e.g. "task-force")
    is never mistaken for a pooled one. The ``isascii()`` guard is load-bearing,
    not decorative: ``str.isdigit()`` is true for characters like "²" that then
    raise ValueError under ``int()``. Profile names are not charset-checked, so a
    user could create ``task-²`` — and because ``acquire`` scans every profile
    through this predicate, letting that raise would break every sweep. Guarding
    on ``isascii()`` first (it short-circuits before ``int()``) returns a plain
    False instead.
    """
    if not name.startswith(TASK_PROFILE_PREFIX):
        return False
    suffix = name[len(TASK_PROFILE_PREFIX):]
    if not (suffix.isascii() and suffix.isdigit()):
        return False
    return suffix == str(int(suffix)) and int(suffix) >= 1


def _number(name: str) -> int:
    return int(name[len(TASK_PROFILE_PREFIX):])


class TaskProfilePool:
    """Hand out reusable ``task-N`` profiles, one lease per sweep.

    Thread-safe under its own lock. Every public method takes the lock for the
    whole operation and never awaits while holding it, so acquire/release are
    atomic against each other whether called from the event loop or a thread.
    """

    def __init__(self, profiles: ProfileStore, settings) -> None:
        self._profiles = profiles
        self._settings = settings
        self._lock = threading.Lock()
        # A task may hold more than one lease at once (within-task parallelism
        # later needs no redesign), so leases are a list per task_id. today a
        # sweep takes exactly one.
        self._leases: dict[str, list[str]] = {}
        # The flat set of every profile name leased right now — the pool's lease
        # authority, independent of the instance manager's reservations.
        self._leased: set[str] = set()

    def acquire(self, task_id: str) -> str:
        """Lease a pooled profile to ``task_id`` and return its name.

        Reuses the lowest-numbered existing pool profile that is not currently
        leased; only when every existing pool profile is leased does it mint the
        next one via ``get_or_create``. Never the Default profile, never a random
        name — always ``task-<n>`` for the smallest free ``n``.
        """
        settings = self._settings.load()
        with self._lock:
            existing = {
                _number(p.name)
                for p in self._profiles.all()
                if is_task_profile(p.name)
            }
            # Reuse the lowest free existing profile — warmth is the whole point.
            for n in sorted(existing):
                name = f"{TASK_PROFILE_PREFIX}{n}"
                if name not in self._leased:
                    return self._grant(task_id, name)
            # Every existing pool profile is leased. Mint the smallest missing
            # number, filling a hole left by a deleted free profile before
            # extending past the current high-water mark. Bounded by task_budget
            # because at most that many leases are ever held at once.
            n = 1
            while n in existing:
                n += 1
            name = f"{TASK_PROFILE_PREFIX}{n}"
            self._profiles.get_or_create(
                name,
                default_country=settings.proxy_country,
                default_region=settings.proxy_region,
            )
            return self._grant(task_id, name)

    def release(self, task_id: str) -> None:
        """Free every profile leased to ``task_id``.

        Idempotent: a task that never acquired (or already released) is a no-op,
        so a sweep's ``finally`` can call this unconditionally.
        """
        with self._lock:
            for name in self._leases.pop(task_id, []):
                self._leased.discard(name)

    def leased_by(self, task_id: str) -> list[str]:
        """The profiles ``task_id`` currently holds. For tests and introspection."""
        with self._lock:
            return list(self._leases.get(task_id, []))

    def _grant(self, task_id: str, name: str) -> str:
        self._leased.add(name)
        self._leases.setdefault(task_id, []).append(name)
        return name
