"""The job store, which exists because Railway sleeps.

The requirement these defend is narrow and easy to regress into a dict: a sweep
that finished must still be collectable after the process that ran it is gone.

Since a record became a union (models.Task), there is a second one: a record
written before `kind` existed must still load, as the sweep it is. Nothing
rewrites the volume, so those files are the shape a real deploy meets first.
"""
from __future__ import annotations

import json
import time

import pytest

from app.models import ArchiveTask, Listing, SweepTask
from app.services.jobs import JobStore


@pytest.fixture
def root(tmp_path):
    return tmp_path / "jobs"


def test_a_finished_job_outlives_the_process(root):
    """The exit criterion, at unit level: kill the container mid-poll and the
    results are still there. A second JobStore is a second boot."""
    first = JobStore(root, boot_id="boot-1")
    job = first.create(source="bizbuysell_serp", url="https://x/y-businesses-for-sale/")
    job.status = "completed"
    job.listings = [Listing(listing_id="2485121", title="A Business", asking_price="$1,258,000")]
    job.summary = "Found 1 listing(s)."
    first.save(job)

    restarted = JobStore(root, boot_id="boot-2")
    restarted.adopt()

    recovered = restarted.get(job.id)
    assert recovered is not None, "a finished sweep must survive a restart"
    assert recovered.status == "completed"
    assert recovered.listings[0].asking_price == "$1,258,000", "verbatim, all the way through"


def test_a_job_interrupted_by_a_restart_is_failed_not_working(root):
    """Nothing is advancing it, so 'working' would tell an agent to wait forever
    for a sweep that died — with no error to explain the silence."""
    first = JobStore(root, boot_id="boot-1")
    job = first.create(source="bizbuysell_serp", url="https://x/y-businesses-for-sale/")
    assert job.status == "working"

    restarted = JobStore(root, boot_id="boot-2")
    assert restarted.adopt() == 1

    recovered = restarted.get(job.id)
    assert recovered.status == "failed"
    assert "interrupted" in recovered.error.lower()
    assert "start it again" in recovered.error.lower(), "say what to do about it"


def test_adopt_leaves_this_boot_alone(root):
    """A sweep running right now is not an orphan."""
    store = JobStore(root, boot_id="boot-1")
    job = store.create(url="https://x/y-businesses-for-sale/")
    assert store.adopt() == 0
    assert store.get(job.id).status == "working"


def test_adopt_does_not_rewrite_a_finished_job(root):
    store = JobStore(root, boot_id="boot-1")
    job = store.create(url="https://x/y-businesses-for-sale/")
    job.status = "completed"
    job.summary = "Found 3 listing(s)."
    store.save(job)

    JobStore(root, boot_id="boot-2").adopt()
    assert store.get(job.id).summary == "Found 3 listing(s)."


def test_an_unknown_job_is_none_not_an_error(root):
    """The poll must be able to say "no such job" rather than crash."""
    assert JobStore(root).get("deadbeef") is None


@pytest.mark.parametrize("hostile", ["../auth", "../../etc/passwd", "a/b", ".", "", "x.json"])
def test_a_job_id_cannot_walk_the_volume(root, hostile):
    """job_id arrives from the outside. The settings, the DEK, and the secret all
    live on this volume next to the jobs."""
    store = JobStore(root)
    assert store.get(hostile) is None


def test_prune_keeps_recent_and_drops_old(root):
    store = JobStore(root)
    old = store.create(url="https://x/old-businesses-for-sale/")
    old.status = "completed"
    old.created_at = time.time() - 30 * 86_400
    store.save(old)

    recent = store.create(url="https://x/new-businesses-for-sale/")
    recent.status = "completed"
    store.save(recent)

    assert store.prune() == 1
    assert store.get(old.id) is None
    assert store.get(recent.id) is not None


def test_prune_takes_the_evidence_with_the_record(root, tmp_path):
    """The leak this closes: pruning the record used to STRAND the screenshots,
    because /runs/<id>/evidence checks the record before serving a file. So the
    volume kept files no page could ever reach again."""
    evidence = tmp_path / "evidence"
    store = JobStore(root, evidence_root=evidence)
    old = store.create(url="https://x/old-businesses-for-sale/")
    old.status = "completed"
    old.created_at = time.time() - 30 * 86_400
    store.save(old)
    recent = store.create(url="https://x/new-businesses-for-sale/")
    recent.status = "completed"
    store.save(recent)
    for job in (old, recent):
        (evidence / job.id / "source-01").mkdir(parents=True)
        (evidence / job.id / "source-01" / "page.png").write_bytes(b"x" * 100)

    assert store.prune() == 1
    assert not (evidence / old.id).exists()
    assert (evidence / recent.id).is_dir(), "a kept run keeps what it captured"


