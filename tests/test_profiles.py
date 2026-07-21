"""ProfileStore: the durable-identity operations behind the profiles feature.

The load-bearing property is that a rename preserves cookies, and a delete
actually destroys them — both proven here against the real filesystem.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.services.profiles import DEFAULT_PROFILE, ProfileConflict, ProfileError, ProfileStore


def store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles")


def _make(s, name, **geo):
    return s.get_or_create(name, default_country="US", default_region="california", **geo)


def _cookie(profile) -> pathlib.Path:
    """A stand-in for the cookie jar: a file inside the profile's user_data_dir."""
    return pathlib.Path(profile.user_data_dir) / "Cookies"


def _reopen_with_user_data_dir(tmp_path, name: str, user_data_dir: pathlib.Path) -> ProfileStore:
    """Model a malformed/tampered profiles.json without trusting private state."""
    index = tmp_path / "profiles" / "profiles.json"
    data = json.loads(index.read_text())
    data[name]["user_data_dir"] = str(user_data_dir)
    index.write_text(json.dumps(data))
    return ProfileStore(tmp_path / "profiles")


class TestCreate:
    def test_new_profile_gets_its_own_dir_and_identity(self, tmp_path):
        s = store(tmp_path)
        p = _make(s, "research")
        assert pathlib.Path(p.user_data_dir).is_dir()
        assert p.country == "US" and p.session_token and p.fingerprint_seed

    def test_same_name_returns_the_same_profile(self, tmp_path):
        s = store(tmp_path)
        assert _make(s, "research").user_data_dir == _make(s, "research").user_data_dir

    def test_explicit_create_refuses_a_collision(self, tmp_path):
        s = store(tmp_path)
        s.create("research", default_country="US", default_region="california")
        with pytest.raises(ProfileConflict, match="already exists"):
            s.create("research", default_country="US", default_region="california")

    def test_all_and_get_return_detached_snapshots(self, tmp_path):
        s = store(tmp_path)
        original = _make(s, "research")
        listed = s.all()[0]
        fetched = s.get("research")
        listed.name = "changed-through-list"
        fetched.country = "XX"
        again = s.get("research")
        assert again.name == "research" and again.country == "US"
        assert again.session_token == original.session_token


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

    def test_corrupt_sibling_path_is_refused_and_record_is_preserved(self, tmp_path):
        s = store(tmp_path)
        original = pathlib.Path(_make(s, "corrupt").user_data_dir)
        sibling = tmp_path / "outside"
        sibling.mkdir()
        canary = sibling / "keep-me"
        canary.write_text("safe")
        s = _reopen_with_user_data_dir(tmp_path, "corrupt", sibling)

        with pytest.raises(ProfileError, match="outside the profiles root"):
            s.delete("corrupt")

        assert canary.read_text() == "safe"
        assert original.is_dir()
        assert "corrupt" in {p.name for p in s.all()}
        assert "corrupt" in {p.name for p in store(tmp_path).all()}

    def test_corrupt_root_path_is_refused_without_destroying_the_index(self, tmp_path):
        s = store(tmp_path)
        _make(s, "corrupt")
        root = tmp_path / "profiles"
        canary = root / "keep-me"
        canary.write_text("safe")
        s = _reopen_with_user_data_dir(tmp_path, "corrupt", root)

        with pytest.raises(ProfileError, match="is the profiles root"):
            s.delete("corrupt")

        assert canary.read_text() == "safe"
        assert (root / "profiles.json").is_file()
        assert "corrupt" in {p.name for p in store(tmp_path).all()}

    def test_symlink_escape_is_resolved_and_refused(self, tmp_path):
        s = store(tmp_path)
        _make(s, "corrupt")
        sibling = tmp_path / "outside"
        sibling.mkdir()
        canary = sibling / "keep-me"
        canary.write_text("safe")
        escape = tmp_path / "profiles" / "escape"
        escape.symlink_to(sibling, target_is_directory=True)
        s = _reopen_with_user_data_dir(tmp_path, "corrupt", escape)

        with pytest.raises(ProfileError, match="outside the profiles root"):
            s.delete("corrupt")

        assert canary.read_text() == "safe"
        assert escape.is_symlink()
        assert "corrupt" in {p.name for p in store(tmp_path).all()}


class TestEnsureDefault:
    def test_seeds_a_fresh_default_when_none_exists(self, tmp_path):
        s = store(tmp_path)
        d = s.ensure_default(default_country="US", default_region="california")
        assert d.name == DEFAULT_PROFILE and pathlib.Path(d.user_data_dir).is_dir()

    def test_does_not_migrate_a_legacy_agent_profile(self, tmp_path):
        # Nick's call: no migration. A pre-existing "agent" is left untouched and
        # a fresh, empty Default is seeded alongside it (agent stays selectable).
        s = store(tmp_path)
        agent = _make(s, "agent")
        _cookie(agent).write_text("session=legacy")
        d = s.ensure_default(default_country="US", default_region="california")
        assert d.name == DEFAULT_PROFILE
        assert d.user_data_dir != agent.user_data_dir            # a fresh jar, not agent's
        assert not _cookie(d).exists()                           # empty, no carried cookies
        names = {x.name for x in s.all()}
        assert "agent" in names and DEFAULT_PROFILE in names     # both present

    def test_is_idempotent_when_default_exists(self, tmp_path):
        s = store(tmp_path)
        first = s.ensure_default(default_country="US", default_region="california")
        again = s.ensure_default(default_country="US", default_region="california")
        assert first.user_data_dir == again.user_data_dir        # second call is a no-op


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
