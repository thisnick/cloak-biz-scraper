"""What this container can physically run — memory ceiling, thread ceiling.

The pool budget (``max_instances``) is a number the user types, and nothing
stops them typing one the container cannot honour. When they do, the failure is
cryptic and late: a launch dies at the OS with ``pthread_create: Resource
temporarily unavailable`` (out of threads), or six browsers rendering heavy
pages at once exhaust memory and Chromium reports ``Page crashed`` mid-sweep —
sometimes taking the whole container down with it.

Each concurrent browser here is not a tab: it is a CloakBrowser (patched
Chromium, dozens of threads) *plus its own Xvnc display*, hundreds of MB each.
So the honest ceiling is small — around one browser per gigabyte — and this
module reads the container's real limits so the app can warn *before* the launch
that would fail, naming a number instead of leaving the user to discover it in a
stack trace.

Everything here degrades to ``None`` rather than raising: on a host without
cgroups or ``/proc`` (a developer's macOS, say) the limits are simply unknown,
and an unknown ceiling must not crash a status page or block a launch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("cloakbiz.capacity")

# Memory to budget per concurrent browser. Deliberately ~1 GB: a CloakBrowser
# plus its own Xvnc display is already hundreds of MB idle, and while it is
# actively rendering a heavy listing page its renderer can approach a gigabyte —
# which is exactly the moment sweeps run several at once. Budgeting a round GB
# apiece keeps the total under the container limit with the app's own baseline
# absorbed by the fractional slack. Calibration against the observed ceiling: a
# ~4 GB container recommends floor(4/1) = 4, matching the live finding that ~6-7
# idle browsers is the ceiling and ~4 is safe once they are all rendering. It
# also leaves the shipped default (max_instances=4) unwarned on a 4 GB box —
# the default should be quiet on a correctly sized container and speak up only
# when someone raises the pool past what the memory allows.
PER_BROWSER_GB = 1.0

# cgroup v1 writes a huge sentinel for "unlimited" instead of a word. Anything
# at or above this is not a real bound; fall through to the next source.
_UNLIMITED = 2 ** 62

_CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_MEMINFO = Path("/proc/meminfo")
_CGROUP_V2_PIDS = Path("/sys/fs/cgroup/pids.max")


@dataclass(frozen=True)
class Capacity:
    """The container's detected ceilings. Any field may be ``None`` (unknown)."""

    memory_limit_bytes: int | None = None
    # The pid/thread ceiling (cgroup ``pids.max``), a secondary signal. The
    # ``pthread_create`` launch failure is a thread-exhaustion failure, so this
    # is the limit that bites first on a container with generous memory but a
    # tight process cap. Surfaced but not (yet) used to compute the estimate,
    # which is memory-based.
    pids_max: int | None = None

    @property
    def memory_limit_gb(self) -> float | None:
        if self.memory_limit_bytes is None:
            return None
        return self.memory_limit_bytes / (1024 ** 3)

    def recommended_max_browsers(self) -> int | None:
        """The most browsers this container should be asked to run, or ``None``.

        ``None`` means the memory ceiling could not be read, so no estimate can
        be made — callers fall back to their prior behaviour rather than warn on
        a guess.
        """
        gb = self.memory_limit_gb
        if gb is None:
            return None
        return max(1, int(gb // PER_BROWSER_GB))


def _read_cgroup_limit(path: Path) -> int | None:
    """A cgroup memory-limit file as an int, or ``None`` if absent/unlimited."""
    try:
        raw = path.read_text().strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0 or value >= _UNLIMITED:
        return None
    return value


def _read_meminfo_total(path: Path) -> int | None:
    """``MemTotal`` from ``/proc/meminfo`` in bytes, or ``None``."""
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            # Format: "MemTotal:  16384000 kB"
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def _read_pids_max(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def detect_memory_limit_bytes(
    *,
    cgroup_v2: Path = _CGROUP_V2_MAX,
    cgroup_v1: Path = _CGROUP_V1_MAX,
    meminfo: Path = _MEMINFO,
) -> int | None:
    """The container's memory ceiling in bytes, or ``None`` if unreadable.

    Prefers the cgroup limit (what the container is actually capped at) over
    physical memory (what the host has): cgroup v2 first, then v1, then
    ``/proc/meminfo`` as a last resort. Paths are injectable for tests.
    """
    v2 = _read_cgroup_limit(cgroup_v2)
    if v2 is not None:
        return v2
    v1 = _read_cgroup_limit(cgroup_v1)
    if v1 is not None:
        return v1
    return _read_meminfo_total(meminfo)


def detect_capacity(
    *,
    cgroup_v2: Path = _CGROUP_V2_MAX,
    cgroup_v1: Path = _CGROUP_V1_MAX,
    meminfo: Path = _MEMINFO,
    pids_max: Path = _CGROUP_V2_PIDS,
) -> Capacity:
    """Read every ceiling this module knows about. Never raises."""
    return Capacity(
        memory_limit_bytes=detect_memory_limit_bytes(
            cgroup_v2=cgroup_v2, cgroup_v1=cgroup_v1, meminfo=meminfo,
        ),
        pids_max=_read_pids_max(pids_max),
    )
