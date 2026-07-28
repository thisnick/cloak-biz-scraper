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

from app.services.instances import InstanceManager
from app.services.settings import Settings, SettingsService
from app.services.views import browser_info


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
