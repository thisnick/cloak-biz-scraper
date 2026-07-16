"""Encryption at rest for the settings store.

Keyed on a volume-local data key (`/data/.dek`), deliberately **not** on
APP_SECRET: the settings must survive a secret rotation. If the DEK were derived
from APP_SECRET, rotating it in Railway would strand every stored setting behind
a key nobody has any more.

Be honest about what this buys. The DEK sits on the same volume as the
ciphertext, so anyone who can read the volume can read the settings — this is
not a defence against an attacker with volume access, and the docs must not
claim otherwise. It defends against casual exposure of data at rest: a disk
snapshot, a backup copied somewhere careless, a support bundle.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("cloakbiz.crypto")


class DecryptError(RuntimeError):
    """Ciphertext could not be decrypted with the DEK on this volume."""


def load_or_create_dek(path: Path) -> bytes:
    """Return the volume's data key, generating it on first boot.

    Written 0600 via O_EXCL so a concurrent boot can never race two keys into
    place — the loser reads the winner's key rather than silently overwriting it
    and orphaning the settings it protects.
    """
    if path.exists():
        key = path.read_bytes().strip()
        _validate(key, path)
        return key

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        key = path.read_bytes().strip()
        _validate(key, path)
        return key
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    logger.info("generated a new data encryption key at %s", path)
    return key


def _validate(key: bytes, path: Path) -> None:
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise DecryptError(
            f"The data key at {path} is not a valid Fernet key ({exc}). It has been "
            f"corrupted or truncated; the settings encrypted with it cannot be read. "
            f"Delete both {path} and the settings file to start over."
        ) from exc


class Cipher:
    """Fernet (AES-128-CBC + HMAC-SHA256) over the volume's data key."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_volume(cls, dek_path: Path) -> "Cipher":
        return cls(load_or_create_dek(dek_path))

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise DecryptError(
                "Stored settings could not be decrypted with this volume's data key. "
                "The settings file and the .dek came from different volumes, or one of "
                "them was replaced."
            ) from exc
