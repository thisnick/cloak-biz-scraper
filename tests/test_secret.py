"""APP_SECRET has one source: the environment, read directly by SecretService.

These tests pin environment authority, the absence of a second persisted copy,
and the independent volume key used to encrypt application settings.
"""
from __future__ import annotations

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
        """Editing the Railway variable and redeploying is how it changes."""
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


class TestThereIsOnlyOneSource:
    def test_there_is_no_in_app_rotation(self):
        assert not hasattr(SecretService(), "rotate"), (
            "rotate() is back — the secret is the Railway variable, changed there"
        )

    def test_nothing_is_written_to_the_volume(self, tmp_path, monkeypatch):
        """The environment value must not be persisted as a second source."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("APP_SECRET", GOOD)
        SecretService().bootstrap()
        assert not (tmp_path / "auth.json").exists(), "APP_SECRET was copied to the volume"


def test_changing_the_secret_never_strands_the_settings(tmp_path, monkeypatch):
    """Settings use the volume DEK, never APP_SECRET, as their encryption key."""
    monkeypatch.setenv("APP_SECRET", GOOD)
    settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    settings.update(proxy_host="proxy.example.com", notion_db_id="db-123")

    monkeypatch.setenv("APP_SECRET", OTHER)  # edit the Railway variable, redeploy
    reopened = SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()
    assert reopened.proxy_host == "proxy.example.com"
    assert reopened.notion_db_id == "db-123"
