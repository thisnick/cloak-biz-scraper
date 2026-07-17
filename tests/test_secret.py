"""APP_SECRET is the environment variable, read every boot — nothing else.

This replaces the volume-authoritative model (seed-once, in-app rotate,
APP_SECRET_RESET recovery). Step 5 measured away the two hazards that model
guarded against — Railway's secret() is stable across redeploys and readable —
so the secret is now simply the Railway variable. These tests pin that, and pin
that the old machinery is gone rather than merely unused.
"""
from __future__ import annotations

import pytest

from app.services.secret import SecretService
from app.services.settings import SettingsService

GOOD = "a-long-enough-secret-0001"
OTHER = "a-different-long-secret-2"


class TestEnvIsTheSourceOfTruth:
    def test_the_env_value_is_the_secret(self, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        assert SecretService().bootstrap() == GOOD
        assert SecretService().current() == GOOD

    def test_a_changed_env_value_takes_effect(self, monkeypatch):
        """Editing the Railway variable and redeploying IS how the secret changes.

        The old model deliberately ignored a changed env on later boots, because
        the volume copy was authoritative and a re-read would have reverted an
        in-app rotation. There is no volume copy now, so the env value simply
        wins — which is the whole point of the simplification.
        """
        monkeypatch.setenv("APP_SECRET", GOOD)
        assert SecretService().current() == GOOD
        monkeypatch.setenv("APP_SECRET", OTHER)  # the redeploy Railway would do
        assert SecretService().current() == OTHER

    def test_whitespace_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("APP_SECRET", f"  {GOOD}  ")
        assert SecretService().current() == GOOD

    def test_missing_secret_is_not_fatal_and_says_so(self, monkeypatch):
        """A crash loop tells a Railway user nothing; None lets the login page
        tell them exactly what to set."""
        monkeypatch.delenv("APP_SECRET", raising=False)
        assert SecretService().bootstrap() is None
        assert SecretService().current() is None

    def test_an_empty_env_value_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("APP_SECRET", "   ")
        assert SecretService().current() is None


class TestVerify:
    def test_right_and_wrong(self, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        s = SecretService()
        assert s.verify(GOOD)
        assert not s.verify(OTHER)
        assert not s.verify("")

    def test_unconfigured_verifies_nothing(self, monkeypatch):
        monkeypatch.delenv("APP_SECRET", raising=False)
        s = SecretService()
        assert not s.verify("")
        assert not s.verify("anything")


class TestTheOldMachineryIsGone:
    """Not just unused — removed. If any of these come back, so does the
    two-places confusion the change existed to delete."""

    def test_there_is_no_in_app_rotation(self):
        assert not hasattr(SecretService(), "rotate"), (
            "rotate() is back — the secret is the Railway variable, changed there"
        )

    def test_nothing_is_written_to_the_volume(self, tmp_path, monkeypatch):
        """The secret used to be encrypted onto the volume as auth.json. It must
        not be persisted at all now — env is the only home."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("APP_SECRET", GOOD)
        SecretService().bootstrap()
        assert not (tmp_path / "auth.json").exists(), "a secret file reappeared on the volume"

    def test_APP_SECRET_RESET_does_nothing(self, monkeypatch):
        """The recovery flag is meaningless now: there is no stored value to
        override, so a boot with the flag set behaves like any other boot."""
        monkeypatch.setenv("APP_SECRET", GOOD)
        monkeypatch.setenv("APP_SECRET_RESET", "true")
        assert SecretService().current() == GOOD
        # And changing APP_SECRET still just works, flag or no flag.
        monkeypatch.setenv("APP_SECRET", OTHER)
        assert SecretService().current() == OTHER


def test_changing_the_secret_never_strands_the_settings(tmp_path, monkeypatch):
    """The property that made rotation safe before, now doing nothing but staying
    true: settings are encrypted with the volume DEK, never with APP_SECRET, so
    changing the secret cannot put them behind a key nobody has.
    """
    monkeypatch.setenv("APP_SECRET", GOOD)
    settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    settings.update(proxy_host="proxy.example.com", notion_db_id="db-123")

    monkeypatch.setenv("APP_SECRET", OTHER)  # edit the Railway variable, redeploy
    reopened = SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()
    assert reopened.proxy_host == "proxy.example.com"
    assert reopened.notion_db_id == "db-123"
