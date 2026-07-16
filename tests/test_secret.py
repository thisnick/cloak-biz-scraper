"""The volume-authoritative APP_SECRET (#17): seeding, rotation, and the
recovery path — plus the trap on the other side of the recovery path."""
from __future__ import annotations

import pytest

from app.services.secret import SecretService, WeakSecret
from app.services.settings import SettingsService

GOOD = "a-long-enough-secret-0001"
OTHER = "a-different-long-secret-2"


@pytest.fixture
def make(tmp_path):
    def _make():
        return SecretService(tmp_path / "auth.json", tmp_path / ".dek")

    return _make


class TestSeeding:
    def test_env_seeds_on_first_boot(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        assert make().bootstrap() == GOOD

    def test_volume_wins_on_every_later_boot(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        # The whole point of #17: an env var that won re-reads would silently
        # revert the UI's rotation on every restart.
        monkeypatch.setenv("APP_SECRET", "changed-in-railway-9999")
        assert make().bootstrap() == GOOD

    def test_no_secret_anywhere_is_not_fatal(self, make, monkeypatch):
        monkeypatch.delenv("APP_SECRET", raising=False)
        # A crash loop tells a Railway user nothing; the login page tells them
        # exactly what to set.
        assert make().bootstrap() is None
        assert make().current() is None

    def test_stored_secret_is_not_plaintext_on_disk(self, make, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        assert GOOD.encode() not in (tmp_path / "auth.json").read_bytes()


class TestVerify:
    def test_right_and_wrong(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        service = make()
        service.bootstrap()
        assert service.verify(GOOD)
        assert not service.verify(OTHER)
        assert not service.verify("")

    def test_unconfigured_verifies_nothing(self, make, monkeypatch):
        monkeypatch.delenv("APP_SECRET", raising=False)
        service = make()
        service.bootstrap()
        assert not service.verify("")
        assert not service.verify("anything")


class TestRotation:
    def test_rotate_then_reopen(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        make().rotate(OTHER)
        assert make().bootstrap() == OTHER, "a restart must not undo a rotation"

    def test_short_secret_refused(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        service = make()
        service.bootstrap()
        with pytest.raises(WeakSecret):
            service.rotate("short")
        assert service.current() == GOOD, "a refused rotation must change nothing"


class TestResetRecovery:
    """APP_SECRET_RESET exists so a forgotten rotated secret cannot brick a
    deployment nobody can shell into."""

    def test_reset_adopts_the_new_env_value(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        make().rotate("forgotten-secret-forever")

        monkeypatch.setenv("APP_SECRET", OTHER)
        monkeypatch.setenv("APP_SECRET_RESET", "true")
        assert make().bootstrap() == OTHER

    def test_reset_recovers_even_to_the_original_seed_value(self, make, monkeypatch):
        # The obvious implementation — "skip if this value was already used" —
        # gets this wrong and leaves the user bricked: resetting back to the
        # value first-boot seeded from is the most likely thing they would try.
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        make().rotate("forgotten-secret-forever")

        monkeypatch.setenv("APP_SECRET_RESET", "true")  # APP_SECRET still GOOD
        assert make().bootstrap() == GOOD

    def test_a_left_behind_flag_does_not_revert_a_later_rotation(self, make, monkeypatch):
        """The trap. Railway variables are sticky and nobody removes the flag."""
        monkeypatch.setenv("APP_SECRET", GOOD)
        monkeypatch.setenv("APP_SECRET_RESET", "true")
        make().bootstrap()          # reset consumed
        make().rotate(OTHER)        # user rotates afterwards, flag still set

        # If a set flag simply meant "re-seed", this restart would silently throw
        # the rotation away and hand back GOOD — the exact un-rotatable-env-var
        # behaviour #17 exists to eliminate.
        assert make().bootstrap() == OTHER
        assert make().bootstrap() == OTHER, "and it must stay rotated across restarts"

    def test_reset_reapplies_when_the_env_value_changes_again(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        monkeypatch.setenv("APP_SECRET_RESET", "true")
        make().bootstrap()
        make().rotate("forgotten-again-secret")

        monkeypatch.setenv("APP_SECRET", OTHER)  # a genuinely new reset request
        assert make().bootstrap() == OTHER

    def test_flag_without_a_value_changes_nothing(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        make().rotate(OTHER)
        monkeypatch.delenv("APP_SECRET")
        monkeypatch.setenv("APP_SECRET_RESET", "true")
        assert make().bootstrap() == OTHER

    def test_flag_is_off_unless_truthy(self, make, monkeypatch):
        monkeypatch.setenv("APP_SECRET", GOOD)
        make().bootstrap()
        make().rotate(OTHER)
        for value in ("false", "0", "no", "", "maybe"):
            monkeypatch.setenv("APP_SECRET_RESET", value)
            assert make().bootstrap() == OTHER, f"{value!r} must not trigger a reset"


def test_rotation_never_strands_the_settings(tmp_path, monkeypatch):
    """The property Step 1 established, restated against the real rotation path.

    Settings are encrypted with the volume's DEK, never with APP_SECRET, so
    rotating the secret cannot put them behind a key nobody has.
    """
    monkeypatch.setenv("APP_SECRET", GOOD)
    settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    settings.update(proxy_host="proxy.example.com", notion_db_id="db-123")

    secret = SecretService(tmp_path / "auth.json", tmp_path / ".dek")
    secret.bootstrap()
    secret.rotate(OTHER)

    reopened = SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()
    assert reopened.proxy_host == "proxy.example.com"
    assert reopened.notion_db_id == "db-123"
