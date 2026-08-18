"""Task history on the volume: the run records, and the evidence each captured.

A sweep's record is a JSON file of a few hundred KB. What it *captured* — a
screenshot and the page HTML for every attempt on every source — lives under
``evidence/<job_id>/`` and is what actually fills a volume. Nothing ever removed
it: retention pruning dropped the record alone, which did not just leave the
captures behind but **orphaned** them, since ``/runs/<job_id>/evidence/...`` is
gated on the record still existing. So the volume accumulated files that no
longer had a page that could show them.

Two things follow. Removal goes through ``JobStore.drop``, which removes a
record and its evidence together and refuses a working job. And the measurement
counts the orphans that are already there — a server that has been running for
months is mostly holding evidence for sweeps whose records were pruned long ago,
and a number that quietly excluded them would understate the problem it exists
to explain.

Everything here is best effort and off the event loop, exactly like the profile
sizes: the settings page must render whether or not any of it works.
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
    from .jobs import JobStore

logger = logging.getLogger("cloakbiz.history")


@dataclass(frozen=True)
class HistoryView:
    """What task history is costing, and when that was measured."""

    runs: int
    bytes: int
    files: int
    orphans: int  # evidence directories with no run record left to reach them
    measured_at: float


@dataclass(frozen=True)
class ClearedHistory:
    """What a clear actually removed — never what it set out to remove."""

    runs: int
    orphans: int
    bytes: int
    kept: int  # runs still working, left exactly as they were


class TaskHistory:
    """Cached, off-the-event-loop size of the task history, and its clear.

    The job store is reached through a callable for the same reason ProfileSizes
    reaches the profile store through one: it is swapped at runtime and under
    tests. The evidence root is read off that store rather than from config, so
    the measurement and the removal can never disagree about where evidence is.
    """

    def __init__(self, jobs: "JobStore | Callable[[], JobStore]") -> None:
        self._get_jobs = jobs if callable(jobs) else lambda: jobs
        self._lock = threading.Lock()
        self._cache: HistoryView | None = None

    def invalidate(self) -> None:
        """Forget the measurement — history was cleared, so the number the page
        is showing is now a lie."""
        with self._lock:
            self._cache = None

    async def snapshot(self, *, refresh: bool = False) -> HistoryView:
        """Runs, evidence bytes, and how much of it is orphaned.

        Cached because it is a walk of every screenshot on the volume, and a
        page visit must not pay for that twice.
        """
        with self._lock:
            cached = self._cache
        if cached is not None and not refresh:
            return cached
        view = await asyncio.to_thread(self._measure)
        with self._lock:
            self._cache = view
        return view

    async def clear(self) -> ClearedHistory:
        """DESTROY every finished run and everything it captured.

        A run that is still working is kept, record and evidence both — that is
        ``drop``'s own guard, re-read per job at the moment of removal rather
        than from the listing this started with, so a sweep that began while
        this was running is never half-deleted underneath itself.
        """
        cleared = await asyncio.to_thread(self._clear)
        self.invalidate()
        logger.info(
            "cleared %d run(s) and %d orphaned evidence director(ies), freeing %d bytes; "
            "%d working run(s) kept",
            cleared.runs, cleared.orphans, cleared.bytes, cleared.kept,
        )
        return cleared

    # ── worker-thread bodies ─────────────────────────────────────────────────

    def _measure(self) -> HistoryView:
        store = self._get_jobs()
        ids = _record_ids(store)
        total = files = orphans = 0
        for entry in reclaim.children(store.evidence_root):
            size, count = _size_of(entry)
            total += size
            files += count
            if entry.name not in ids:
                orphans += 1
        return HistoryView(
            runs=len(ids), bytes=total, files=files, orphans=orphans,
            measured_at=time.time(),
        )

    def _clear(self) -> ClearedHistory:
        store = self._get_jobs()
        root = Path(store.evidence_root)
        runs = kept = orphans = freed = 0

        for job in store.all():
            if job.status == "working":
                kept += 1
                continue
            size = _size_of(root / job.id)[0]
            if store.drop(job.id):
                runs += 1
                freed += size

        # The orphans: every directory whose record was pruned months ago, plus
        # the `archive/` tree left by the versions of `archive_page` that filed
        # captures per URL instead of per task. An archive writes under its own
        # task id now, so it is removed with its record like any other run — but
        # what is already on the volume still has no record to reach it, and this
        # is the only thing that ever takes it away.
        # The live set is re-read HERE, not reused from above: a sweep that
        # started while the loop was running has a record and a directory it is
        # writing to right now, and neither is an orphan.
        live = _record_ids(store)
        for entry in reclaim.children(root):
            if entry.name in live:
                continue
            size = _size_of(entry)[0]
            try:
                gone = reclaim.remove_child(root, entry)
            except reclaim.Unsafe as exc:
                logger.warning("left the evidence directory %s alone: %s", entry.name, exc)
                continue
            if not gone:
                logger.warning("could not fully remove the evidence directory %s", entry.name)
                continue
            orphans += 1
            freed += size

        return ClearedHistory(runs=runs, orphans=orphans, bytes=freed, kept=kept)


def _record_ids(store: "JobStore") -> set[str]:
    """The ids of the records on disk, from their filenames alone.

    Deliberately not ``store.all()``: this needs the ids, not the jobs, and
    parsing 500 sweep results to count directories would make measuring cost
    more than the walk it exists to explain.
    """
    try:
        return {path.stem for path in store.root.glob("*.json")}
    except OSError:  # pragma: no cover - a volume that cannot be listed
        return set()


def _size_of(entry: Path) -> tuple[int, int]:
    """Bytes and files under one evidence directory.

    A symlink is reported as nothing at all: ``os.walk`` follows a link handed
    to it as the top of a tree even with ``followlinks=False``, so measuring one
    would count somebody else's disk as ours.
    """
    return (0, 0) if entry.is_symlink() else measure_dir(entry)
