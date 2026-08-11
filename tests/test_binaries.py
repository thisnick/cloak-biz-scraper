"""Old browser builds: measured always, removed only when we know which one runs.

The volume filled up because the cloakbrowser package caches one directory per
version and the version is unpinned, so this reclaims them — and the whole risk
of the feature is deleting the binary the server is about to launch. Hence the
shape of these tests: the in-use build is read from the recorded path, an
unreadable answer removes NOTHING, and every odd entry in the cache is refused
rather than guessed at.
"""
from __future__ import annotations

import pathlib
import types

import pytest

from app.services import binaries, reclaim
from app.services.binaries import BrowserBuilds, BuildsBusy, BuildsUnknown
from app.services.instances import InstanceManager
from app.services.settings import SettingsService


@pytest.fixture
def cache(tmp_path) -> pathlib.Path:
    d = tmp_path / ".cloakbrowser"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path) -> SettingsService:
    return SettingsService(tmp_path / "settings.json", tmp_path / ".dek")


@pytest.fixture
def instances(settings) -> InstanceManager:
    """The real manager: the busy guard is only worth testing against the object
    that actually knows a browser is opening."""
    return InstanceManager(settings)


@pytest.fixture
def builds(cache, settings, instances) -> BrowserBuilds:
    return BrowserBuilds(cache, lambda: settings, instances)


def _build(cache: pathlib.Path, name: str, *, size: int = 100) -> pathlib.Path:
    """One cached build, laid out the way the package unpacks one on Linux —
    the only platform the container runs."""
    d = cache / name
    d.mkdir(parents=True)
    (d / "chrome").write_bytes(b"x" * size)
    return d


def _in_use(settings: SettingsService, directory: pathlib.Path) -> None:
    """Record a resolved binary, exactly as InstanceManager.note_binary does."""
    settings.update(binary_last_path=str(directory / "chrome"))


def _busy(instances: InstanceManager) -> None:
    instances.running["i-1"] = types.SimpleNamespace(profile="Default")


