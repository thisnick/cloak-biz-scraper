"""Archiving draws from the shared task-profile pool.

Archiving used to launch on ``archive-<host+path>``: a durable profile minted per
URL and never cleaned up, so the profile list grew by one for every distinct page
anyone ever archived — the same unbounded accumulation the pool already ended for
sweeps. These tests pin the replacement: a leased ``task-N`` identity, returned on
every exit path, drawn from the SAME pool the sweeps use (two pools would each
believe a profile was free and hand it to a sweep and an archive at once, and the
two browsers would collide on Chromium's singleton lock in one user-data-dir).

The browser is never launched here — ``scrape_with_retry`` is replaced, which is
the seam between "which identity did we ask for" and "what did the page say".
"""
from __future__ import annotations

import asyncio

import pytest

from app.models import ArchiveTask
from app.services.archive import ArchiveService, describe
from app.services.instances import InstanceManager
from app.services.jobs import JobStore
from app.services.profiles import ProfileStore
from app.services.scrape import ScrapeService
from app.services.settings import SettingsService
from app.services.task_profiles import is_task_profile

URL = "https://www.bizbuysell.com/Business-Opportunity/a-laundromat/2274905/"


@pytest.fixture
def settings(tmp_path):
    store = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    store.update(max_instances=3, interactive_reserve=1)  # task_budget == 2
    return store


@pytest.fixture
def jobs(tmp_path):
    """The task store an archive records itself in. Real, on the test's own
    volume, so the record can be read back the way the dashboard reads it."""
    return JobStore(tmp_path / "jobs", boot_id="boot-1")


@pytest.fixture
def manager(settings, tmp_path):
    """A real InstanceManager for its profile store and its one pool. Nothing
    launches: every test replaces scrape_with_retry."""
    instances = InstanceManager(settings)
    # Swapped before the pool is first touched — the pool is built lazily over
    # whatever store is installed by then.
    instances.profiles = ProfileStore(tmp_path / "profiles")
    return instances


def _service(manager, settings, jobs, monkeypatch, retry, appender=None) -> ArchiveService:
    monkeypatch.setattr("app.services.archive.scrape_with_retry", retry)
    # The real converter shells out to node+md2blocks, which lives in the image
    # and not in CI. Which identity we launched on is independent of it.
    monkeypatch.setattr("app.services.archive.md_to_blocks", _blocks)
    return ArchiveService(manager, settings, jobs, appender=appender or _appender())


