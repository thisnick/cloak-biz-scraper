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


def _service(manager, settings, jobs, monkeypatch, retry, notion=None,
             typesafe=None) -> ArchiveService:
    """An ArchiveService with the browser and Notion both stood in for.

    `notion` is one FakeNotion shared by every archive the service makes (so a
    second archive of the same page sees the first one's section); without it
    each archive gets a fresh, empty page."""
    monkeypatch.setattr("app.services.archive.scrape_with_retry", retry)
    # The real converter shells out to node+md2blocks, which lives in the image
    # and not in CI. Which identity we launched on is independent of it.
    monkeypatch.setattr("app.services.archive.md_to_blocks", _blocks)
    factory = (lambda token: notion) if notion is not None else (lambda token: FakeNotion())
    return ArchiveService(manager, settings, jobs, notion_client=factory, typesafe=typesafe)


async def _blocks(markdown: str, base_url: str) -> list[dict]:
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"type": "text", "text": {"content": markdown}}]}}]


def _heading(text: str, level: int = 1) -> dict:
    """A heading block as Notion RETURNS it — with plain_text, unlike the
    blocks this app sends."""
    kind = f"heading_{level}"
    return {"object": "block", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text},
                                  "plain_text": text}]}}


def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text},
                                         "plain_text": text}]}}


class FakeNotion:
    """A NotionClient stand-in holding each page's top-level blocks.

    Every page starts with `blocks`. Serves GET /blocks/{id}/children in pages
    of `page_size` with Notion's cursor fields, appends PATCHed children to that
    page, and records every call. `refuse` is raised from any PATCH, like Notion
    refusing the write.
    """

    def __init__(self, blocks: list[dict] | None = None, *, page_size: int = 100,
                 refuse: Exception | None = None) -> None:
        self._initial = list(blocks or [])
        self.pages: dict[str, list[dict]] = {}
        self.page_size = page_size
        self.refuse = refuse
        self.calls: list[tuple[str, str, dict]] = []

    def _page(self, path: str) -> list[dict]:
        page_id = path.split("/")[2]
        return self.pages.setdefault(page_id, list(self._initial))

    async def request(self, method: str, path: str, **kw):
        self.calls.append((method, path, kw))
        blocks = self._page(path)
        if method == "GET":
            start = int((kw.get("params") or {}).get("start_cursor") or 0)
            nxt = start + self.page_size
            more = nxt < len(blocks)
            return {"results": blocks[start:nxt], "has_more": more,
                    "next_cursor": str(nxt) if more else None}
        if method == "PATCH":
            if self.refuse is not None:
                raise self.refuse
            blocks.extend(kw["json"]["children"])
            return {"results": kw["json"]["children"]}
        raise AssertionError(f"unexpected Notion call {method} {path}")

    @property
    def patches(self) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if c[0] == "PATCH"]


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
        refuse = FakeNotion(refuse=RuntimeError("Notion said no"))
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=refuse)
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
        refuse = FakeNotion(refuse=RuntimeError("Notion said no"))
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=refuse)
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


# ── read / append: the two halves a sweep's triage reuses ───────────────────


class FakeTypeSafe:
    """The guard's classifier: one noul answer, or an error, recorded."""

    def __init__(self, p: float = 0.96, error: Exception | None = None) -> None:
        self.p = p
        self.error = error
        self.calls: list[tuple[object, str]] = []

    async def noul(self, state, instructions):
        self.calls.append((state, instructions))
        if self.error is not None:
            raise self.error
        return self.p


def _with_key(settings):
    settings.update(typesafe_openrouter_api_key="sk-or-v1-archive-guard-test")
    return settings


def _reads(markdown: str, title: str = "A Laundromat"):
    async def retry(instances, *, profile, owner, **kw):
        return {"blocked": False, "error": None, "attempts_used": 1,
                "data": {"title": title, "used_path": "readability", "markdown": markdown}}

    return retry


