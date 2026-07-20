"""ProfileStore: the durable-identity operations behind the profiles feature.

The load-bearing property is that a rename preserves cookies, and a delete
actually destroys them — both proven here against the real filesystem.
"""
from __future__ import annotations

import pathlib

import pytest

from app.services.profiles import DEFAULT_PROFILE, ProfileError, ProfileStore


def store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def _make(s, name, **geo):
    return s.get_or_create(name, default_country="US", default_region="california", **geo)


def _cookie(profile) -> pathlib.Path:
    """A stand-in for the cookie jar: a file inside the profile's user_data_dir."""
    return pathlib.Path(profile.user_data_dir) / "Cookies"


class TestCreate:
    def test_new_profile_gets_its_own_dir_and_identity(self, tmp_path):
        s = store(tmp_path)
        p = _make(s, "research")
        assert pathlib.Path(p.user_data_dir).is_dir()
        assert p.country == "US" and p.session_token and p.fingerprint_seed

    def test_same_name_returns_the_same_profile(self, tmp_path):
        s = store(tmp_path)
        assert _make(s, "research").user_data_dir == _make(s, "research").user_data_dir


class TestRenamePreservesCookies:
    def test_cookies_survive_a_rename(self, tmp_path):
        s = store(tmp_path)
        p = _make(s, "old")
        _cookie(p).write_text("session=abc123")           # "log in"
        s.rename("old", "new")
        renamed = {x.name: x for x in s.all()}["new"]
        assert _cookie(renamed).read_text() == "session=abc123"  # still logged in
        assert renamed.user_data_dir == p.user_data_dir          # same jar
        assert renamed.session_token == p.session_token          # same identity
        assert "old" not in {x.name for x in s.all()}

    def test_recreating_the_old_name_does_not_reattach_the_jar(self, tmp_path):
        """The collision the unique-dir change guards against: after old->new,
        making 'old' again must be a FRESH jar, not new's cookies."""
        s = store(tmp_path)
        p = _make(s, "old")
        _cookie(p).write_text("session=abc123")
        s.rename("old", "new")
        fresh = _make(s, "old")
        assert fresh.user_data_dir != p.user_data_dir
        assert not _cookie(fresh).exists()

    def test_rename_to_an_existing_name_is_refused(self, tmp_path):
        s = store(tmp_path)
        _make(s, "a"); _make(s, "b")
        with pytest.raises(ProfileError):
            s.rename("a", "b")

    def test_rename_a_missing_profile_is_refused(self, tmp_path):
        with pytest.raises(ProfileError):
            store(tmp_path).rename("nope", "x")


class TestDelete:
    def test_delete_removes_the_profile_and_its_jar(self, tmp_path):
        s = store(tmp_path)
        p = _make(s, "gone")
        _cookie(p).write_text("x")
        assert s.delete("gone") is True
        assert "gone" not in {x.name for x in s.all()}
        assert not pathlib.Path(p.user_data_dir).exists()   # rmtree actually ran

    def test_the_default_profile_cannot_be_deleted(self, tmp_path):
        s = store(tmp_path)
        s.ensure_default(default_country="US", default_region="california")
        with pytest.raises(ProfileError):
            s.delete(DEFAULT_PROFILE)

    def test_deleting_a_missing_profile_is_false_not_error(self, tmp_path):
        assert store(tmp_path).delete("nope") is False


class TestEnsureDefault:
    def test_seeds_a_fresh_default_when_none_exists(self, tmp_path):
        s = store(tmp_path)
        d = s.ensure_default(default_country="US", default_region="california")
        assert d.name == DEFAULT_PROFILE and pathlib.Path(d.user_data_dir).is_dir()

    def test_migrates_a_legacy_agent_keeping_its_cookies(self, tmp_path):
        s = store(tmp_path)
        agent = _make(s, "agent")
        _cookie(agent).write_text("session=legacy")
        d = s.ensure_default(default_country="US", default_region="california")
        assert d.name == DEFAULT_PROFILE
        assert d.user_data_dir == agent.user_data_dir            # same jar migrated
        assert _cookie(d).read_text() == "session=legacy"
        assert "agent" not in {x.name for x in s.all()}

    def test_is_idempotent_and_leaves_agent_alone_if_default_exists(self, tmp_path):
        s = store(tmp_path)
        _make(s, "agent")
        first = s.ensure_default(default_country="US", default_region="california")
        # A second call is a no-op; and since Default now exists, a separate agent
        # (recreated) is left untouched.
        again = s.ensure_default(default_country="US", default_region="california")
        assert first.user_data_dir == again.user_data_dir
        a2 = _make(s, "agent")
        s.ensure_default(default_country="US", default_region="california")
        assert "agent" in {x.name for x in s.all()}  # not migrated a second time
        assert a2.name == "agent"


class TestSetGeo:
    def test_updates_country_and_region(self, tmp_path):
        s = store(tmp_path)
        _make(s, "p")
        s.set_geo("p", country="GB", region="london")
        p = {x.name: x for x in s.all()}["p"]
        assert p.country == "GB" and p.region == "london"

    def test_persists_across_reload(self, tmp_path):
        s = store(tmp_path)
        _make(s, "p")
        s.rename("p", "q")
        s.set_geo("q", country="FR", region="paris")
        reopened = ProfileStore(tmp_path / "profiles")
        q = {x.name: x for x in reopened.all()}["q"]
        assert q.country == "FR" and q.region == "paris"