async def _blocks(markdown: str, base_url: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": markdown}}]}}]


def _appender(counted: list | None = None):
    async def append(token, page_id, blocks):
        if counted is not None:
            counted.append((page_id, len(blocks)))
        return len(blocks)

    return append


def _ok(profile_seen: list | None = None):
    """A scrape_with_retry that reads a page successfully."""

    async def retry(instances, *, profile, owner, **kw):
        if profile_seen is not None:
            profile_seen.append(profile)
        return {
            "blocked": False, "error": None, "attempts_used": 1,
            "data": {"title": "A Laundromat", "used_path": "readability",
                     "markdown": "# A Laundromat\n\nCash flow $120,000.\n"},
        }

    return retry


def _names(manager) -> list[str]:
    return sorted(p.name for p in manager.profiles.all())


class TestPooledIdentity:
    @pytest.mark.asyncio
    async def test_archiving_leases_a_pooled_identity(self, manager, settings, jobs, monkeypatch):
        seen: list[str] = []
        svc = _service(manager, settings, jobs, monkeypatch, _ok(seen))

        result = await svc.archive(URL, "page-1")

        assert result.ok, result.error
        assert seen == ["task-1"], "launched on a pooled identity"
        assert _names(manager) == ["task-1"]

    @pytest.mark.asyncio
    async def test_a_hundred_urls_do_not_leave_a_hundred_profiles(
        self, manager, settings, jobs, monkeypatch,
    ):
        """The actual bug. Sequential archives of distinct URLs used to mint
        archive-<host+path> each time; now they reuse the one warm identity."""
        seen: list[str] = []
        svc = _service(manager, settings, jobs, monkeypatch, _ok(seen))

        for n in range(10):
            assert (await svc.archive(f"https://example.com/listing/{n}", "page-1")).ok

        assert seen == ["task-1"] * 10, "the same warm profile every time"
        assert _names(manager) == ["task-1"]

    @pytest.mark.asyncio
    async def test_no_archive_prefixed_profile_is_ever_created(
        self, manager, settings, jobs, monkeypatch,
    ):
        svc = _service(manager, settings, jobs, monkeypatch, _ok())
        await svc.archive(URL, "page-1")

        names = _names(manager)
        assert not any(n.startswith("archive-") for n in names), names
        assert all(is_task_profile(n) for n in names), "pooled names only"


class TestTheLeaseIsAlwaysReturned:
    @pytest.mark.asyncio
    async def test_after_a_successful_archive(self, manager, settings, jobs, monkeypatch):
        svc = _service(manager, settings, jobs, monkeypatch, _ok())
        await svc.archive(URL, "page-1")
        assert manager.task_profiles.acquire("next") == "task-1", "reused, not leaked"

    @pytest.mark.asyncio
    async def test_after_a_blocked_page(self, manager, settings, jobs, monkeypatch):
        async def blocked(instances, *, profile, owner, **kw):
            return {"blocked": True, "error": None, "attempts_used": 3, "data": {}}

        svc = _service(manager, settings, jobs, monkeypatch, blocked)
        result = await svc.archive(URL, "page-1")

        assert not result.ok and "anti-bot" in result.error
        assert manager.task_profiles.acquire("next") == "task-1"

    @pytest.mark.asyncio
    async def test_after_the_notion_write_fails(self, manager, settings, jobs, monkeypatch):
        async def refuse(token, page_id, blocks):
            raise RuntimeError("Notion said no")

        svc = _service(manager, settings, jobs, monkeypatch, _ok(), appender=refuse)
        result = await svc.archive(URL, "page-1")

        assert not result.ok and "could not write it to Notion" in result.summary
        assert manager.task_profiles.acquire("next") == "task-1"

    @pytest.mark.asyncio
    async def test_after_the_launch_itself_raises(self, manager, settings, jobs, monkeypatch):
        async def explode(instances, *, profile, owner, **kw):
            raise RuntimeError("no free display")

        svc = _service(manager, settings, jobs, monkeypatch, explode)
        with pytest.raises(RuntimeError, match="no free display"):
            await svc.archive(URL, "page-1")

        assert manager.task_profiles.acquire("next") == "task-1"
        assert manager.task_profiles.leased_by("next") == ["task-1"]

    @pytest.mark.asyncio
    async def test_a_cancelled_caller_does_not_wedge_the_gate(
        self, manager, settings, jobs, monkeypatch,
    ):
        """A disconnected MCP caller cancels mid-archive. The lease and the gate
        slot must both come back, or later archives queue behind nothing."""
        started = asyncio.Event()

        async def park(instances, *, profile, owner, **kw):
            started.set()
            await asyncio.sleep(3600)

        svc = _service(manager, settings, jobs, monkeypatch, park)
        task = asyncio.create_task(svc.archive(URL, "page-1"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert manager.task_profiles.acquire("next") == "task-1"
        assert svc._past_gate == 0, "the admission slot was returned"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_archives_of_the_same_url_never_share_a_profile(
        self, manager, settings, jobs, monkeypatch,
    ):
        """The old per-URL name could not tell these apart: both archives got
        ``archive-<same-slug>``, one user-data-dir, and Chromium's singleton
        lock. Distinct leases are what keeps them apart now."""
        seen: list[str] = []
        both_in = asyncio.Event()
        release = asyncio.Event()

        async def park(instances, *, profile, owner, **kw):
            seen.append(profile)
            if len(seen) == 2:
                both_in.set()
            await release.wait()
            return {"blocked": False, "error": None, "attempts_used": 1,
                    "data": {"title": "t", "markdown": "# t\n"}}

        svc = _service(manager, settings, jobs, monkeypatch, park)
        tasks = [asyncio.create_task(svc.archive(URL, "page-1")) for _ in range(2)]
        await asyncio.wait_for(both_in.wait(), timeout=5)
        release.set()
        assert all(r.ok for r in await asyncio.gather(*tasks))

        assert sorted(seen) == ["task-1", "task-2"], "same URL, different identities"

    @pytest.mark.asyncio
    async def test_concurrent_archives_stay_within_the_task_budget(
        self, manager, settings, jobs, monkeypatch,
    ):
        """A profile is leased before the launch that waits for a pool slot, so
        without the admission gate eight overlapping archives would each lease
        (and mint) one first. task_budget is 2 here."""
        assert settings.load().task_budget == 2
        in_flight = 0
        peak = 0
        release = asyncio.Event()

        async def park(instances, *, profile, owner, **kw):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await release.wait()
            finally:
                in_flight -= 1
            return {"blocked": False, "error": None, "attempts_used": 1,
                    "data": {"title": "t", "markdown": "# t\n"}}

        svc = _service(manager, settings, jobs, monkeypatch, park)
        tasks = [asyncio.create_task(svc.archive(f"{URL}?p={n}", "page-1")) for n in range(8)]
        await asyncio.sleep(0.05)
        assert peak == 2, "only task_budget archives run past the gate at once"
        assert _names(manager) == ["task-1", "task-2"]

        release.set()
        assert all(r.ok for r in await asyncio.gather(*tasks))
        assert _names(manager) == ["task-1", "task-2"], "the rest reuse, never mint"


class TestOnePoolForEveryTask:
    def test_sweeps_and_archives_share_one_lease_authority(self, manager, settings, tmp_path):
        """Two pools over the same ProfileStore would both call task-1 free."""
        sweeps = ScrapeService(manager, JobStore(tmp_path / "jobs"), settings)
        archives = ArchiveService(manager, settings, JobStore(tmp_path / "archive-jobs"))

        assert sweeps._task_profiles is manager.task_profiles
        assert archives._task_profiles is manager.task_profiles

    @pytest.mark.asyncio
    async def test_an_archive_never_takes_a_profile_a_sweep_is_holding(
        self, manager, settings, jobs, monkeypatch,
    ):
        seen: list[str] = []
        svc = _service(manager, settings, jobs, monkeypatch, _ok(seen))
        manager.task_profiles.acquire("job-7:0")  # a sweep is mid-flight

        await svc.archive(URL, "page-1")

        assert seen == ["task-2"], "task-1 is leased by the sweep"
        assert _names(manager) == ["task-1", "task-2"]

    @pytest.mark.asyncio
    async def test_the_profile_a_sweep_returned_is_handed_to_the_next_archive(
        self, manager, settings, jobs, monkeypatch,
    ):
        seen: list[str] = []
        svc = _service(manager, settings, jobs, monkeypatch, _ok(seen))
        manager.task_profiles.acquire("job-7:0")
        manager.task_profiles.release("job-7:0")

        await svc.archive(URL, "page-1")

        assert seen == ["task-1"], "warm from the sweep, not a fresh mint"
        assert _names(manager) == ["task-1"]


class TestTheArchiveRecordsItself:
    """An archive is a task, so it has to appear in the Tasks list.

    It leases a pooled identity through the same admission gate a sweep does and
    holds a browser for about a minute — and for all that time the dashboard used
    to show nothing whatsoever, because `archive_page` was the one piece of
    browser work that wrote no record. The tool's answer is unchanged; what is
    new is that the run is visible while it happens and readable afterwards.
    """

    @pytest.mark.asyncio
    async def test_a_finished_archive_is_in_the_task_list(self, manager, settings, jobs, monkeypatch):
        svc = _service(manager, settings, jobs, monkeypatch, _ok())

        result = await svc.archive(URL, "page-1")

        (task,) = jobs.all()
        assert task.kind == "archive"
        assert task.status == "completed"
        assert task.urls == [URL] and task.notion_page_id == "page-1"
        assert task.title == "A Laundromat"
        assert task.blocks_appended == result.blocks_appended > 0
        assert task.used_path == "readability"
        assert task.summary == result.summary, "the row and the tool tell one story"

    @pytest.mark.asyncio
    async def test_the_returned_result_is_unchanged(self, manager, settings, jobs, monkeypatch):
        """Recording is added AROUND the archive, not into its answer: the tool
        contract is the same ArchiveResult it always was."""
        svc = _service(manager, settings, jobs, monkeypatch, _ok())

        result = await svc.archive(URL, "page-1")

        assert result.ok and result.error is None
        assert result.url == URL and result.notion_page_id == "page-1"
        assert result.title == "A Laundromat" and result.used_path == "readability"
        assert result.blocks_appended > 0 and result.attempts_used == 1
        assert result.markdown_chars > 0
        assert "Archived 'A Laundromat' into Notion" in result.summary

    @pytest.mark.asyncio
    async def test_it_shows_as_working_while_the_browser_is_open(
        self, manager, settings, jobs, monkeypatch,
    ):
        """The whole point of writing the record BEFORE the launch: the minute an
        archive spends running is a minute it is visible, not a minute of an
        apparently idle server."""
        reading = asyncio.Event()
        release = asyncio.Event()

        async def park(instances, *, profile, owner, **kw):
            reading.set()
            await release.wait()
            return {"blocked": False, "error": None, "attempts_used": 1,
                    "data": {"title": "A Laundromat", "used_path": "readability",
                             "markdown": "# A Laundromat\n"}}

        svc = _service(manager, settings, jobs, monkeypatch, park)
        task = asyncio.create_task(svc.archive(URL, "page-1"))
        await asyncio.wait_for(reading.wait(), timeout=5)

        (running,) = [j for j in jobs.all() if j.status == "working"]
        assert running.kind == "archive"
        assert running.summary == "Archiving www.bizbuysell.com…"
        assert running.notion_page_id == "page-1"

        release.set()
        await task
        assert jobs.get(running.id).status == "completed", "working → completed"

    @pytest.mark.asyncio
    async def test_a_blocked_page_records_a_failed_task(self, manager, settings, jobs, monkeypatch):
        async def blocked(instances, *, profile, owner, **kw):
            return {"blocked": True, "error": None, "attempts_used": 3, "data": {}}

        svc = _service(manager, settings, jobs, monkeypatch, blocked)
        result = await svc.archive(URL, "page-1")

        (task,) = jobs.all()
        assert task.status == "failed", "a block is a failure, exactly as it is for a sweep"
        assert task.blocks_appended == 0
        assert "anti-bot" in task.error
        assert task.error == result.error and task.summary == result.summary
        assert not result.ok, "and the tool still says so itself"

    @pytest.mark.asyncio
    async def test_a_refused_notion_write_records_a_failed_task(
        self, manager, settings, jobs, monkeypatch,
    ):
        async def refuse(token, page_id, blocks):
            raise RuntimeError("Notion said no")

        svc = _service(manager, settings, jobs, monkeypatch, _ok(), appender=refuse)
        result = await svc.archive(URL, "page-1")

        (task,) = jobs.all()
        assert task.status == "failed"
        assert task.error == "Notion said no" == result.error
        assert task.title == "A Laundromat", "it read the page; only the write failed"
        assert task.blocks_appended == 0

    @pytest.mark.asyncio
    async def test_a_launch_that_raises_still_closes_the_task_out(
        self, manager, settings, jobs, monkeypatch,
    ):
        """The exception reaches the caller unchanged — but a record left saying
        "working" would have the dashboard show a browser that is not running."""
        async def explode(instances, *, profile, owner, **kw):
            raise RuntimeError("no free display")

        svc = _service(manager, settings, jobs, monkeypatch, explode)
        with pytest.raises(RuntimeError, match="no free display"):
            await svc.archive(URL, "page-1")

        (task,) = jobs.all()
        assert task.status == "failed"
        assert task.error == "no free display"
        assert task.summary == "Archive failed."

    @pytest.mark.asyncio
    async def test_a_cancelled_archive_does_not_stay_working(
        self, manager, settings, jobs, monkeypatch,
    ):
        started = asyncio.Event()

        async def park(instances, *, profile, owner, **kw):
            started.set()
            await asyncio.sleep(3600)

        svc = _service(manager, settings, jobs, monkeypatch, park)
        call = asyncio.create_task(svc.archive(URL, "page-1"))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        (task,) = jobs.all()
        assert task.status == "failed"
        assert "nothing was written to the notion page" in task.error.lower()

    @pytest.mark.asyncio
    async def test_a_call_with_nothing_to_do_records_no_task(
        self, manager, settings, jobs, monkeypatch,
    ):
        """No browser, no lease, no minute of work — so nothing belongs in a list
        of what this server ran."""
        svc = _service(manager, settings, jobs, monkeypatch, _ok())

        result = await svc.archive(URL, "")

        assert not result.ok and "are required" in result.error
        assert jobs.all() == []

    @pytest.mark.asyncio
    async def test_the_evidence_lands_under_the_task_id(self, manager, settings, jobs, monkeypatch):
        """Where the captures go is what makes them reachable: under the task's
        own id, `/runs/<id>/evidence/...` serves them and dropping the record
        takes them with it. They used to go under an `archive/<url>` directory no
        page could reach and only "Clear task history" could remove."""
        svc = _service(manager, settings, jobs, monkeypatch, _ok())

        result = await svc.archive(URL, "page-1")

        (task,) = jobs.all()
        assert task.evidence_dir == result.evidence_dir
        assert result.evidence_dir.endswith(f"/{task.id}")
        assert "/archive/" not in result.evidence_dir

    @pytest.mark.asyncio
    async def test_two_archives_are_two_rows(self, manager, settings, jobs, monkeypatch):
        svc = _service(manager, settings, jobs, monkeypatch, _ok())

        await svc.archive(URL, "page-1")
        await svc.archive(URL, "page-2")

        assert sorted(t.notion_page_id for t in jobs.all()) == ["page-1", "page-2"]


class TestTheArchiveLabel:
    """`describe(task)` is the archive's own label for the dashboard, living
    beside the archive the way the sweep's lives beside the sweep."""

    def test_it_names_the_page_once_the_title_is_known(self):
        assert describe(ArchiveTask(id="a", urls=[URL], title="A Laundromat")) == (
            "Archive · A Laundromat"
        )

    def test_it_falls_back_to_the_host_while_it_is_still_reading(self):
        assert describe(ArchiveTask(id="b", urls=[URL])) == "Archive · www.bizbuysell.com"

    def test_a_paragraph_of_a_title_is_cut_down(self):
        task = ArchiveTask(id="c", urls=[URL], title="A " + "very " * 40 + "long title")
        label = describe(task)
        assert len(label) <= 70 and label.endswith("…")

    def test_a_task_with_no_url_at_all_still_renders(self):
        assert describe(ArchiveTask(id="d")) == "Archive · a page"