def test_prune_leaves_a_working_job_and_its_evidence_alone(root, tmp_path):
    evidence = tmp_path / "evidence"
    store = JobStore(root, evidence_root=evidence)
    job = store.create(url="https://x/y-businesses-for-sale/")
    job.created_at = time.time() - 30 * 86_400
    store.save(job)
    (evidence / job.id).mkdir(parents=True)
    (evidence / job.id / "page.png").write_bytes(b"x" * 100)

    assert store.prune() == 0
    assert store.get(job.id) is not None
    assert (evidence / job.id / "page.png").exists()


def test_prune_never_drops_a_running_job(root):
    """An old timestamp on a working job means a long sweep, not a stale one."""
    store = JobStore(root)
    job = store.create(url="https://x/y-businesses-for-sale/")
    job.created_at = time.time() - 30 * 86_400
    store.save(job)

    assert store.prune() == 0
    assert store.get(job.id) is not None


# ── the union, and the records written before there was one ─────────────────
#
# Every job on a live volume predates `kind`. If the loader refused them, or
# quietly dropped what only a sweep carries, a user would open the dashboard
# after a deploy and find their history gone — so these drive the REAL JobStore
# against files written in the old shape, not model_validate against a dict.


def _legacy_bytes(**overrides) -> str:
    """A job exactly as the flat `Job` model wrote it: no `kind`, and the
    single scalar `url` of the shape before that."""
    record = {
        "id": "legacy0000ab",
        "status": "completed",
        "source": "bizbuysell_serp",
        "url": "https://www.bizbuysell.com/california/businesses-for-sale/",
        "max_pages": 3,
        "sync": True,
        "db_id": "db-123",
        "summary": "Found 2 listing(s) across 3 pages.",
        "pages_crawled": 3,
        "error": None,
        "synced": {"new": 2, "existing": 1, "db_id": "db-123", "skipped": ["EBITDA"]},
        "listings": [
            {"listing_id": "2453593", "url": "https://x/1", "normalized_url": "x/1",
             "title": "Remodeling Contractor", "asking_price": "$965,000",
             "source": "bizbuysell_serp", "synced_row_id": "notion-page-2453593"},
        ],
        "boot_id": "boot-0",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_060.0,
    }
    record.update(overrides)
    return json.dumps(record)


def _write_legacy(root, **overrides) -> str:
    root.mkdir(parents=True, exist_ok=True)
    text = _legacy_bytes(**overrides)
    job_id = json.loads(text)["id"]
    (root / f"{job_id}.json").write_text(text)
    return job_id


def test_a_job_written_before_kinds_loads_as_the_sweep_it_is(root):
    """The migration, through the real reader: no `kind` on disk means sweep,
    and everything only a sweep carries survives the trip."""
    job_id = _write_legacy(root)

    job = JobStore(root).get(job_id)

    assert isinstance(job, SweepTask), f"a job with no kind loaded as {type(job).__name__}"
    assert job.kind == "sweep"
    assert job.pages_crawled == 3 and job.max_pages == 3 and job.sync is True
    assert job.synced is not None and (job.synced.new, job.synced.existing) == (2, 1)
    assert job.synced.skipped == ["EBITDA"]
    assert [l.listing_id for l in job.listings] == ["2453593"]
    assert job.listings[0].asking_price == "$965,000", "verbatim, across the migration too"
    # And the migration BEFORE this one still applies underneath it.
    assert job.urls == ["https://www.bizbuysell.com/california/businesses-for-sale/"]


def test_a_legacy_job_is_listed_with_the_rest(root):
    """`all()` is what the dashboard reads. A record it cannot parse is a record
    that silently disappears from the history."""
    legacy = _write_legacy(root)
    store = JobStore(root)
    fresh = store.create(url="https://x/y-businesses-for-sale/")

    assert {j.id for j in store.all()} == {legacy, fresh.id}


def test_a_legacy_job_round_trips_and_keeps_its_listings(root):
    """Loaded, saved, reloaded: the kind is written down this time, and nothing
    the old file held is lost on the way through."""
    job_id = _write_legacy(root)
    store = JobStore(root)

    job = store.get(job_id)
    job.summary = "Re-read and saved."
    store.save(job)

    assert json.loads((root / f"{job_id}.json").read_text())["kind"] == "sweep"
    again = store.get(job_id)
    assert isinstance(again, SweepTask)
    assert again.summary == "Re-read and saved."
    assert [l.listing_id for l in again.listings] == ["2453593"]
    assert again.synced.db_id == "db-123"


