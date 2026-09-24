"""Files a site handed the browser, kept where the agent that asked can fetch them.

The mirror image of services/uploads.py. There the bytes come from the agent and
the danger is a path that reads the wrong file; here the bytes come from the web
and the dangers are a site filling the volume, and a file ending up in front of
somebody it was not fetched for.

**The browser writes, but never where the caller says.** `agent-browser
download <selector> <path>` saves to any path it is given, so the `download`
verb is service-handled exactly as `screenshot` is: the caller names an element
and nothing else, and the path is a fresh landing directory minted here. What
leaves the landing directory is decided by `commit`, from the bytes that are
actually on disk.

**One ticket per file.** Each finished download gets its own `dl_<hex>`
directory with the same manifest shape an upload ticket has — subject, expiry,
one file entry — so expiry, the owner's clear and the Disk space measurement are
the rules uploads already live by, reached through the same `reclaim` helpers.
A ticket per file also makes the fetch URL a capability for exactly one file.

**The fetch token may ride in the URL, and that is a deliberate difference from
uploads.** An upload is always a curl, and curl can set a header. A download is
often wanted by the *person*, in a chat client that has no shell at all — and a
link they can click is the whole feature for them. So the ticket is accepted as
`?t=` as well as a bearer header, it is scoped to one file and one subject, it
dies with the file, and services/log_safety.py already strips query strings from
every log line.

**Bytes are bounded while they arrive, not after.** Chromium writes a download
to `<guid>.crdownload` inside the landing directory, so the agent-browser
service watches that directory while the CLI runs and cancels the download (by
that GUID) the moment it outgrows what `begin` reserved. `commit` re-checks the
finished size anyway; the watchdog is what stops 5 GB reaching the disk, the
re-check is what makes the cap true whatever the watchdog missed.

Nothing is released by hand. Files expire on the same two-hour clock uploads
use, and sweeps run only when something already asked (startup, a new download,
the settings page) — never on a timer, for the reason services/heartbeat.py
gives.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from . import reclaim, signing
from .presentation import human_size
from .profile_sizes import measure_dir
# The manifest format and the filename rules are the uploads store's, reused
# rather than restated: one reader for "is this a live ticket", one sanitiser for
# "what may a filename on this volume look like".
from .uploads import _expired, _read_manifest, _write_manifest, safe_name

logger = logging.getLogger("cloakbiz.downloads")

# ── The caps ────────────────────────────────────────────────────────────────
# The same numbers uploads use, for the same reasons: a scanned PDF or a data
# export lives well under 100 MB, and a gigabyte is as much as this volume —
# which also holds profiles, settings and the .dek — should give to files an
# agent is expected to fetch within two hours.
MAX_BYTES_PER_FILE = 100 * 1024 * 1024
DOWNLOADS_BUDGET_BYTES = 1024 * 1024 * 1024
TTL_SEC = 2 * 60 * 60

HANDLE_PREFIX = "dl_"
_HANDLE_RE = re.compile(r"^dl_[0-9a-f]{16}$")
AUD = "download"

# Landing directories sit directly under the root, like tickets, so `reclaim`
# can remove them by the same containment rule. The leading dot is what keeps
# them from ever being mistaken for a ticket: no handle starts with one.
LANDING_PREFIX = ".incoming-"
# How old an unreserved landing directory must be before a sweep removes it.
# See `_sweep`: this is what closes the gap between `begin` creating one and
# recording it.
_LANDING_GRACE_SEC = 60
# The name the CLI is told to save as. Never shown to anybody: `commit` renames
# the file to the site's own suggested name (sanitised) on the way out.
_LANDING_FILE = "download"

# What `sniff` can name from the first bytes. Anything else is reported — and
# served — as application/octet-stream: the file is always sent as an
# attachment, so the type is information for the caller, never an instruction
# for a browser to render something.
_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
)
# A zip is also every modern Office document. The extension only ever narrows a
# zip that the bytes already proved is one — it cannot make anything else claim
# to be a spreadsheet.
_ZIP_FAMILY = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_TEXT_TYPES = {".csv": "text/csv", ".json": "application/json", ".txt": "text/plain"}
OCTET = "application/octet-stream"
_EXTENSION = {
    "application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "application/zip": ".zip",
    "application/gzip": ".gz", "text/csv": ".csv", "text/plain": ".txt",
    "application/json": ".json",
}
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_SNIFF_BYTES = 4096


class DownloadsError(Exception):
    """Something about this download is refused. Every subclass says what to do."""


class NotFound(DownloadsError):
    """No such file for this ticket — or a ticket that is not this caller's."""


