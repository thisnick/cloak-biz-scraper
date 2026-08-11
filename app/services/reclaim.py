"""The one containment rule behind everything this app deletes on the volume.

Two separate reclaim paths end in an rmtree: the old browser builds under the
cloakbrowser cache, and the evidence a finished task captured. Both name their
target from data we did not write — a settings field, a directory listing on a
volume — and both sit two directories above ``settings.json`` and the ``.dek``
that decrypts it. ``ProfileStore._removable_dir`` already applies exactly this
rule to a profile's user_data_dir; this is the same rule, factored out, so the
three cannot drift apart.

The rule: a destructive action may only ever remove a **direct child of a root
it resolved itself**. The entry is canonicalized, must land strictly inside that
root, must not *be* the root, and must not be nested any deeper. Anything else
is refused and left exactly where it is — a hostile or merely odd entry is not
worth deleting on a guess.

**A symlink is removed, never followed.** ``resolve()`` on a link hands back a
path outside the root, which is precisely the tree we have no business touching.
So a link is identified as a link first and only ever unlinked: the link is ours
to remove because it lives in our directory, and its target is not.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class Unsafe(ValueError):
    """Refused: this entry is not a direct child of the root we resolved."""


def removable_child(root: Path | str, entry: Path | str) -> Path:
    """The canonical path to remove for ``entry``, or raise.

    Callers remove the Path returned here, never the one they passed in. For a
    symlink that is the link itself; for anything else it is the resolved
    directory, which must be one level below the resolved root.
    """
    entry = Path(entry)
    try:
        canonical_root = Path(root).resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        raise Unsafe(f"refusing to remove {entry.name!r}: {root} cannot be resolved") from exc

    if entry.is_symlink():
        try:
            parent = entry.parent.resolve()
        except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
            raise Unsafe(f"refusing to remove the link {entry.name!r}") from exc
        if parent != canonical_root:
            raise Unsafe(
                f"refusing to remove the link {entry.name!r}: it is not directly inside {root}"
            )
        return entry

    try:
        target = entry.resolve()
        relative = target.relative_to(canonical_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Unsafe(
            f"refusing to remove {entry.name!r}: it resolves outside {root}"
        ) from exc
    if not relative.parts:
        raise Unsafe(f"refusing to remove {entry.name!r}: it is {root} itself")
    if len(relative.parts) != 1:
        raise Unsafe(
            f"refusing to remove {entry.name!r}: it is nested below {root}, not a child of it"
        )
    return target


def remove_child(root: Path | str, entry: Path | str) -> bool:
    """Remove one direct child of ``root``. Raises Unsafe rather than guess.

    Best effort once the containment check has passed, in the same spirit as
    ``rmtree(ignore_errors=True)``: a file that cannot be removed is not worth
    failing the whole reclaim over. The return value is the honest one — whether
    the entry is actually gone — so a caller can report what it really freed.
    """
    target = removable_child(root, entry)
    try:
        if target.is_symlink() or not target.is_dir():
            target.unlink(missing_ok=True)
        else:
            shutil.rmtree(target, ignore_errors=True)
    except OSError:
        return False
    # lexists, not exists: a dangling link that survived is still there.
    return not os.path.lexists(target)


def children(root: Path | str) -> list[Path]:
    """The direct entries of ``root``, sorted; empty when it does not exist.

    Never raises. A missing evidence or cache directory is the state of a volume
    nothing has written to yet, not an error worth failing a measurement over.
    """
    try:
        return sorted(Path(root).iterdir())
    except OSError:
        return []
