"""The containment rule every rmtree on the volume goes through.

These are the tests that matter most in the whole reclaim feature: two
directories above the browser cache and the evidence root sit `settings.json`
and the `.dek` that decrypts it, so "which path may this remove" is the only
question worth being paranoid about. Each case proves the guard REFUSES —
raising, and leaving the thing it refused exactly where it was.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from app.services import reclaim
from app.services.reclaim import Unsafe, children, remove_child, removable_child


@pytest.fixture
def root(tmp_path) -> pathlib.Path:
    r = tmp_path / "cache"
    r.mkdir()
    return r


def _dir(parent: pathlib.Path, name: str, *, size: int = 10) -> pathlib.Path:
    d = parent / name
    (d / "nested").mkdir(parents=True)
    (d / "nested" / "f").write_bytes(b"x" * size)
    return d


class TestRemovableChild:
    def test_a_direct_child_is_returned_canonically(self, root):
        target = _dir(root, "chromium-1")
        assert removable_child(root, target) == target.resolve()

    def test_the_root_itself_is_refused(self, root):
        with pytest.raises(Unsafe, match="itself"):
            removable_child(root, root)

    def test_a_grandchild_is_refused(self, root):
        target = _dir(root, "chromium-1")
        with pytest.raises(Unsafe, match="nested below"):
            removable_child(root, target / "nested")

    def test_a_path_outside_the_root_is_refused(self, root, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(Unsafe, match="outside"):
            removable_child(root, outside)

    def test_a_traversal_out_of_the_root_is_refused(self, root):
        with pytest.raises(Unsafe, match="outside"):
            removable_child(root, root / ".." / "settings.json")

    def test_a_symlink_resolves_to_the_link_not_its_target(self, root, tmp_path):
        """The whole point: a link inside our directory is ours to unlink, and
        whatever it points at is not ours to touch."""
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        link = root / "chromium-link"
        link.symlink_to(outside, target_is_directory=True)

        assert removable_child(root, link) == link  # never outside/, never resolved

    def test_a_symlink_that_is_not_a_direct_child_is_refused(self, root, tmp_path):
        deep = root / "chromium-1"
        deep.mkdir()
        link = deep / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(Unsafe, match="not directly inside"):
            removable_child(root, link)


class TestRemoveChild:
    def test_removes_a_direct_child_tree(self, root):
        target = _dir(root, "chromium-1")
        assert remove_child(root, target) is True
        assert not target.exists()

    def test_a_symlinked_directory_is_unlinked_and_its_target_survives(self, root, tmp_path):
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        link = root / "chromium-link"
        link.symlink_to(outside, target_is_directory=True)

        assert remove_child(root, link) is True
        assert not link.is_symlink()                       # the link is gone
        assert (outside / "huge").read_bytes() == b"x" * 5000  # the tree is not

    def test_a_dangling_symlink_is_removed(self, root):
        link = root / "chromium-gone"
        link.symlink_to(root / "never-existed", target_is_directory=True)
        assert remove_child(root, link) is True
        assert not os.path.lexists(link)

    def test_a_plain_file_child_is_removed(self, root):
        stray = root / "chromium-1.zip"
        stray.write_bytes(b"x" * 10)
        assert remove_child(root, stray) is True
        assert not stray.exists()

    def test_a_refusal_leaves_the_tree_untouched(self, root, tmp_path):
        outside = tmp_path / "elsewhere"
        (outside / "keep").mkdir(parents=True)
        with pytest.raises(Unsafe):
            remove_child(root, outside)
        assert (outside / "keep").is_dir()

    def test_a_missing_child_is_not_an_error(self, root):
        assert remove_child(root, root / "never-existed") is True

    def test_a_tree_that_cannot_be_removed_reports_false(self, root, monkeypatch):
        """rmtree(ignore_errors=True) never raises, so "did it work" has to be
        answered by looking, or a banner would claim bytes it did not free."""
        target = _dir(root, "chromium-1")
        monkeypatch.setattr(reclaim.shutil, "rmtree", lambda *a, **k: None)
        assert remove_child(root, target) is False
        assert target.is_dir()


class TestChildren:
    def test_lists_direct_entries_sorted(self, root):
        _dir(root, "chromium-2")
        _dir(root, "chromium-1")
        assert [p.name for p in children(root)] == ["chromium-1", "chromium-2"]

    def test_a_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert children(tmp_path / "never-existed") == []
