"""The resolved build survives the process that resolved it.

The bug: which browser actually resolved was remembered only in InstanceManager's
memory, while the binary itself lives on the volume. Every redeploy or
wake-from-sleep therefore downgraded a verified Pro install back to
``pro-unverified`` ("Pro key saved") until the user re-verified or a browser
happened to launch — nothing had changed but the process.

These tests simulate a restart the honest way: build a SECOND manager over the
same settings store, exactly as the next lifespan does, and ask it what the build
is. They also pin the two ways the stored answer must NOT be trusted — a key that
changed since, and a cache that no longer holds the binary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import license as license_service
from app.services.instances import InstanceManager
from app.services.settings import Settings, SettingsService
from app.services.views import browser_info


class _Info:
    """What cloakbrowser's validate_license returns."""

    def __init__(self, valid: bool, plan: str = "team") -> None:
        self.valid, self.plan = valid, plan


def _store(tmp_path) -> SettingsService:
    return SettingsService(tmp_path / "settings.json", tmp_path / ".dek")


def _binary(tmp_path, name: str = "chromium-148.0.7778.215.5-pro") -> str:
    """A resolved artifact that really exists, because the check stats it."""
    directory = tmp_path / ".cloakbrowser" / name
    directory.mkdir(parents=True, exist_ok=True)
    chrome = directory / "chrome"
    chrome.write_text("#!/bin/sh\n")
    return str(chrome)


class TestSurvivesARestart:
    def test_a_verified_pro_build_is_still_pro_after_a_restart(self, tmp_path):
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        InstanceManager(store).note_binary(pro, settings)

        restarted = InstanceManager(store)
        assert restarted.binary_path_for(store.load()) == pro
        assert browser_info(store.load(), restarted).build == "pro"
        assert browser_info(store.load(), restarted).pro is True

    def test_the_old_behaviour_is_what_would_fail_here(self, tmp_path):
        """Guard against a silent revert: if the status stops being written
        through, the restarted manager falls back to the saved-key branch and
        this is the assertion that catches it."""
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="a-real-key")
        never_resolved = InstanceManager(store)
        info = browser_info(store.load(), never_resolved)
        assert info.build == "pro-unverified" and info.pro is None

    def test_a_public_build_keeps_its_resolved_version_after_a_restart(self, tmp_path):
        # Without the stored path this reports the *selection* ("latest"), not
        # the version that actually resolved.
        store = _store(tmp_path)
        free = _binary(tmp_path, "chromium-146.0.7680.177.3")
        InstanceManager(store).note_binary(free, store.load())

        info = browser_info(store.load(), InstanceManager(store))
        assert info.build == "public" and info.pro is False
        assert info.version == "146.0.7680.177.3"


class TestTheStoredStatusIsNeverTrustedBlindly:
    def test_a_purged_cache_does_not_keep_reporting_pro(self, tmp_path):
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        InstanceManager(store).note_binary(pro, settings)

        Path(pro).unlink()  # volume rebuilt, cache dir emptied — Pro is gone
        restarted = InstanceManager(store)
        assert restarted.binary_path_for(store.load()) is None
        assert browser_info(store.load(), restarted).build == "pro-unverified"

    def test_a_new_key_is_not_verified_by_the_previous_ones_artifact(self, tmp_path):
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        InstanceManager(store).note_binary(pro, store.update(cloakbrowser_license_key="key-one"))

        store.update(cloakbrowser_license_key="key-two")
        restarted = InstanceManager(store)
        assert restarted.binary_path_for(store.load()) is None
        assert browser_info(store.load(), restarted).build == "pro-unverified"

    def test_a_new_version_pin_is_not_verified_by_the_previous_pins_artifact(self, tmp_path):
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(
            cloakbrowser_license_key="a-real-key", cloakbrowser_version="148.0.7778.215.5"
        )
        InstanceManager(store).note_binary(pro, settings)

        store.update(cloakbrowser_version="148.0.7778.215.2")
        assert InstanceManager(store).binary_path_for(store.load()) is None

    def test_a_failed_verification_clears_the_stored_status_too(self, tmp_path):
        """forget_binary is the UI's "this key does not work" path. If it only
        cleared memory, the next restart would resurrect the old Pro claim."""
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)
        manager.note_binary(pro, settings)

        manager.forget_binary()
        assert store.load().binary_last_path == ""
        assert InstanceManager(store).binary_path_for(store.load()) is None

    def test_clearing_the_key_leaves_no_pro_claim_behind(self, tmp_path):
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        manager = InstanceManager(store)
        manager.note_binary(pro, store.update(cloakbrowser_license_key="a-real-key"))

        # What POST /settings/cloakbrowser?action=public does, in order.
        store.update(cloakbrowser_license_key="")
        manager.forget_binary()
        assert browser_info(store.load(), InstanceManager(store)).build == "public"


