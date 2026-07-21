"""The public/Pro selection and the keyed downgrade gate.

Blank deliberately selects public. The bug these exist to prevent is narrower:
`ensure_binary` does not raise on a bad *present* key, it returns public. Anything
that treats "it did not throw" as "Pro works" silently downgrades someone who
explicitly asked for Pro.
"""
from __future__ import annotations

import pytest

from app.services import license as license_service
from app.services.license import (
    LicenseNotPro,
    is_pro,
    resolve_browser_binary,
)

# The real cache-directory names, measured in-container.
PRO = "/data/.cloakbrowser/chromium-148.0.7778.215.5-pro/chrome"
FREE = "/data/.cloakbrowser/chromium-146.0.7680.177.3/chrome"


class Info:
    def __init__(self, valid: bool, plan: str = "team") -> None:
        self.valid, self.plan = valid, plan


@pytest.fixture
def package(monkeypatch):
    """Stand in for the cloakbrowser package's two entry points."""

    class Fake:
        info: object = Info(True)
        path: str = PRO
        validate_calls: list[str] = []
        ensure_calls: list[tuple[str | None, str | None]] = []

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


class TestIsPro:
    def test_reads_the_path_that_will_actually_run(self):
        # Ground truth: what the package unpacked, not what we asked for.
        assert is_pro(PRO)
        assert not is_pro(FREE)

    def test_a_version_that_merely_mentions_pro_is_not_pro(self):
        assert not is_pro("/data/.cloakbrowser/chromium-1.2.3.4-proto/chrome")


class TestResolveBrowserBinary:
    def test_valid_key_returns_the_pro_path(self, package):
        assert resolve_browser_binary("real-key") == PRO
        assert package.validate_calls == ["real-key"]

    def test_empty_key_deliberately_resolves_public_without_validation(self, package):
        package.path = FREE
        assert resolve_browser_binary("") == FREE
        assert package.validate_calls == [], "public mode must not contact licensing"
        assert package.ensure_calls == [(None, None)]

    def test_invalid_key_refuses_and_names_the_plan(self, package):
        package.info = Info(False, "unknown")
        with pytest.raises(LicenseNotPro, match="rejected this licence key"):
            resolve_browser_binary("totally-bogus-key-123")

    def test_invalid_key_never_downloads_the_free_binary(self, package):
        # This proves the keyed refusal occurs before the package's built-in
        # public fallback. Removing the validation guard makes this call return
        # FREE and this assertion fail.
        package.info = Info(False, "unknown")
        package.path = FREE
        with pytest.raises(LicenseNotPro):
            resolve_browser_binary("bogus")
        assert package.ensure_calls == []

    def test_licensing_outage_with_nothing_cached_fails_closed(self, package):
        # validate_license returns None only when the server is unreachable AND
        # no successful validation was ever cached on this volume. Measured: that
        # is precisely when ensure_binary hands back free 146.
        package.info = None
        with pytest.raises(LicenseNotPro, match="licensing server did not respond"):
            resolve_browser_binary("real-key")

    def test_free_path_refused_even_when_validation_says_yes(self, package):
        # The claim and the artefact disagree; believe the artefact.
        package.path = FREE
        with pytest.raises(LicenseNotPro, match="not the Pro build"):
            resolve_browser_binary("real-key")

    def test_blank_mode_will_not_mislabel_a_pro_artifact_public(self, package):
        package.path = PRO
        with pytest.raises(RuntimeError, match="Refusing to mislabel"):
            resolve_browser_binary("")