@pytest.mark.asyncio
class TestSnapshot:
    async def test_lists_every_build_with_its_version_edition_and_size(
        self, builds, cache, settings
    ):
        _build(cache, "chromium-148.0.7778.215.5-pro", size=700)
        _build(cache, "chromium-146.0.7680.177.3", size=300)
        _in_use(settings, cache / "chromium-148.0.7778.215.5-pro")

        view = await builds.snapshot()
        rows = {b.name: b for b in view.builds}

        assert rows["chromium-148.0.7778.215.5-pro"].version == "148.0.7778.215.5"
        assert rows["chromium-148.0.7778.215.5-pro"].pro is True
        assert rows["chromium-148.0.7778.215.5-pro"].bytes == 700
        assert rows["chromium-148.0.7778.215.5-pro"].files == 1
        assert rows["chromium-146.0.7680.177.3"].pro is False
        assert view.total_bytes == 1000

    async def test_the_in_use_build_comes_from_the_recorded_path(
        self, builds, cache, settings
    ):
        _build(cache, "chromium-148.0.7778.215.5-pro")
        _build(cache, "chromium-146.0.7680.177.3")
        _in_use(settings, cache / "chromium-146.0.7680.177.3")

        view = await builds.snapshot()

        assert view.in_use_known is True and view.reason == ""
        assert view.in_use.name == "chromium-146.0.7680.177.3"
        assert [b.name for b in view.stale] == ["chromium-148.0.7778.215.5-pro"]
        assert view.builds[0].in_use is True, "the one in use is listed first"

    async def test_with_nothing_recorded_nothing_is_stale_and_the_reason_says_why(
        self, builds, cache
    ):
        """Sizes still measure — a full volume must always be explainable — but
        no build may be offered for removal when none can be identified."""
        _build(cache, "chromium-148.0.7778.215.5-pro", size=700)

        view = await builds.snapshot()

        assert view.in_use_known is False and view.in_use is None
        assert view.stale == [] and view.stale_bytes == 0
        assert view.total_bytes == 700
        assert "Save & verify" in view.reason

    async def test_a_binary_nested_deeper_still_names_its_version_directory(
        self, builds, cache, settings
    ):
        """macOS unpacks Chromium.app/Contents/MacOS/Chromium inside the version
        directory, so the binary's PARENT is not the build. The top component
        under the cache is, on every platform."""
        directory = _build(cache, "chromium-148.0.7778.215.5-pro")
        settings.update(
            binary_last_path=str(directory / "Chromium.app" / "Contents" / "MacOS" / "Chromium")
        )

        assert (await builds.snapshot()).in_use.name == "chromium-148.0.7778.215.5-pro"

    async def test_a_recorded_path_outside_the_cache_is_unknown_not_all_stale(
        self, builds, cache, settings, tmp_path
    ):
        """The dangerous misreading: a path we cannot place must not turn every
        directory here into "the old ones"."""
        _build(cache, "chromium-148.0.7778.215.5-pro")
        settings.update(binary_last_path=str(tmp_path / "elsewhere" / "chrome"))

        view = await builds.snapshot()

        assert view.in_use_known is False and view.stale == []
        assert "not in this cache directory" in view.reason

    async def test_a_recorded_build_that_is_gone_is_unknown(self, builds, cache, settings):
        directory = _build(cache, "chromium-148.0.7778.215.5-pro")
        _in_use(settings, directory)
        reclaim.remove_child(cache, directory)  # a purged cache
        _build(cache, "chromium-146.0.7680.177.3")

        view = await builds.snapshot()

        assert view.in_use_known is False and view.stale == []
        assert "no longer on the volume" in view.reason

    async def test_anything_that_is_not_a_build_directory_is_not_listed(self, builds, cache):
        _build(cache, "chromium-148.0.7778.215.5-pro")
        (cache / "licence-cache.json").write_bytes(b"x" * 999)
        (cache / "downloads").mkdir()

        assert [b.name for b in (await builds.snapshot()).builds] == [
            "chromium-148.0.7778.215.5-pro"
        ]

    async def test_a_symlinked_build_is_listed_but_never_walked(
        self, builds, cache, tmp_path
    ):
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        (cache / "chromium-linked").symlink_to(outside, target_is_directory=True)

        view = await builds.snapshot()

        assert [b.name for b in view.builds] == ["chromium-linked"]
        assert view.total_bytes == 0, "5000 bytes outside the cache are not ours"

    async def test_a_missing_cache_directory_measures_as_nothing(
        self, settings, instances, tmp_path
    ):
        view = await BrowserBuilds(tmp_path / "never-existed", lambda: settings,
                                   instances).snapshot()
        assert view.builds == [] and view.in_use_known is False

    async def test_measuring_runs_off_the_event_loop(self, builds, cache, monkeypatch):
        """The loop carries VNC sockets, /healthz, and MCP calls; a 700 MB walk
        must not sit on it."""
        import threading

        _build(cache, "chromium-148.0.7778.215.5-pro")
        seen: list[int] = []
        monkeypatch.setattr(
            binaries, "measure_dir",
            lambda path: seen.append(threading.get_ident()) or (1, 1),
        )

        await builds.snapshot()

        assert seen and threading.get_ident() not in seen

    async def test_a_cached_size_is_not_re_walked_and_refresh_forces_it(
        self, builds, cache, monkeypatch
    ):
        directory = _build(cache, "chromium-148.0.7778.215.5-pro", size=100)
        assert (await builds.snapshot()).total_bytes == 100

        (directory / "extra").write_bytes(b"x" * 400)

        assert (await builds.snapshot()).total_bytes == 100          # served cached
        assert (await builds.snapshot(refresh=True)).total_bytes == 500


