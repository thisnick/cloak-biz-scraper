"""The licence gate.

The bug these exist to prevent: `ensure_binary` does not raise on a bad key, it
returns the FREE browser. Anything that treats "it did not throw" as "Pro works"
reports success while resolving a binary the Step 0 fonts gate never covered —
which is the one conclusion this whole product rests on.
"""
from __future__ import annotations

import pytest

from app.services import license as license_service
from app.services.license import (
    LicenseNotConfigured,
    LicenseNotPro,
    is_pro,
    resolve_pro_binary,
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
        ensure_called: bool = False

        def validate_license(self, key):
            return self.info

        def ensure_binary(self, license_key=None, browser_version=None):
            self.ensure_called = True
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


class TestResolveProBinary:
    def test_valid_key_returns_the_pro_path(self, package):
        assert resolve_pro_binary("real-key") == PRO

    def test_empty_key_refuses(self, package):
        with pytest.raises(LicenseNotConfigured):
            resolve_pro_binary("")

    def test_invalid_key_refuses_and_names_the_plan(self, package):
        package.info = Info(False, "unknown")
        with pytest.raises(LicenseNotPro, match="rejected this licence key"):
            resolve_pro_binary("totally-bogus-key-123")

    def test_invalid_key_never_downloads_the_free_binary(self, package):
        # Bailing before ensure_binary keeps 150 MB of a browser we refuse to run
        # off the user's volume.
        package.info = Info(False, "unknown")
        with pytest.raises(LicenseNotPro):
            resolve_pro_binary("bogus")
        assert not package.ensure_called

    def test_licensing_outage_with_nothing_cached_fails_closed(self, package):
        # validate_license returns None only when the server is unreachable AND
        # no successful validation was ever cached on this volume. Measured: that
        # is precisely when ensure_binary hands back free 146.
        package.info = None
        with pytest.raises(LicenseNotPro, match="licensing server did not respond"):
            resolve_pro_binary("real-key")

    def test_free_path_refused_even_when_validation_says_yes(self, package):
        # The claim and the artefact disagree; believe the artefact.
        package.path = FREE
        with pytest.raises(LicenseNotPro, match="not the Pro build"):
            resolve_pro_binary("real-key")


class TestVerifyReportsHonestly:
    @pytest.mark.asyncio
    async def test_valid_key_reports_pro_and_the_resolved_version(self, package):
        report = await license_service.verify("real-key")
        assert report.ok
        assert report.version == "148.0.7778.215.5"
        assert "Pro" in report.message

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
    async def test_no_key_asks_for_one(self, package):
        report = await license_service.verify("")
        assert not report.ok and "No licence key yet" in report.message


class TestLaunchRefusesFree:
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
