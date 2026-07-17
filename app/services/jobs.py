"""Sweep jobs, on the volume.

**Why the volume and not a dict.** Railway sleeps a service after ten minutes
with no *outbound* traffic, and wakes it on *inbound*. A sweep pins the machine
by doing its job — Chromium's egress counts — but the moment it finishes, the
clock starts. The likeliest shape of a real session is: sweep ends, the agent
takes a while to come back, the container sleeps, the poll arrives and wakes it.
An in-memory registry would answer that poll from a freshly booted process that
has never heard of the job, and the results of a sweep that actually succeeded
would be gone. So a finished job must outlive the process that ran it.

**Why jobs from a previous boot are failed, not left working.** A job is
"working" because a coroutine in *this* process is running it. If the process is
gone, nothing is advancing that job and nothing ever will, so leaving the record
saying "working" would have `get_scrape_listing_results` tell an agent to keep
waiting for a sweep that died — forever, and with no error to explain it. The
poll must be able to say what happened. This is the honest reading of a status
we can no longer vouch for.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from ..models import Job

logger = logging.getLogger("cloakbiz.jobs")

# Sweeps are small (a few hundred KB of JSON at 50 cards/page) but they are not
# free, and nothing else ever deletes them. Retention is deliberately generous:
# the cost of keeping a job is a file, the cost of dropping one too early is an
# agent polling for a result that existed an hour ago.
_KEEP_DAYS = 14
_KEEP_MOST_RECENT = 500


class JobStore:
    """Job records as one JSON file each, under the volume's jobs directory."""

    def __init__(self, root: Path, boot_id: str | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Identifies this process run. Any job claiming to be "working" under a
        # different boot id is being worked on by a process that no longer exists.
        self.boot_id = boot_id or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        # Job ids are minted here, but a poll's id arrives from the outside:
        # without this a job_id of "../settings" would read straight off the
        # volume, and the store is the only place that knows ids are hex.
        if not job_id or not job_id.isalnum():
            raise ValueError(f"not a job id: {job_id!r}")
        return self.root / f"{job_id}.json"

    def create(self, *, summary=None, **fields) -> Job:
        """Write a new job down.

        `summary` may be a callable taking the new id: the message that tells a
        model how to collect this sweep has to name the job, and the id is minted
        here — so this lets the caller fill it in within the same write rather
        than saving once and immediately saving again.
        """
        job = Job(
            id=uuid.uuid4().hex[:12],
            boot_id=self.boot_id,
            created_at=time.time(),
            updated_at=time.time(),
            **fields,
        )
        if summary is not None:
            job.summary = summary(job.id) if callable(summary) else summary
        self.save(job)
        return job

    def save(self, job: Job) -> Job:
        job.updated_at = time.time()
        with self._lock:
            path = self._path(job.id)
            tmp = path.with_suffix(".json.tmp")
            # Atomic replace: a poll that reads a half-written file would report
            # a corrupt job for a sweep that is fine.
            tmp.write_text(job.model_dump_json())
            os.replace(tmp, path)
        return job

    def get(self, job_id: str) -> Job | None:
        try:
            path = self._path(job_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return Job.model_validate_json(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("job %s is unreadable: %s", job_id, exc)
            return None

    def all(self) -> list[Job]:
        jobs: list[Job] = []
        for path in self.root.glob("*.json"):
            try:
                jobs.append(Job.model_validate_json(path.read_text()))
            except Exception:  # noqa: BLE001 — one bad file must not hide the rest
                continue
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def adopt(self) -> int:
        """Fail every job left 'working' by a process that is no longer running.

        Called once at startup, before anything can poll. Returns how many were
        interrupted, which is worth logging: it is the only signal that the
        container died mid-sweep rather than between sweeps.
        """
        interrupted = 0
        for job in self.all():
            if job.status != "working" or job.boot_id == self.boot_id:
                continue
            job.status = "failed"
            job.error = (
                "This sweep was interrupted — the server stopped or restarted while it was "
                "running, so it never finished. Nothing was saved. Start it again."
            )
            job.summary = "Sweep interrupted by a restart."
            self.save(job)
            interrupted += 1
        if interrupted:
            logger.warning("marked %d interrupted job(s) as failed after restart", interrupted)
        return interrupted

    def prune(self) -> int:
        """Drop jobs that are old and surplus. Never touches a working job."""
        cutoff = time.time() - _KEEP_DAYS * 86_400
        jobs = self.all()
        removed = 0
        for i, job in enumerate(jobs):
            if job.status == "working":
                continue
            if job.created_at < cutoff or i >= _KEEP_MOST_RECENT:
                try:
                    self._path(job.id).unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue
        if removed:
            logger.info("pruned %d old job(s)", removed)
        return removed