class Expired(DownloadsError):
    """A real download, past its TTL."""


class TooLarge(DownloadsError):
    """The file outgrew what this server will keep for one download."""


class NoRoom(DownloadsError):
    """Kept downloads are already at the global budget."""


class NothingSaved(DownloadsError):
    """The browser reported success and left no file behind."""


@dataclass(frozen=True)
class Landing:
    """One download in flight: where the browser is told to write, and how much
    it may write there before the watchdog cancels it."""

    dir: Path
    allowance: int

    @property
    def target(self) -> Path:
        return self.dir / _LANDING_FILE

    @property
    def reservation(self) -> str:
        return self.dir.name


@dataclass(frozen=True)
class KeptDownload:
    """A finished download, as the caller is told about it."""

    handle: str
    name: str
    bytes: int
    sha256: str
    content_type: str
    source_url: str
    expires_at: float
    token: str


# ── Tokens ───────────────────────────────────────────────────────────────────


def audience(handle: str) -> str:
    return f"{AUD}:{handle}"


def issue_ticket(handle: str, secret: str, *, subject: str,
                 ttl_sec: int = TTL_SEC, now: float | None = None) -> str:
    """The bearer for fetching one kept download. Audience `download:<handle>`,
    so no CDP, VNC, upload or OAuth token can stand in for it, and it cannot open
    any other download."""
    return signing.issue(
        {"aud": audience(handle), "sub": subject}, secret, ttl_sec=ttl_sec, now=now
    )


def ticket_subject(token: str | None, handle: str, secret: str | None, *,
                   now: float | None = None) -> str | None:
    """The subject a live ticket for *this* handle was minted for, else None."""
    if not handle or not _HANDLE_RE.match(handle):
        return None
    claims = signing.verify(token, secret, audience=audience(handle), now=now)
    if claims is None:
        return None
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None


# ── Content ──────────────────────────────────────────────────────────────────


def sniff(head: bytes, name: str = "") -> str:
    """What these bytes are, as far as their first few kilobytes say.

    The magic number decides. The extension is consulted only to narrow a zip
    into an Office type, or to name text that has no magic number at all — and
    text is only text when there is no NUL byte in the window and it decodes.
    """
    ext = os.path.splitext(name)[1].lower()
    for magic, content_type in _MAGIC:
        if head.startswith(magic):
            if content_type == "application/zip":
                return _ZIP_FAMILY.get(ext, content_type)
            return content_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head and b"\x00" not in head:
        # A multi-byte character cut in half by the end of a full window is
        # still text, so a full window gives up its last few bytes.
        window = head if len(head) < _SNIFF_BYTES else head[:-4]
        try:
            window.decode("utf-8")
        except UnicodeDecodeError:
            return OCTET
        return _TEXT_TYPES.get(ext, "text/plain")
    return OCTET


def _digest_and_head(path: Path) -> tuple[str, bytes]:
    digest = sha256()
    head = b""
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            if len(head) < _SNIFF_BYTES:
                head += chunk[: _SNIFF_BYTES - len(head)]
            digest.update(chunk)
    return digest.hexdigest(), head