@pytest.mark.asyncio
class TestRemoveStale:
    async def test_removes_the_old_builds_and_keeps_the_one_in_use(
        self, builds, cache, settings
    ):
        keep = _build(cache, "chromium-148.0.7778.215.5-pro", size=700)
        old_a = _build(cache, "chromium-146.0.7680.177.3", size=300)
        old_b = _build(cache, "chromium-145.0.1.1", size=200)
        _in_use(settings, keep)

        removed = await builds.remove_stale()

        assert sorted(removed.removed) == ["chromium-145.0.1.1", "chromium-146.0.7680.177.3"]
        assert removed.bytes == 500
        assert removed.kept == "chromium-148.0.7778.215.5-pro"
        assert not old_a.exists() and not old_b.exists()
        assert (keep / "chrome").read_bytes() == b"x" * 700

    async def test_with_nothing_recorded_it_refuses_and_deletes_nothing(
        self, builds, cache
    ):
        """The guard the owner asked for by name: no recorded path means we
        cannot identify the running build, and we do not guess."""
        old = _build(cache, "chromium-146.0.7680.177.3")
        newer = _build(cache, "chromium-148.0.7778.215.5-pro")

        with pytest.raises(BuildsUnknown, match="Save & verify"):
            await builds.remove_stale()

        assert old.is_dir() and newer.is_dir()

    async def test_an_unreadable_settings_store_refuses_and_deletes_nothing(
        self, cache, instances, monkeypatch
    ):
        old = _build(cache, "chromium-146.0.7680.177.3")
        broken = types.SimpleNamespace(
            load=lambda: (_ for _ in ()).throw(OSError("volume is having a day"))
        )
        service = BrowserBuilds(cache, lambda: broken, instances)

        with pytest.raises(BuildsUnknown, match="could not be read"):
            await service.remove_stale()

        assert old.is_dir()
        # …and measuring still answers, so the page can still show the size.
        assert (await service.snapshot()).total_bytes == 100

    async def test_a_running_browser_refuses_and_deletes_nothing(
        self, builds, cache, settings, instances
    ):
        """A live Chromium has its binary mapped; removing it underneath one is
        how a browser dies mid-sweep."""
        keep = _build(cache, "chromium-148.0.7778.215.5-pro")
        old = _build(cache, "chromium-146.0.7680.177.3")
        _in_use(settings, keep)
        _busy(instances)

        with pytest.raises(BuildsBusy, match="Close every browser"):
            await builds.remove_stale()

        assert old.is_dir() and keep.is_dir()

    async def test_a_browser_that_is_only_opening_still_refuses(
        self, builds, cache, settings, instances
    ):
        """The window a scan of `running` cannot see: reserved, not yet running."""
        keep = _build(cache, "chromium-148.0.7778.215.5-pro")
        old = _build(cache, "chromium-146.0.7680.177.3")
        _in_use(settings, keep)
        instances._reserve_profile("Default")

        with pytest.raises(BuildsBusy):
            await builds.remove_stale()

        assert old.is_dir()

    async def test_a_symlinked_build_is_unlinked_and_its_target_survives(
        self, builds, cache, settings, tmp_path
    ):
        keep = _build(cache, "chromium-148.0.7778.215.5-pro")
        _in_use(settings, keep)
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        link = cache / "chromium-linked"
        link.symlink_to(outside, target_is_directory=True)

        removed = await builds.remove_stale()

        assert removed.removed == ["chromium-linked"]
        assert not link.is_symlink()
        assert (outside / "huge").read_bytes() == b"x" * 5000, "the target is not ours"

    async def test_a_poisoned_listing_is_refused_entry_by_entry(
        self, builds, cache, settings, tmp_path, monkeypatch
    ):
        """Defence in depth: even if the listing itself lied, only a direct
        child of the resolved cache may be removed."""
        keep = _build(cache, "chromium-148.0.7778.215.5-pro")
        _in_use(settings, keep)
        outside = tmp_path / "elsewhere"
        (outside / "keep").mkdir(parents=True)
        real = _build(cache, "chromium-146.0.7680.177.3", size=300)
        monkeypatch.setattr(
            builds, "_names",
            lambda: ["chromium-148.0.7778.215.5-pro", "chromium-146.0.7680.177.3",
                     "..", ".", "../elsewhere", "chromium-146.0.7680.177.3/chrome-linux"],
        )

        removed = await builds.remove_stale()

        assert removed.removed == ["chromium-146.0.7680.177.3"]
        assert not real.exists()
        assert (outside / "keep").is_dir(), "a path outside the cache was touched"
        assert cache.is_dir() and keep.is_dir(), "the cache root itself was touched"

    async def test_nothing_to_remove_is_not_an_error(self, builds, cache, settings):
        keep = _build(cache, "chromium-148.0.7778.215.5-pro")
        _in_use(settings, keep)

        removed = await builds.remove_stale()

        assert removed.removed == [] and removed.bytes == 0
        assert keep.is_dir()

    async def test_the_snapshot_after_a_removal_shows_what_is_left(
        self, builds, cache, settings
    ):
        keep = _build(cache, "chromium-148.0.7778.215.5-pro", size=700)
        _build(cache, "chromium-146.0.7680.177.3", size=300)
        _in_use(settings, keep)
        assert (await builds.snapshot()).total_bytes == 1000

        await builds.remove_stale()

        view = await builds.snapshot()
        assert [b.name for b in view.builds] == ["chromium-148.0.7778.215.5-pro"]
        assert view.total_bytes == 700
