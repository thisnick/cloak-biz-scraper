"""Profile disk usage: measured off the event loop, cached, and never able to
break the settings page.

The load-bearing properties are that the walk survives a directory being written
to underneath it, that it never follows a symlink out of the profiles root, and
that a cached measurement is not silently re-walked on every page visit.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from app.services import profile_sizes
from app.services.profile_sizes import ProfileSizes, measure_dir
from app.services.profiles import ProfileStore


def _store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def _make(s, name):
    return s.get_or_create(name, default_country="US", default_region="california")


def _sizes(s) -> ProfileSizes:
    return ProfileSizes(lambda: s)


def _tree(root: pathlib.Path) -> int:
    """Three files totalling 60 bytes, one of them nested."""
    (root / "Cookies").write_bytes(b"a" * 10)
    (root / "Local Storage").mkdir()
    (root / "Local Storage" / "leveldb").write_bytes(b"b" * 20)
    (root / "Cache").mkdir()
    (root / "Cache" / "f").write_bytes(b"c" * 30)
    return 60


class TestMeasureDir:
    def test_counts_bytes_and_files_across_the_tree(self, tmp_path):
        total = _tree(tmp_path)
        assert measure_dir(tmp_path) == (total, 3)

    def test_a_missing_directory_is_zero_not_an_error(self, tmp_path):
        assert measure_dir(tmp_path / "never-existed") == (0, 0)

    def test_an_empty_directory_is_zero(self, tmp_path):
        assert measure_dir(tmp_path) == (0, 0)

    def test_a_file_vanishing_mid_walk_does_not_raise(self, tmp_path, monkeypatch):
        """An OPEN profile is written to while this walks it, so a file listed a
        moment ago may already be gone by the time it is stat'd."""
        _tree(tmp_path)
        real_stat = os.stat

        def flaky_stat(path, *args, **kwargs):
            if str(path).endswith("Cookies"):
                raise FileNotFoundError(path)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(profile_sizes.os, "stat", flaky_stat)
        # The vanished file is skipped; everything else is still counted.
        assert measure_dir(tmp_path) == (50, 2)

    def test_an_unreadable_subdirectory_is_skipped(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root reads everything")
        (tmp_path / "Cookies").write_bytes(b"a" * 10)
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "secret").write_bytes(b"b" * 999)
        locked.chmod(0o000)
        try:
            assert measure_dir(tmp_path) == (10, 1)
        finally:
            locked.chmod(0o700)

    def test_a_symlinked_directory_is_not_followed(self, tmp_path):
        """The size of somebody else's tree must never be walked into ours."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        inside = tmp_path / "profile"
        inside.mkdir()
        (inside / "Cookies").write_bytes(b"a" * 10)
        (inside / "link").symlink_to(outside, target_is_directory=True)

        assert measure_dir(inside) == (10, 1)  # 5000 stayed outside

    def test_a_symlinked_file_is_counted_as_the_link_not_its_target(self, tmp_path):
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"x" * 5000)
        inside = tmp_path / "profile"
        inside.mkdir()
        (inside / "link").symlink_to(outside)

        total, files = measure_dir(inside)
        assert files == 1 and 0 < total < 5000


@pytest.mark.asyncio
class TestSnapshot:
    async def test_reports_bytes_and_files_per_profile(self, tmp_path):
        s = _store(tmp_path)
        total = _tree(pathlib.Path(_make(s, "research").user_data_dir))
        _make(s, "empty")

        rows = {r.name: r for r in await _sizes(s).snapshot()}

        assert rows["research"].bytes == total and rows["research"].files == 3
        assert rows["empty"].bytes == 0 and rows["empty"].files == 0
        assert rows["research"].measured_at > 0

    async def test_a_cached_measurement_is_not_re_walked(self, tmp_path, monkeypatch):
        s = _store(tmp_path)
        _tree(pathlib.Path(_make(s, "research").user_data_dir))
        sizes = _sizes(s)
        first = await sizes.snapshot()

        walks = []
        monkeypatch.setattr(
            profile_sizes, "measure_dir", lambda p: walks.append(p) or (999, 999)
        )
        again = await sizes.snapshot()

        assert walks == []                                  # the disk was not touched
        assert again[0].bytes == first[0].bytes
        assert again[0].measured_at == first[0].measured_at  # same measurement

    async def test_refresh_re_walks_and_picks_up_new_data(self, tmp_path):
        s = _store(tmp_path)
        directory = pathlib.Path(_make(s, "research").user_data_dir)
        sizes = _sizes(s)
        assert (await sizes.snapshot())[0].bytes == 0

        (directory / "Cookies").write_bytes(b"a" * 40)

        assert (await sizes.snapshot())[0].bytes == 0        # still the cached zero
        assert (await sizes.snapshot(refresh=True))[0].bytes == 40

    async def test_invalidate_forces_the_next_snapshot_to_measure(self, tmp_path):
        s = _store(tmp_path)
        directory = pathlib.Path(_make(s, "research").user_data_dir)
        sizes = _sizes(s)
        await sizes.snapshot()
        (directory / "Cookies").write_bytes(b"a" * 40)

        sizes.invalidate("research")

        assert (await sizes.snapshot())[0].bytes == 40

    async def test_clearing_a_profile_invalidates_its_cached_size(self, tmp_path):
        """What the service does after a wipe: the cached number is now a lie."""
        s = _store(tmp_path)
        _tree(pathlib.Path(_make(s, "research").user_data_dir))
        sizes = _sizes(s)
        assert (await sizes.snapshot())[0].files == 3

        s.clear("research")
        sizes.invalidate("research")

        assert (await sizes.snapshot())[0].files == 0

    async def test_a_deleted_profile_leaves_no_cached_entry(self, tmp_path):
        s = _store(tmp_path)
        _make(s, "research")
        _make(s, "gone")
        sizes = _sizes(s)
        assert len(await sizes.snapshot()) == 2

        s.delete("gone")

        rows = await sizes.snapshot()
        assert [r.name for r in rows] == ["research"]
        assert sizes.cached("gone") is None

    async def test_a_stored_path_outside_the_root_is_reported_as_zero_not_walked(
        self, tmp_path, monkeypatch
    ):
        """A tampered profiles.json must not aim the walk at somebody else's
        tree — and must not raise either, or the settings page loses its sizes."""
        s = _store(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        _make(s, "corrupt").user_data_dir = str(outside)  # as a tampered index would

        walks = []
        monkeypatch.setattr(
            profile_sizes, "measure_dir", lambda p: walks.append(p) or (5000, 1)
        )
        rows = await _sizes(s).snapshot()

        assert walks == []
        assert rows[0].bytes == 0 and rows[0].files == 0

    async def test_measuring_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """The walk must not block the loop — VNC sockets, /healthz, and MCP
        calls all share it."""
        import threading

        s = _store(tmp_path)
        _make(s, "research")
        seen: list[int] = []
        monkeypatch.setattr(
            profile_sizes, "measure_dir",
            lambda path: seen.append(threading.get_ident()) or (1, 1),
        )

        await _sizes(s).snapshot()

        assert seen and threading.get_ident() not in seen
