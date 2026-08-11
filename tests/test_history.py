"""Task history: the runs, and the evidence that used to outlive them forever.

Pruning has always dropped a run's JSON record and left its screenshots behind —
which also stranded them, since the only route that serves evidence checks the
record first. So the properties under test are: evidence goes with its run,
already-stranded evidence goes too, a run that is still WORKING keeps both, and
nothing outside the evidence root is ever touched.
"""
from __future__ import annotations

import pathlib

import pytest

from app.services.history import TaskHistory
from app.services.jobs import JobStore


@pytest.fixture
def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "jobs", boot_id="boot-1", evidence_root=tmp_path / "evidence")


@pytest.fixture
def history(store) -> TaskHistory:
    return TaskHistory(lambda: store)


def _evidence(store: JobStore, job_id: str, *, size: int = 100) -> pathlib.Path:
    """What a sweep leaves behind: a screenshot and the page it saw."""
    d = store.evidence_root / job_id / "source-01" / "final"
    d.mkdir(parents=True)
    (d / "page.png").write_bytes(b"x" * size)
    return store.evidence_root / job_id


def _run(store: JobStore, *, status: str = "completed", size: int = 100):
    job = store.create(source="bizbuysell_serp", url="https://x/y-businesses-for-sale/")
    job.status = status
    store.save(job)
    _evidence(store, job.id, size=size)
    return job


@pytest.mark.asyncio
class TestSnapshot:
    async def test_counts_runs_and_the_evidence_they_are_holding(self, history, store):
        _run(store, size=100)
        _run(store, size=250)

        view = await history.snapshot()

        assert view.runs == 2
        assert view.bytes == 350 and view.files == 2
        assert view.orphans == 0

    async def test_evidence_with_no_run_left_is_counted_as_orphaned(self, history, store):
        """The real state of a long-running server: pruning kept dropping
        records and the screenshots stayed. They are the bulk of the problem, so
        a number that excluded them would understate it."""
        _run(store, size=100)
        _evidence(store, "deadbeef1234", size=900)  # a run pruned months ago

        view = await history.snapshot()

        assert view.runs == 1
        assert view.orphans == 1
        assert view.bytes == 1000, "orphaned evidence is still on the volume"

    async def test_an_empty_volume_measures_as_nothing(self, history):
        view = await history.snapshot()
        assert (view.runs, view.bytes, view.files, view.orphans) == (0, 0, 0, 0)

    async def test_a_symlinked_evidence_directory_is_never_walked(
        self, history, store, tmp_path
    ):
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        store.evidence_root.mkdir(parents=True, exist_ok=True)
        (store.evidence_root / "linked").symlink_to(outside, target_is_directory=True)

        view = await history.snapshot()

        assert view.bytes == 0 and view.orphans == 1

    async def test_a_second_look_serves_the_cache_and_refresh_re_measures(
        self, history, store
    ):
        job = _run(store, size=100)
        assert (await history.snapshot()).bytes == 100

        (store.evidence_root / job.id / "extra.png").write_bytes(b"x" * 400)

        assert (await history.snapshot()).bytes == 100
        assert (await history.snapshot(refresh=True)).bytes == 500

    async def test_measuring_runs_off_the_event_loop(self, history, store, monkeypatch):
        import threading

        import app.services.history as history_module

        _run(store)
        seen: list[int] = []
        monkeypatch.setattr(
            history_module, "measure_dir",
            lambda path: seen.append(threading.get_ident()) or (1, 1),
        )

        await history.snapshot()

        assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
