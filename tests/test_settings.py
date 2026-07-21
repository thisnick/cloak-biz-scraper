"""The settings store: seeding, persistence, encryption, and validation."""
from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.services.crypto import Cipher, DecryptError, load_or_create_dek
from app.services.settings import Settings, SettingsService


@pytest.fixture
def store(tmp_path):
    def _make():
        return SettingsService(tmp_path / "settings.json", tmp_path / ".dek")

    return _make


def test_first_boot_seeds_from_env_then_ignores_it(store, tmp_path, monkeypatch):
    monkeypatch.setenv("EVOMI_PROXY_USER", "seeded-user")
    monkeypatch.setenv("MAX_INSTANCES", "7")
    assert store().load().proxy_user == "seeded-user"

    # Second boot: the volume is authoritative, so a changed env is ignored —
    # otherwise every restart would silently revert what the user set in the UI.
    monkeypatch.setenv("EVOMI_PROXY_USER", "changed-in-env")
    fresh = store().load()
    assert fresh.proxy_user == "seeded-user"
    assert fresh.max_instances == 7


def test_settings_survive_a_new_process(store):
    store().update(proxy_host="proxy.example.com", max_instances=6)
    reopened = store().load()
    assert reopened.proxy_host == "proxy.example.com"
    assert reopened.max_instances == 6


def test_file_on_disk_is_not_readable_plaintext(store, tmp_path):
    store().update(cloakbrowser_license_key="cb_supersecret", proxy_password="hunter2")
    raw = (tmp_path / "settings.json").read_bytes()
    assert b"cb_supersecret" not in raw
    assert b"hunter2" not in raw
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_dek_is_stable_and_private(tmp_path):
    path = tmp_path / ".dek"
    first = load_or_create_dek(path)
    assert load_or_create_dek(path) == first, "a second boot must not mint a new key"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_settings_are_not_keyed_on_app_secret(store, monkeypatch):
    """Rotating APP_SECRET must never strand the settings."""
    monkeypatch.setenv("APP_SECRET", "original")
    store().update(proxy_host="pinned.example.com")
    monkeypatch.setenv("APP_SECRET", "rotated-completely-different")
    assert store().load().proxy_host == "pinned.example.com"


def test_wrong_dek_fails_loudly(tmp_path):
    service = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    service.update(proxy_host="host")
    (tmp_path / ".dek").write_bytes(Fernet.generate_key())
    with pytest.raises(DecryptError):
        SettingsService(tmp_path / "settings.json", tmp_path / ".dek").load()


def test_corrupt_dek_reports_rather_than_regenerating(tmp_path):
    path = tmp_path / ".dek"
    path.write_bytes(b"not-a-key")
    with pytest.raises(DecryptError):
        Cipher.from_volume(path)


def test_redacted_hides_secrets_but_keeps_shape(store):
    s = store().update(cloakbrowser_license_key="cb_abc", proxy_password="pw", proxy_user="u")
    red = s.redacted()
    assert red["cloakbrowser_license_key"] == "***"
    assert red["proxy_password"] == "***"
    assert red["proxy_user"] == "u"
    assert red["max_instances"] == 4


def test_redacted_does_not_invent_a_secret_that_is_unset(store):
    assert store().load().redacted()["proxy_password"] == ""


class TestVersionPin:
    def test_empty_means_latest(self):
        assert Settings().cloakbrowser_version == ""

    def test_valid_pin_accepted(self):
        assert Settings(cloakbrowser_version="148.0.7778.215.2").cloakbrowser_version == (
            "148.0.7778.215.2"
        )

    @pytest.mark.parametrize(
        "bad", ["latest", "v148.0.7778.215.2", "148.x", "148.0.7778.215.2; rm -rf /", "../../etc"]
    )
    def test_malformed_pin_rejected(self, bad):
        # The pin is interpolated into cache paths and download URLs, so garbage
        # must never reach that far.
        with pytest.raises(ValueError, match="Invalid browser version pin"):
            Settings(cloakbrowser_version=bad)

    def test_malformed_pin_never_reaches_the_volume(self, store, tmp_path):
        store().update(proxy_host="good")
        with pytest.raises(ValueError):
            store().update(cloakbrowser_version="nonsense")
        assert store().load().cloakbrowser_version == ""
        assert store().load().proxy_host == "good"


class TestPoolBudget:
    def test_defaults(self):
        s = Settings()
        assert (s.max_instances, s.interactive_reserve, s.task_budget) == (4, 1, 3)

    def test_reserve_must_leave_room_for_tasks(self):
        with pytest.raises(ValueError, match="must be less than"):
            Settings(max_instances=2, interactive_reserve=2)

    def test_zero_reserve_gives_tasks_everything(self):
        assert Settings(max_instances=4, interactive_reserve=0).task_budget == 4


class TestProxyConfigured:
    def test_false_when_empty(self):
        assert not Settings().proxy_configured()

    def test_false_when_partial(self):
        assert not Settings(proxy_user="u", proxy_host="h").proxy_configured()

    def test_true_when_complete(self):
        assert Settings(
            proxy_user="u", proxy_password="p", proxy_host="h", proxy_port="1000"
        ).proxy_configured()

    def test_direct_and_partial_are_distinct_states(self):
        direct = Settings()
        partial = Settings(proxy_user="u")
        assert direct.proxy_present() is False and direct.proxy_status() == "direct"
        assert partial.proxy_present() is True and partial.proxy_status() == "incomplete"