class TestWhatGetsWrittenDown:
    def test_the_licence_key_is_never_stored_in_the_status(self, tmp_path):
        store = _store(tmp_path)
        key = "cb_a-very-secret-key"
        settings = store.update(cloakbrowser_license_key=key)
        InstanceManager(store).note_binary(_binary(tmp_path), settings)

        stored = store.load()
        assert key not in stored.binary_last_key_hash
        assert len(stored.binary_last_key_hash) == 64, "a sha256 digest, not the key"
        assert stored.binary_last_resolved_at > 0

    def test_an_unchanged_status_does_not_rewrite_the_settings_file(self, tmp_path):
        # note_binary runs on every launch. Rewriting the encrypted settings file
        # each time would be pure disk churn for a value that did not change.
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)

        writes = []
        original = store.update

        def counting_update(**changes):
            writes.append(changes)
            return original(**changes)

        store.update = counting_update  # type: ignore[method-assign]
        manager.note_binary(pro, settings)
        assert len(writes) == 1, "the first resolution is written down"
        for _ in range(5):
            manager.note_binary(pro, store.load())
        assert len(writes) == 1, "an unchanged status is not rewritten"

    def test_a_status_that_cannot_be_written_never_breaks_a_launch(self, tmp_path):
        # A browser that launched has launched. Failing to record which build it
        # was is a degraded status, not a failed launch.
        store = _store(tmp_path)
        pro = _binary(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)

        def boom(**changes):
            raise OSError("read-only volume")

        store.update = boom  # type: ignore[method-assign]
        manager.note_binary(pro, settings)
        assert manager.binary_path_for(settings) == pro, "memory still answers"