def test_an_archive_task_is_stored_and_read_back_as_an_archive(root):
    store = JobStore(root)
    task = store.create(
        kind="archive", url="https://example.com/a-listing/", notion_page_id="page-77",
        status="completed", title="A Laundromat", blocks_appended=12, used_path="readability",
    )
    assert isinstance(task, ArchiveTask)

    read = store.get(task.id)
    assert isinstance(read, ArchiveTask) and read.kind == "archive"
    assert read.notion_page_id == "page-77" and read.title == "A Laundromat"
    assert read.blocks_appended == 12 and read.used_path == "readability"
    assert read.urls == ["https://example.com/a-listing/"], "the url migration is on the base"


def test_each_kind_carries_only_its_own_fields(root):
    """The point of the union: an archive has no listings to be empty, and a
    sweep has no Notion page to be blank."""
    store = JobStore(root)
    sweep = store.create(url="https://x/y-businesses-for-sale/", source="bizbuysell_serp")
    archive = store.create(kind="archive", url="https://x/one/", notion_page_id="page-1")

    stored_sweep = json.loads((root / f"{sweep.id}.json").read_text())
    stored_archive = json.loads((root / f"{archive.id}.json").read_text())

    assert stored_sweep["kind"] == "sweep" and stored_archive["kind"] == "archive"
    for sweep_only in ("listings", "synced", "pages_crawled", "max_pages", "db_id"):
        assert sweep_only in stored_sweep
        assert sweep_only not in stored_archive, f"an archive stored a sweep's {sweep_only}"
    for archive_only in ("notion_page_id", "title", "blocks_appended", "used_path"):
        assert archive_only in stored_archive
        assert archive_only not in stored_sweep, f"a sweep stored an archive's {archive_only}"
    assert not hasattr(store.get(archive.id), "listings")


def test_the_discriminator_routes_a_hand_written_archive(root):
    """A record whose `kind` says archive is read as one — the loader believes
    the file, it does not infer the kind from which fields are present."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "handwritten1.json").write_text(json.dumps({
        "kind": "archive", "id": "handwritten1", "status": "failed",
        "urls": ["https://example.com/x"], "notion_page_id": "page-9",
        "error": "example.com served an anti-bot page instead of the listing.",
        "created_at": 5.0, "updated_at": 6.0,
    }))

    task = JobStore(root).get("handwritten1")
    assert isinstance(task, ArchiveTask) and task.notion_page_id == "page-9"


def test_an_interrupted_archive_says_nothing_reached_notion(root):
    """`adopt` speaks for both kinds, and "nothing was saved" means a different
    thing for each — someone reading this is about to go and look at the page."""
    first = JobStore(root, boot_id="boot-1")
    task = first.create(kind="archive", url="https://x/one/", notion_page_id="page-1")

    assert JobStore(root, boot_id="boot-2").adopt() == 1

    recovered = first.get(task.id)
    assert recovered.status == "failed"
    assert "nothing was written to the notion page" in recovered.error.lower()
    assert "start it again" in recovered.error.lower()
    assert recovered.summary == "Archive interrupted by a restart."


def test_prune_takes_an_archive_and_its_evidence_like_any_other_task(root, tmp_path):
    """Retention is a property of the store, not of sweeps: an archive writes its
    captures under its own id exactly as a sweep does, so both go together."""
    evidence = tmp_path / "evidence"
    store = JobStore(root, evidence_root=evidence)
    old = store.create(kind="archive", url="https://x/old/", notion_page_id="page-1")
    old.status = "completed"
    old.created_at = time.time() - 30 * 86_400
    store.save(old)
    (evidence / old.id / "final").mkdir(parents=True)
    (evidence / old.id / "final" / "article.md").write_text("# A Laundromat")

    assert store.prune() == 1
    assert store.get(old.id) is None
    assert not (evidence / old.id).exists()


def test_prune_never_drops_a_working_archive(root, tmp_path):
    """An archive runs for about a minute with the record already written; a
    prune landing in that minute must not delete it out from under itself."""
    evidence = tmp_path / "evidence"
    store = JobStore(root, evidence_root=evidence)
    task = store.create(kind="archive", url="https://x/one/", notion_page_id="page-1")
    task.created_at = time.time() - 30 * 86_400
    store.save(task)
    (evidence / task.id).mkdir(parents=True)

    assert store.prune() == 0
    assert store.get(task.id) is not None
    assert (evidence / task.id).is_dir()


def test_a_corrupt_job_file_does_not_hide_the_others(root):
    """A torn write on one job must not take out the listing of every other."""
    store = JobStore(root)
    good = store.create(url="https://x/y-businesses-for-sale/")
    (root / "corrupt1.json").write_text("{not json")

    assert [j.id for j in store.all()] == [good.id]
    assert store.get("corrupt1") is None
