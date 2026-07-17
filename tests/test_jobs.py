"""The job store, which exists because Railway sleeps.

The requirement these defend is narrow and easy to regress into a dict: a sweep
that finished must still be collectable after the process that ran it is gone.
"""
from __future__ import annotations

import time

import pytest

from app.models import Listing
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


def test_prune_never_drops_a_running_job(root):
    """An old timestamp on a working job means a long sweep, not a stale one."""
    store = JobStore(root)
    job = store.create(url="https://x/y-businesses-for-sale/")
    job.created_at = time.time() - 30 * 86_400
    store.save(job)

    assert store.prune() == 0
    assert store.get(job.id) is not None


def test_a_corrupt_job_file_does_not_hide_the_others(root):
    """A torn write on one job must not take out the listing of every other."""
    store = JobStore(root)
    good = store.create(url="https://x/y-businesses-for-sale/")
    (root / "corrupt1.json").write_text("{not json")

    assert [j.id for j in store.all()] == [good.id]
    assert store.get("corrupt1") is None