class TestTheBootEstablishesIt:
    """Persisting a status keeps an answer; it cannot invent one.

    Only the Verify button and a successful launch ever recorded a build, so a
    server that has done neither reports `pro-unverified` indefinitely — with a
    saved key and a Pro binary sitting on the volume. That is the state every
    existing deployment upgraded into, and it is why the status looked unchanged
    after the store started remembering it.
    """

    @pytest.fixture
    def package(self, monkeypatch, tmp_path):
        """Stand in for the cloakbrowser package, resolving a real file."""

        class Fake:
            info: object = _Info(True)
            path: str = _binary(tmp_path)
            validate_calls: list = []
            ensure_calls: list = []

            def validate_license(self, key):
                self.validate_calls.append(key)
                return self.info

            def ensure_binary(self, license_key=None, browser_version=None):
                self.ensure_calls.append((license_key, browser_version))
                return self.path

        fake = Fake()
        import cloakbrowser.browser
        import cloakbrowser.license

        monkeypatch.setattr(cloakbrowser.license, "validate_license", fake.validate_license)
        monkeypatch.setattr(cloakbrowser.browser, "ensure_binary", fake.ensure_binary)
        return fake

    @pytest.mark.asyncio
    async def test_a_server_that_never_verified_still_learns_its_build(
        self, tmp_path, package,
    ):
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)
        assert browser_info(store.load(), manager).build == "pro-unverified"

        await license_service.establish(manager, store)

        assert browser_info(store.load(), manager).build == "pro"
        assert store.load().binary_last_path == package.path, "and written down"

    @pytest.mark.asyncio
    async def test_what_it_established_survives_the_next_restart(self, tmp_path, package):
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="a-real-key")
        await license_service.establish(InstanceManager(store), store)

        assert browser_info(store.load(), InstanceManager(store)).build == "pro"

    @pytest.mark.asyncio
    async def test_a_later_boot_does_not_touch_the_licensing_server(
        self, tmp_path, package,
    ):
        """The reason this is cheap enough to do on every boot: once the volume
        has the answer, waking the container costs no round-trip at all."""
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="a-real-key")
        await license_service.establish(InstanceManager(store), store)
        assert package.validate_calls == ["a-real-key"], "first boot resolves"

        assert await license_service.establish(InstanceManager(store), store) is None
        assert package.validate_calls == ["a-real-key"], "later boots read the volume"

    @pytest.mark.asyncio
    async def test_a_licensing_outage_never_erases_a_known_build(self, tmp_path, package):
        # verify() fails closed on an outage, which is exactly when erasing a
        # good status would strand a paying user on the public build.
        store = _store(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)
        manager.note_binary(package.path, settings)

        package.info = None  # licensing server unreachable
        assert await license_service.establish(manager, store) is None
        assert browser_info(store.load(), manager).build == "pro"

    @pytest.mark.asyncio
    async def test_a_rejected_key_at_boot_is_reported_not_recorded(self, tmp_path, package):
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="totally-bogus-key-123")
        manager = InstanceManager(store)
        package.info = _Info(False, "unknown")  # licensing rejects it

        report = await license_service.establish(manager, store)

        assert report is not None and not report.ok
        assert "rejected this licence key" in report.message
        assert store.load().binary_last_path == "", "a refused key records nothing"
        assert browser_info(store.load(), manager).build == "pro-unverified"

    @pytest.mark.asyncio
    async def test_a_public_server_learns_its_version_too(self, tmp_path, package):
        store = _store(tmp_path)
        package.path = _binary(tmp_path, "chromium-146.0.7680.177.3")

        await license_service.establish(InstanceManager(store), store)

        info = browser_info(store.load(), InstanceManager(store))
        assert info.build == "public" and info.version == "146.0.7680.177.3"
        assert package.validate_calls == [], "a blank key never contacts licensing"

    @pytest.mark.asyncio
    async def test_a_stored_path_that_no_longer_exists_is_re_resolved(
        self, tmp_path, package,
    ):
        """Self-healing: the volume said Pro, the cache no longer has it, so the
        boot resolves again rather than reporting a binary that is gone."""
        store = _store(tmp_path)
        settings = store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)
        manager.note_binary(str(tmp_path / ".cloakbrowser" / "chromium-1.2.3.4-pro" / "chrome"),
                            settings)

        report = await license_service.establish(InstanceManager(store), store)

        assert report is not None and report.ok
        assert store.load().binary_last_path == package.path
        assert browser_info(store.load(), InstanceManager(store)).version == "148.0.7778.215.5"

    @pytest.mark.asyncio
    async def test_a_broken_settings_store_does_not_take_down_the_boot(
        self, tmp_path, package,
    ):
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key="a-real-key")
        manager = InstanceManager(store)

        def boom(**changes):
            raise OSError("read-only volume")

        store.update = boom  # type: ignore[method-assign]
        report = await license_service.establish(manager, store)

        assert report is not None and report.ok, "the build still resolved"
        assert manager.binary_path_for(store.load()) == package.path, "memory still answers"


class TestTheLifespanWiring:
    """The gap was never the logic — it was that nothing called it."""

    @pytest.mark.real_startup_resolve
    def test_booting_the_app_establishes_the_build(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        called: list[tuple] = []

        async def record(instances, settings) -> None:
            called.append((instances, settings))

        # The marker keeps conftest's stub out of the way; the licence call
        # itself is still faked, so no network and no download.
        monkeypatch.setattr("app.services.license.establish", record)

        with TestClient(app) as client:
            client.get("/healthz")

        assert called, "the lifespan never asked which build it runs"
        instances, settings = called[0]
        assert instances is app.state.instances and settings is app.state.settings


class TestUnchangedBehaviour:
    def test_a_settings_file_written_before_this_change_still_loads(self, tmp_path):
        # The new fields default, so an existing volume is readable and simply
        # reports pro-unverified until the next verify or launch — which is what
        # it did before.
        settings = Settings.model_validate({"cloakbrowser_license_key": "k"})
        assert settings.binary_last_path == ""
        store = _store(tmp_path)
        store.save(settings)
        assert InstanceManager(store).binary_path_for(store.load()) is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_key_is_public_with_or_without_a_stored_path(self, tmp_path, blank):
        store = _store(tmp_path)
        store.update(cloakbrowser_license_key=blank)
        assert browser_info(store.load(), InstanceManager(store)).build == "public"