class TestVerifyReportsHonestly:
    @pytest.mark.asyncio
    async def test_valid_key_reports_pro_and_the_resolved_version(self, package):
        report = await license_service.verify("real-key")
        assert report.ok
        assert report.version == "148.0.7778.215.5"
        assert "Pro" in report.message
        assert report.pro is True and report.binary_path == PRO

    @pytest.mark.asyncio
    async def test_bogus_key_is_never_reported_as_accepted(self, package):
        """The blocker, in one test.

        Before: ensure_binary quietly returned free 146, nothing raised, and the
        UI said "Licence accepted. CloakBrowser Pro 146.0.7680.177.3 is
        downloaded and ready" — where "accepted", "Pro", and the implication it
        was usable were all false.
        """
        package.info = Info(False, "unknown")
        report = await license_service.verify("totally-bogus-key-123")

        assert not report.ok
        assert "accepted" not in report.message.lower()
        assert "Pro " not in report.message, "must not call a free binary Pro"
        assert "146" not in report.message, "must not report the free version as licensed"
        assert "rejected this licence key" in report.message

    @pytest.mark.asyncio
    async def test_outage_is_not_reported_as_success(self, package):
        package.info = None
        report = await license_service.verify("real-key")
        assert not report.ok
        assert "accepted" not in report.message.lower()

    @pytest.mark.asyncio
    async def test_no_key_reports_the_public_build_and_caveat(self, package):
        package.path = FREE
        report = await license_service.verify("")
        assert report.ok and report.pro is False and report.binary_path == FREE
        assert "public build" in report.message
        assert "fewer bypasses" in report.message
        assert "not been tested by us against the listing sites" in report.message


class TestLaunchSelection:
    def test_resolved_status_is_invalidated_when_the_selected_key_changes(self, tmp_path):
        from app.services.instances import InstanceManager
        from app.services.settings import SettingsService

        store = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        manager = InstanceManager(store)
        public = store.load()
        manager.note_binary(FREE, public)
        assert manager.binary_path_for(public) == FREE

        keyed = store.update(cloakbrowser_license_key="a-new-key")
        assert manager.binary_path_for(keyed) is None, (
            "a path resolved for public mode must never make a new key look verified"
        )

    @pytest.mark.asyncio
    async def test_an_invalid_key_cannot_launch(self, package, tmp_path, monkeypatch):
        """instances.py guarded only the EMPTY key, so an invalid one launched
        free silently — the worst version of this bug, because a running browser
        looks like everything worked."""
        from app.services.instances import InstanceManager
        from app.services.settings import SettingsService

        package.info = Info(False, "unknown")
        store = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        store.update(
            cloakbrowser_license_key="totally-bogus-key-123",
            proxy_user="u", proxy_password="p", proxy_host="h", proxy_port="1000",
        )
        manager = InstanceManager(store)

        from app.models import InstanceCreate

        with pytest.raises(LicenseNotPro):
            await manager.launch(InstanceCreate(profile="x"), origin="interactive")
        assert len(manager.running) == 0, "a refused launch must not hold a pool slot"

    @pytest.mark.asyncio
    async def test_blank_key_launches_public_and_is_passed_blank_to_browser(
        self, package, tmp_path, monkeypatch
    ):
        """Drive the real manager boundary, not just the resolver in isolation."""
        from app.models import InstanceCreate
        from app.services.instances import InstanceManager
        from app.services.profiles import ProfileStore
        from app.services.settings import SettingsService

        package.path = FREE
        store = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        manager = InstanceManager(store)
        manager.profiles = ProfileStore(tmp_path / "profiles")

        class Displays:
            async def allocate(self):
                return 100

            async def start(self, number, width, height):
                return None

            async def stop(self, number):
                return None

        class Context:
            def on(self, event, callback):
                pass

            async def close(self):
                pass

        manager.displays = Displays()
        monkeypatch.setattr(manager, "_alloc_cdp_port", lambda: 9333)

        import cloakbrowser

        launch_calls = []

        async def launch(**kwargs):
            launch_calls.append(kwargs)
            return Context()

        monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)

        inst = await manager.launch(InstanceCreate(profile="Default"), origin="interactive")
        assert inst.id in manager.running
        assert launch_calls[0]["license_key"] is None
        assert manager.binary_path_for(store.load()) == FREE
        assert package.validate_calls == [], "blank launch must not validate a licence"