def _file_name(suggested: str | None, content_type: str) -> str:
    """The site's suggested filename, made safe; or `download.<ext>`.

    `safe_name` keeps an extension that is really there and supplies one from
    the type only when nothing usable was given. The browser's suggestion is
    third-party text — `Content-Disposition` is whatever the site sent — so it is
    exactly as untrusted as an uploaded filename, and goes through the same rule.
    """
    raw = (suggested or "").strip()
    if not raw:
        return f"download{_EXTENSION.get(content_type, '')}"
    name = safe_name(raw, content_type)
    if name == "upload" or name.startswith("upload."):
        # safe_name's fallback stem is named for the store it was written for.
        name = "download" + name[len("upload"):]
    return name


# ── The store ────────────────────────────────────────────────────────────────


class DownloadService:
    """Landing directories for downloads in flight, tickets for finished ones.

    Holds the root and an in-memory ledger of what is in flight. The signing
    secret is passed per call, as everywhere else, so an APP_SECRET rotation
    takes effect on the restart that applies it.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # Guards admission (measure, then reserve) and commit (the ledger and
        # the manifest). The decision and the reservation must not have an
        # await between them, or eight concurrent downloads all see room for one.
        self._lock = asyncio.Lock()
        # Landing directory name -> bytes reserved for it. A landing directory
        # in this ledger is live; one on disk that is not is abandoned.
        self._reserved: dict[str, int] = {}
        self._revision = 0
        self._reclaim_lock = asyncio.Lock()

    @property
    def revision(self) -> int:
        """Bumped whenever the bytes under this root change — see
        uploads.UploadService.revision, whose cache this store shares."""
        return self._revision

    def busy(self) -> set[str]:
        """Landing directories a download is writing into right now."""
        return set(self._reserved)

    def _on_disk(self) -> int:
        """Bytes under the root, except live landing directories.

        Those are covered by their reservation, and counting them as well would
        charge the same bytes twice. An ABANDONED landing directory is counted —
        it is disk the budget has to cover until a sweep removes it.
        """
        live = self.busy()
        total = 0
        for entry in reclaim.children(self.root):
            if entry.name in live or entry.is_symlink():
                continue
            if entry.is_dir():
                total += measure_dir(entry)[0]
            else:
                try:
                    total += entry.lstat().st_size
                except OSError:  # pragma: no cover - it vanished mid-walk
                    pass
        return total

    # ── in flight ──
    async def begin(self, *, now: float | None = None) -> Landing:
        """A fresh landing directory and the number of bytes it may hold.

        Sweeps first: expired downloads are the only space ever freed without a
        human, and a new download is entitled to it. Then reserves the smaller of
        the per-file cap and whatever the budget has left, under the lock.
        """
        await self.sweep(now=now)
        async with self._lock:
            room = DOWNLOADS_BUDGET_BYTES - self._on_disk() - sum(self._reserved.values())
            allowance = min(MAX_BYTES_PER_FILE, room)
            if allowance <= 0:
                raise NoRoom(
                    f"downloaded files are already using the {human_size(DOWNLOADS_BUDGET_BYTES)} "
                    "this server keeps for them. They expire on their own within two hours; to "
                    "free the space now, open Settings → Disk space and clear downloaded files."
                )
            landing = self.root / f"{LANDING_PREFIX}{secrets.token_hex(8)}"
            landing.mkdir(parents=True)
            self._reserved[landing.name] = allowance
        return Landing(dir=landing, allowance=allowance)

    def landed_bytes(self, landing: Landing) -> tuple[int, list[str]]:
        """What the browser has written so far, and the GUIDs of partial files.

        The GUIDs are what `Browser.cancelDownload` takes, and Chromium names
        the partial file `<guid>.crdownload`, so the watchdog can cancel exactly
        the download it is measuring without having listened to any event.
        """
        total = 0
        partial: list[str] = []
        for entry in reclaim.children(landing.dir):
            try:
                total += entry.lstat().st_size
            except OSError:  # pragma: no cover - renamed under us
                continue
            if entry.name.endswith(".crdownload"):
                partial.append(entry.name[: -len(".crdownload")])
        return total, partial

    def too_large(self, landing: Landing) -> TooLarge:
        return TooLarge(
            f"that file is bigger than the {human_size(landing.allowance)} this server can "
            f"keep for one download right now (the limit is {human_size(MAX_BYTES_PER_FILE)} "
            "per file, less when other downloads are using the space). The download was "
            "cancelled."
        )

    async def commit(self, landing: Landing, *, subject: str, secret: str | None,
                     suggested_name: str | None = None, source_url: str = "",
                     now: float | None = None) -> KeptDownload:
        """Move the landed file into its own ticket and mint the fetch token.

        Only the one file the CLI was told to write is taken — never "whatever
        is in the directory", which could be a stray GUID file a click dropped
        there. It must be a regular file, not a link, and within the allowance.
        The landing directory is removed on every path out of here.
        """
        try:
            if not secret:
                raise DownloadsError(
                    "this server has no APP_SECRET set, so it cannot hand out a download link"
                )
            target = landing.target
            if target.is_symlink() or not target.is_file():
                raise NothingSaved(
                    "the browser reported the download as finished but no file arrived"
                )
            size = target.lstat().st_size
            if size > landing.allowance:
                raise self.too_large(landing)
            checksum, head = await asyncio.to_thread(_digest_and_head, target)
            content_type = sniff(head, suggested_name or "")
            name = _file_name(suggested_name, content_type)
            now = time.time() if now is None else now
            async with self._lock:
                handle = f"{HANDLE_PREFIX}{secrets.token_hex(8)}"
                ticket_dir = self.root / handle
                ticket_dir.mkdir()
                os.replace(target, ticket_dir / name)
                _write_manifest(ticket_dir, {
                    "sub": subject, "created": now, "expires": now + TTL_SEC,
                    "source_url": source_url,
                    "files": [{"name": name, "bytes": size, "sha256": checksum,
                               "content_type": content_type}],
                })
                self._revision += 1
        finally:
            self.abort(landing)
        logger.info("kept download %s (%d bytes, %s) as %s", name, size, content_type, handle)
        return KeptDownload(
            handle=handle, name=name, bytes=size, sha256=checksum,
            content_type=content_type, source_url=source_url,
            expires_at=now + TTL_SEC,
            token=issue_ticket(handle, secret, subject=subject, now=now),
        )

    def abort(self, landing: Landing) -> None:
        """Remove the landing directory and give its reservation back.

        Idempotent and safe after commit. A failed download must cost nothing,
        or a run of failures becomes its own outage.
        """
        self._reserved.pop(landing.reservation, None)
        try:
            gone = reclaim.remove_child(self.root, landing.dir)
        except reclaim.Unsafe as exc:  # pragma: no cover - we minted this path
            logger.warning("left the landing directory %s alone: %s", landing.dir.name, exc)
            return
        if gone:
            self._revision += 1
        else:  # pragma: no cover - a file we could not remove
            logger.warning("could not fully remove the landing directory %s", landing.dir.name)

    # ── fetching ──
    def open_for(self, handle: str, name: str, token: str | None, secret: str | None,
                 *, now: float | None = None) -> tuple[Path, dict]:
        """The file behind one fetch URL, or raise. **The security function.**

        The token must be live, minted for this handle, and for the subject the
        manifest records. The name must be the manifest's, and what is returned
        is the path built from the manifest and checked to stay inside the
        ticket directory — never the caller's string reused.

        One refusal for "no such ticket", "not your ticket" and "bad token": the
        difference only helps someone who should not be holding the URL.
        """
        now = time.time() if now is None else now
        subject = ticket_subject(token, handle, secret, now=now)
        if subject is None:
            raise NotFound("that download link is not valid — it may have expired")
        ticket_dir = self.root / handle
        manifest = _read_manifest(ticket_dir)
        if manifest is None or manifest.get("sub") != subject:
            raise NotFound("that download link is not valid — it may have expired")
        if _expired(manifest, now):
            raise Expired("that download has expired — downloads are kept for two hours. "
                          "Download it again from the page.")
        entry = next(
            (e for e in manifest.get("files") or [] if e.get("name") == name), None
        )
        if entry is None:
            raise NotFound("that download link is not valid — it may have expired")
        try:
            root = ticket_dir.resolve()
            target = (ticket_dir / str(entry["name"])).resolve()
        except (OSError, RuntimeError):  # pragma: no cover - defensive
            raise NotFound("that download link is not valid") from None
        if not target.is_relative_to(root) or not target.is_file():
            raise NotFound("that download link is not valid")
        return target, entry

    # ── expiry ──
    async def sweep(self, *, now: float | None = None,
                    at_startup: bool = False) -> "SweptDownloads":
        """Remove expired tickets and abandoned landing directories."""
        return await self._reclaim(expired_only=True, now=now, at_startup=at_startup)

    async def clear(self, *, expired_only: bool = True,
                    now: float | None = None) -> "SweptDownloads":
        """The owner's override from Settings → Disk space. Never touches a
        download that is still arriving."""
        return await self._reclaim(expired_only=expired_only, now=now)

    async def _reclaim(self, *, expired_only: bool, now: float | None,
                       at_startup: bool = False) -> "SweptDownloads":
        async with self._reclaim_lock:
            swept = await asyncio.to_thread(
                self._sweep, time.time() if now is None else now, expired_only, at_startup,
            )
        if swept.handles or swept.bytes:
            self._revision += 1
        if swept.handles or swept.refused:
            logger.info(
                "swept %d download(s), freeing %d bytes; %d kept, %d refused",
                swept.handles, swept.bytes, swept.kept, swept.refused,
            )
        return swept

    def _sweep(self, now: float, expired_only: bool, at_startup: bool) -> "SweptDownloads":
        live = self.busy()
        if at_startup:
            # Nothing can be downloading before the first request is served, so
            # a reservation here would mean this flag reached the wrong caller.
            assert not live, "at_startup sweeps run before any download can start"
        handles = files = freed = kept = refused = 0
        for entry in reclaim.children(self.root):
            if entry.name in live:
                kept += 1
                continue
            if entry.name.startswith(LANDING_PREFIX):
                # Not in the ledger, so nothing is writing to it on purpose: a
                # crash mid-download, or a stray a click dropped after the
                # browser's download folder last pointed here. Removed — but
                # this walk runs in a thread, and `begin` creates the directory
                # a moment before it records the reservation. So the ledger is
                # re-read here rather than trusted from the snapshot above, and
                # a landing younger than a minute is left for the next sweep.
                # At startup nothing can be in flight, so everything goes.
                if entry.name in self._reserved or (
                        not at_startup and not _older_than(entry, now, _LANDING_GRACE_SEC)):
                    kept += 1
                    continue
            else:
                manifest = None if entry.is_symlink() else _read_manifest(entry)
                if manifest is not None and expired_only and not _expired(manifest, now):
                    kept += 1
                    continue
                if manifest is None and expired_only and not _older_than(entry, now, TTL_SEC):
                    # Caught mid-commit, or damaged: nothing resolves out of it,
                    # and waiting out a TTL is what keeps a sweep from racing a
                    # commit that is creating it.
                    kept += 1
                    continue
            size, count = (0, 0) if entry.is_symlink() else measure_dir(entry)
            try:
                gone = reclaim.remove_child(self.root, entry)
            except reclaim.Unsafe as exc:
                logger.warning("left the download entry %s alone: %s", entry.name, exc)
                refused += 1
                continue
            if not gone:
                logger.warning("could not fully remove the download entry %s", entry.name)
                refused += 1
                continue
            if not entry.name.startswith(LANDING_PREFIX):
                handles += 1
                files += count
            freed += size
        return SweptDownloads(handles=handles, files=files, bytes=freed, kept=kept,
                              refused=refused)


def _older_than(entry: Path, now: float, seconds: float) -> bool:
    try:
        return (now - entry.lstat().st_mtime) > seconds
    except OSError:  # pragma: no cover - it vanished under us
        return False


@dataclass(frozen=True)
class SweptDownloads:
    """What a sweep actually removed — never what it set out to remove."""

    handles: int
    files: int
    bytes: int
    kept: int
    refused: int