class TestTheReadHalf:
    """`read` is the browser and nothing else: a sweep's triage reads detail
    pages with it under its own job, so it must not write to Notion, record an
    archive task, or keep the pooled identity."""

    @pytest.mark.asyncio
    async def test_it_reads_without_notion_or_a_task_record(
        self, manager, settings, jobs, monkeypatch, tmp_path,
    ):
        def no_notion(token):
            raise AssertionError("read must never reach for Notion")

        monkeypatch.setattr("app.services.archive.scrape_with_retry", _ok())
        svc = ArchiveService(manager, settings, jobs, notion_client=no_notion)

        read = await svc.read(URL, tmp_path / "evidence" / "job-1" / "detail-01")

        assert read.ok and read.failure is None
        assert read.title == "A Laundromat" and read.used_path == "readability"
        assert "Cash flow $120,000." in read.markdown
        assert read.attempts_used == 1
        assert read.evidence_dir.endswith("job-1/detail-01"), "the caller's evidence dir"
        assert jobs.all() == [], "no ArchiveTask for a read"
        assert manager.task_profiles.acquire("next") == "task-1", "the lease came back"
        assert svc._past_gate == 0

    @pytest.mark.asyncio
    async def test_a_blocked_read_is_a_failure_in_plain_words(
        self, manager, settings, jobs, monkeypatch, tmp_path,
    ):
        async def blocked(instances, *, profile, owner, **kw):
            return {"blocked": True, "error": None, "attempts_used": 3, "data": {}}

        svc = _service(manager, settings, jobs, monkeypatch, blocked)
        read = await svc.read(URL, tmp_path / "ev")

        assert read.blocked and not read.ok
        assert "www.bizbuysell.com served an anti-bot page" in read.failure
        assert read.attempts_used == 3

    @pytest.mark.asyncio
    async def test_an_error_or_an_empty_page_is_a_failure_too(
        self, manager, settings, jobs, monkeypatch, tmp_path,
    ):
        async def errored(instances, *, profile, owner, **kw):
            return {"blocked": False, "error": "Timed out loading the page.",
                    "attempts_used": 3, "data": {}}

        svc = _service(manager, settings, jobs, monkeypatch, errored)
        assert (await svc.read(URL, tmp_path / "a")).failure == "Timed out loading the page."

        svc = _service(manager, settings, jobs, monkeypatch, _reads("   \n"))
        read = await svc.read(URL, tmp_path / "b")
        assert not read.ok and "no readable content" in read.failure

    @pytest.mark.asyncio
    async def test_the_owner_label_can_be_the_callers(
        self, manager, settings, jobs, monkeypatch, tmp_path,
    ):
        owners: list[str] = []

        async def retry(instances, *, profile, owner, **kw):
            owners.append(owner)
            return await _ok()(instances, profile=profile, owner=owner, **kw)

        svc = _service(manager, settings, jobs, monkeypatch, retry)
        await svc.read(URL, tmp_path / "a")
        await svc.read(URL, tmp_path / "b", owner="job:abc:detail")
        assert owners[0].startswith("archive:") and owners[1] == "job:abc:detail"


class TestTheAppendHalf:
    @pytest.mark.asyncio
    async def test_it_writes_the_prelude_then_the_content(
        self, manager, settings, jobs, monkeypatch,
    ):
        notion = FakeNotion()
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        done = await svc.append(notion, "page-9", "# Title\n\nBody.", URL)

        assert not done.already_archived and done.blocks_appended == 4
        (patch,) = notion.patches
        assert patch[1] == "/blocks/page-9/children"
        kinds = [b["type"] for b in patch[2]["json"]["children"]]
        assert kinds == ["divider", "heading_1", "callout", "paragraph"]
        heading = patch[2]["json"]["children"][1]["heading_1"]["rich_text"][0]["text"]["content"]
        assert heading == "Source Content"

    @pytest.mark.asyncio
    async def test_it_writes_on_the_client_it_is_given(self, manager, settings, jobs, monkeypatch):
        """Triage shares one client (one pace) across the whole phase, so append
        must use it rather than making its own."""
        def no_factory(token):
            raise AssertionError("append must use the client it was handed")

        monkeypatch.setattr("app.services.archive.md_to_blocks", _blocks)
        svc = ArchiveService(manager, settings, jobs, notion_client=no_factory)
        shared = FakeNotion()
        await svc.append(shared, "page-1", "one", URL)
        await svc.append(shared, "page-2", "two", URL)
        assert [c[1] for c in shared.patches] == ["/blocks/page-1/children",
                                                  "/blocks/page-2/children"]