class TestClear:
    async def test_a_run_takes_its_evidence_with_it(self, history, store):
        job = _run(store, size=300)

        cleared = await history.clear()

        assert cleared.runs == 1 and cleared.bytes == 300 and cleared.kept == 0
        assert store.get(job.id) is None
        assert not (store.evidence_root / job.id).exists()

    async def test_orphaned_evidence_is_removed_too(self, history, store):
        orphan = _evidence(store, "deadbeef1234", size=900)

        cleared = await history.clear()

        assert cleared.runs == 0 and cleared.orphans == 1 and cleared.bytes == 900
        assert not orphan.exists()

    async def test_a_working_run_keeps_its_record_and_its_evidence(self, history, store):
        """A sweep in flight is writing to both. Clearing history while one runs
        must leave it able to report its result."""
        running = _run(store, status="working", size=100)
        done = _run(store, status="completed", size=200)

        cleared = await history.clear()

        assert cleared.runs == 1 and cleared.kept == 1 and cleared.bytes == 200
        assert store.get(running.id) is not None
        assert (store.evidence_root / running.id).is_dir()
        assert store.get(done.id) is None
        assert not (store.evidence_root / done.id).exists()

    async def test_a_run_that_starts_mid_clear_is_not_treated_as_an_orphan(
        self, history, store
    ):
        """The orphan sweep re-reads the live records rather than reusing the
        listing it started with — otherwise a sweep that began a moment ago
        would have its evidence deleted out from under it."""
        _run(store, status="completed")
        late = None

        original = store.all

        def all_and_then_start_one():
            nonlocal late
            jobs = original()
            if late is None:
                late = _run(store, status="working", size=400)
            return jobs

        store.all = all_and_then_start_one
        cleared = await history.clear()
        store.all = original

        assert late is not None
        assert store.get(late.id) is not None
        assert (store.evidence_root / late.id).is_dir(), "a live sweep lost its evidence"
        assert cleared.orphans == 0

    async def test_the_page_archives_are_cleared_too(self, history, store):
        """`archive_page` files its captures under `evidence/archive/<url>`,
        which never had a job record and no route can reach — the same dead
        weight as an orphan, and cleared as one."""
        archive = store.evidence_root / "archive" / "example.com-a-listing" / "final"
        archive.mkdir(parents=True)
        (archive / "page.png").write_bytes(b"x" * 400)

        cleared = await history.clear()

        assert cleared.orphans == 1 and cleared.bytes == 400
        assert not (store.evidence_root / "archive").exists()

    async def test_nothing_to_clear_is_not_an_error(self, history):
        cleared = await history.clear()
        assert (cleared.runs, cleared.orphans, cleared.bytes, cleared.kept) == (0, 0, 0, 0)

    async def test_a_symlinked_evidence_directory_is_unlinked_not_followed(
        self, history, store, tmp_path
    ):
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        store.evidence_root.mkdir(parents=True, exist_ok=True)
        link = store.evidence_root / "linked"
        link.symlink_to(outside, target_is_directory=True)

        cleared = await history.clear()

        assert cleared.orphans == 1
        assert not link.is_symlink()
        assert (outside / "huge").read_bytes() == b"x" * 5000, "the target is not ours"

    async def test_the_measurement_after_a_clear_is_re_taken(self, history, store):
        _run(store, size=300)
        assert (await history.snapshot()).bytes == 300

        await history.clear()

        assert (await history.snapshot()).bytes == 0


class TestJobStoreDrop:
    """The one removal both the retention prune and the user's clear go through."""

    def test_removes_the_record_and_the_evidence_together(self, store):
        job = _run(store)
        assert store.drop(job.id) is True
        assert store.get(job.id) is None
        assert not (store.evidence_root / job.id).exists()

    def test_refuses_a_working_job(self, store):
        job = _run(store, status="working")
        assert store.drop(job.id) is False
        assert store.get(job.id) is not None
        assert (store.evidence_root / job.id).is_dir()

    def test_refuses_an_id_that_is_not_one(self, store, tmp_path):
        """A job id arrives from outside on every poll. `../../settings` must
        never reach the volume, here least of all."""
        (store.evidence_root).mkdir(parents=True, exist_ok=True)
        secret = tmp_path / "settings.json"
        secret.write_text("{}")

        assert store.drop("../settings") is False
        assert store.drop("../../settings.json") is False
        assert secret.exists()

    def test_a_missing_evidence_directory_is_not_an_error(self, store):
        job = store.create(source="bizbuysell_serp", url="https://x/y/")
        job.status = "completed"
        store.save(job)

        assert store.drop(job.id) is True
        assert store.get(job.id) is None