class TestAppendingIsIdempotent:
    """A page that already carries the "Source Content" section is left alone —
    whether this server archived it or an agent did before triage existed."""

    @pytest.mark.asyncio
    async def test_a_page_with_the_section_gets_nothing(self, manager, settings, jobs, monkeypatch):
        notion = FakeNotion([_para("My notes."), _heading("Source Content"), _para("old copy")])
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        done = await svc.append(notion, "page-1", "Body.", URL)

        assert done.already_archived and done.blocks_appended == 0
        assert notion.patches == [], "nothing appended"

    @pytest.mark.asyncio
    async def test_archive_page_reports_it_already_archived(
        self, manager, settings, jobs, monkeypatch,
    ):
        notion = FakeNotion([_heading("Source Content")])
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        result = await svc.archive(URL, "page-1")

        assert result.ok and result.error is None, "the page IS archived"
        assert result.blocks_appended == 0
        assert "already has a 'Source Content' section" in result.summary
        assert notion.patches == []
        (task,) = jobs.all()
        assert task.status == "completed" and task.summary == result.summary

    @pytest.mark.asyncio
    async def test_a_repeat_call_is_answered_before_any_browser_work(
        self, manager, settings, jobs, monkeypatch,
    ):
        """The section is looked for first, so a repeat call costs one Notion
        read instead of a minute of page loading — and no pooled identity."""
        async def never(*args, **kw):
            raise AssertionError("an archived page must not be read again")

        notion = FakeNotion([_heading("Source Content")])
        svc = _service(manager, settings, jobs, monkeypatch, never, notion=notion)

        result = await svc.archive(URL, "page-1")

        assert result.ok and result.blocks_appended == 0 and result.attempts_used == 0
        assert f"{URL} is already archived there" in result.summary
        assert [c[0] for c in notion.calls] == ["GET"] and notion.patches == []
        assert svc._past_gate == 0
        assert _names(manager) == [], "no identity was leased for it"

    @pytest.mark.asyncio
    async def test_a_notion_page_that_cannot_be_opened_stops_it_before_reading(
        self, manager, settings, jobs, monkeypatch,
    ):
        async def never(*args, **kw):
            raise AssertionError("no point reading a page there is nowhere to put")

        class Unshared(FakeNotion):
            async def request(self, method, path, **kw):
                raise RuntimeError("Notion could not find that page or database.")

        svc = _service(manager, settings, jobs, monkeypatch, never, notion=Unshared())

        result = await svc.archive(URL, "page-1")

        assert not result.ok and "Notion could not find that page" in result.error
        assert "was not read and nothing was written" in result.error
        (task,) = jobs.all()
        assert task.status == "failed" and task.error == result.error

    @pytest.mark.asyncio
    async def test_a_second_archive_of_the_same_page_appends_nothing(
        self, manager, settings, jobs, monkeypatch,
    ):
        notion = FakeNotion()
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        first = await svc.archive(URL, "page-1")
        second = await svc.archive(URL, "page-1")

        assert first.blocks_appended > 0
        assert second.ok and second.blocks_appended == 0
        assert len(notion.patches) == 1, "the content is on the page once"

    @pytest.mark.asyncio
    async def test_the_section_is_found_past_the_first_page_of_blocks(
        self, manager, settings, jobs, monkeypatch,
    ):
        """Someone's own notes can run to hundreds of blocks above an archived
        section; stopping at the first page of children would miss it."""
        blocks = [_para(f"note {n}") for n in range(150)] + [_heading("Source Content")]
        notion = FakeNotion(blocks, page_size=100)
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        done = await svc.append(notion, "page-1", "Body.", URL)

        assert done.already_archived
        gets = [c for c in notion.calls if c[0] == "GET"]
        assert [c[2]["params"].get("start_cursor") for c in gets] == [None, "100"]
        assert all(c[1] == "/blocks/page-1/children" for c in gets)

    @pytest.mark.asyncio
    async def test_a_lookalike_is_not_the_section(self, manager, settings, jobs, monkeypatch):
        """Only the Heading 1 prelude writes counts — a smaller heading or a
        paragraph that happens to say the words is someone's own content."""
        notion = FakeNotion([_heading("Source Content", level=2), _para("Source Content"),
                             _heading("Source Contents")])
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        done = await svc.append(notion, "page-1", "Body.", URL)

        assert not done.already_archived and len(notion.patches) == 1

    @pytest.mark.asyncio
    async def test_a_custom_heading_is_its_own_section(self, manager, settings, jobs, monkeypatch):
        notion = FakeNotion([_heading("Source Content")])
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion)

        done = await svc.append(notion, "page-1", "Body.", URL, heading="Detail Page")

        assert not done.already_archived and done.blocks_appended > 0


class TestTheGuard:
    """With a TypeSafe key saved, a page that is not its real content — a login
    wall, a 404, a removed listing, an anti-bot check — is never written."""

    @pytest.mark.asyncio
    async def test_no_key_means_no_guard(self, manager, settings, jobs, monkeypatch):
        classifier = FakeTypeSafe(p=0.01)
        notion = FakeNotion()
        svc = _service(manager, settings, jobs, monkeypatch, _ok(), notion=notion,
                       typesafe=classifier)

        result = await svc.archive(URL, "page-1")

        assert classifier.calls == [], "nothing is asked without a key"
        assert result.ok and result.blocks_appended > 0, "exactly as before the guard"

    @pytest.mark.asyncio
    async def test_no_classifier_means_no_guard(self, manager, settings, jobs, monkeypatch):
        svc = _service(manager, _with_key(settings), jobs, monkeypatch, _ok())
        assert (await svc.archive(URL, "page-1")).ok

    @pytest.mark.asyncio
    async def test_the_real_page_passes_and_is_archived(self, manager, settings, jobs, monkeypatch):
        from app.services.archive import GUARD_QUESTION

        classifier = FakeTypeSafe(p=0.96)
        notion = FakeNotion()
        svc = _service(manager, _with_key(settings), jobs, monkeypatch, _ok(), notion=notion,
                       typesafe=classifier)

        result = await svc.archive(URL, "page-1")

        assert result.ok and result.blocks_appended > 0
        ((state, instructions),) = classifier.calls
        assert instructions == GUARD_QUESTION
        assert state == {"page_text": "# A Laundromat\n\nCash flow $120,000.\n"}
        assert "could not run" not in result.summary

    @pytest.mark.asyncio
    async def test_a_page_below_the_threshold_writes_nothing(
        self, manager, settings, jobs, monkeypatch,
    ):
        classifier = FakeTypeSafe(p=0.03)
        notion = FakeNotion()
        svc = _service(manager, _with_key(settings), jobs, monkeypatch,
                       _reads("This listing is no longer available."), notion=notion,
                       typesafe=classifier)

        result = await svc.archive(URL, "page-1")

        assert not result.ok and result.blocks_appended == 0
        assert result.error == (
            "The page doesn't look like its real content (P=0.03: a login wall, error, "
            "removed listing or anti-bot page?) — nothing was written."
        )
        assert notion.patches == [], "nothing is written"
        assert [c[0] for c in notion.calls] == ["GET"], "only the look for an earlier section"
        (task,) = jobs.all()
        assert task.status == "failed" and task.error == result.error
        # With evidence: the verdict sits beside the page capture.
        import json
        from pathlib import Path

        verdict = json.loads((Path(result.evidence_dir) / "guard.json").read_text())
        assert verdict["p_real_content"] == 0.03 and verdict["threshold"] == 0.3

    @pytest.mark.asyncio
    async def test_the_threshold_itself_passes(self, manager, settings, jobs, monkeypatch):
        svc = _service(manager, _with_key(settings), jobs, monkeypatch, _ok(),
                       typesafe=FakeTypeSafe(p=0.3))
        assert (await svc.archive(URL, "page-1")).ok

    @pytest.mark.asyncio
    async def test_it_reads_only_the_top_of_the_page(self, manager, settings, jobs, monkeypatch):
        classifier = FakeTypeSafe()
        svc = _service(manager, _with_key(settings), jobs, monkeypatch,
                       _reads("x" * 3990 + "y" * 5000), typesafe=classifier)

        await svc.archive(URL, "page-1")

        ((state, _),) = classifier.calls
        assert state["page_text"] == "x" * 3990 + "y" * 10

    @pytest.mark.asyncio
    async def test_a_classifier_outage_never_stops_an_archive(
        self, manager, settings, jobs, monkeypatch, caplog,
    ):
        from app.services.typesafe import TypeSafeUnavailable

        classifier = FakeTypeSafe(error=TypeSafeUnavailable(
            "The TypeSafe Classifier (e.g. Jev) did not answer after 4 attempts."))
        notion = FakeNotion()
        svc = _service(manager, _with_key(settings), jobs, monkeypatch, _ok(), notion=notion,
                       typesafe=classifier)

        with caplog.at_level("WARNING", logger="cloakbiz.archive"):
            result = await svc.archive(URL, "page-1")

        assert result.ok and result.blocks_appended > 0, "archived as if there were no guard"
        assert "could not run" in result.summary and "archived unchecked" in result.summary
        assert "did not answer" in result.summary
        assert len(notion.patches) == 1
        assert any("guard unavailable" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_page_that_was_never_read_is_never_asked_about(
        self, manager, settings, jobs, monkeypatch,
    ):
        async def blocked(instances, *, profile, owner, **kw):
            return {"blocked": True, "error": None, "attempts_used": 3, "data": {}}

        classifier = FakeTypeSafe()
        svc = _service(manager, _with_key(settings), jobs, monkeypatch, blocked,
                       typesafe=classifier)
        result = await svc.archive(URL, "page-1")
        assert "anti-bot" in result.error and classifier.calls == []


class TestTheGuardHelpers:
    """The guard, reusable: a sweep's triage asks it in the same request as its
    detail-page question."""

    @pytest.mark.asyncio
    async def test_guard_asks_one_noul_about_the_page_text(self):
        from app.services.archive import GUARD_QUESTION, guard

        classifier = FakeTypeSafe(p=0.5)
        assert await guard(classifier, "Hello") == 0.5
        assert classifier.calls == [({"page_text": "Hello"}, GUARD_QUESTION)]

    def test_the_question_bundles_as_an_api_question(self):
        from app.services.archive import GUARD_QUESTION, guard_question, guard_state

        assert guard_question() == {"type": "noul", "instructions": GUARD_QUESTION}
        assert guard_state("a" * 5000) == {"page_text": "a" * 4000}

    def test_the_wording_is_the_one_validated_live(self):
        from app.services.archive import GUARD_QUESTION, GUARD_THRESHOLD

        assert GUARD_QUESTION == (
            "The text is the real content of the page — for example a business listing's "
            "details — not a login or sign-up wall, a cookie or consent screen, an error or "
            "not-found page, a removed or no-longer-available notice, or an anti-bot check."
        )
        assert GUARD_THRESHOLD == 0.3
