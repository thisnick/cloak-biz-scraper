"""Bytes an agent hands us, staged at a path this service is willing to vouch for.

MCP tool arguments are JSON, so a photo cannot travel in a tool call — the agent
POSTs it over plain HTTP instead and gets back a container-side path. That path
is later fed to `agent-browser upload`, which is `setInputFiles`, which reads
whatever it is pointed at. Two directories above this one sit `settings.json`
and the `.dek` that decrypts it, so the interesting question is never "can we
write the bytes" but **"can we still prove, later, that we wrote them"**.

That proof is `.ticket.json`. Every staged file is recorded there with the name
we chose for it, and `resolve_for` will hand back nothing that is not in that
record. A path the caller invented — `/data/.dek`, `/etc/passwd`, a name we
never wrote — has no entry, so there is nothing to return. And what is returned
is the path built from the *manifest*, never the caller's string reused after a
boolean check: the same discipline `reclaim.removable_child` states outright
("callers remove the Path returned here, never the one they passed in").

**One directory per ticket, directly under the uploads root.** That is what
makes every entry a direct child, which is exactly the shape
`reclaim.removable_child` requires, so expiry can reuse the containment rule the
rest of the volume already deletes through instead of growing a second one.

**On the volume, not /tmp.** services/heartbeat.py documents that Railway sleeps
after ten minutes without outbound traffic, and the container can nap between
the upload and the browser call — the model thinks, the user wanders off. A
staged file that evaporates between two tool calls is a miserable bug to
diagnose.

**The token is its own kind.** `signing.py`'s thesis is that a valid signature
proves a token came from us and proves nothing about what it is *for*; the
audience is the type system. A ticket is `upload:<handle>`, so a CDP token, a
VNC token, a session cookie and a ticket for a *different* handle all fail to
verify here, and a ticket grants exactly "add bytes to this one staging slot".

**Content is decided by the first bytes, not by what the caller called it.** An
extension is a claim and `Content-Type` is a claim; the magic number is
evidence. That is what makes "upload my .env named photo.jpg" a 415 rather than
a file sitting on the volume waiting to be posted to a listing site.

Nothing here is released by hand. There is no release call, because "the client
must remember to clean up" is a contract clients do not keep — a conversation
ends, a model moves on, a Railway nap lands mid-task. Files expire on a clock
instead: `resolve_for` refuses a ticket past its TTL long before `sweep` takes
the bytes away, so the expiry a caller actually experiences is a refusal that
says to upload again, not a mystery 404.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from . import reclaim, signing
# Re-exported deliberately: routes/uploads.py builds its own refusals from the
# same numbers, and one formatter for the whole feature is the point.
from .presentation import human_size
from .profile_sizes import measure_dir

logger = logging.getLogger("cloakbiz.uploads")

# ── The caps, as the four numbers a model is told and the server enforces ────
#
# Per-file is generous on purpose: full-resolution camera output and scanned
# PDFs both live near 100 MB, and a user should not have to think about it. The
# per-ticket total is the one that actually binds — 12 x 100 MB is 1.2 GB, which
# no Railway volume should absorb from a single call. Both are enforced WHILE
# the bytes stream, never after the body has landed.
MAX_BYTES_PER_FILE = 100 * 1024 * 1024
MAX_FILES_PER_TICKET = 12
MAX_BYTES_PER_TICKET = 250 * 1024 * 1024

# The global ceiling. These are the first genuinely large files on a volume that
# also holds profiles, evidence, settings.json and the .dek, so filling it does
# not degrade an upload — it makes profile writes start failing.
#
# **The invariant: the bytes under the uploads root can never exceed this, under
# any amount of concurrency.** That is a stronger promise than it looks, and the
# first version of this module did not keep it. Measuring the volume and then
# granting is not a bound: eight writers that all measure before any of them
# commits will all be admitted, and the measured peak was 8.00x the per-ticket
# cap. So admission is a RESERVATION, taken under the same lock that later
# commits — see `_reserved` and `begin`. A reservation cannot be raced, because
# the deciding read and the write of the ledger happen without an await between
# them.
UPLOADS_BUDGET_BYTES = 1024 * 1024 * 1024

# Bookkeeping is bytes too, and an invariant with an unaccounted term is not an
# invariant. A ticket's manifest is reserved once at mint (headroom for the
# whole ticket's life) and each staged file reserves room for the entry it will
# add. Both are generous: an entry is ~150 bytes of JSON and twelve of them plus
# the wrapper fit inside the headroom several times over.
_MANIFEST_SEED_BYTES = 256
_MANIFEST_ENTRY_BYTES = 512

# Two hours: long enough that the same photos can be posted to several sites in
# one session, short enough that a forgotten ticket is a rounding error on the
# volume. The signed token and the manifest carry the SAME expiry, so a live
# token can never name a dead ticket or the reverse.
TTL_SEC = 2 * 60 * 60

HANDLE_PREFIX = "upl_"
_HANDLE_RE = re.compile(r"^upl_[0-9a-f]{16}$")

# The audience half of `upload:<handle>`. See tokens.py: cdp/vnc are separate
# audiences for the same reason.
AUD = "upload"

MANIFEST_NAME = ".ticket.json"
_INCOMING_PREFIX = ".incoming-"

# What the client is told it may send, in the order a model reads them.
ACCEPTS = ("image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf")

# WebP needs twelve bytes to identify (RIFF....WEBP); everything else needs
# fewer, so twelve is the whole sniff window and nothing is written before it
# has been read.
_SNIFF_BYTES = 12
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

_EXTENSION = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "application/pdf": ".pdf",
}

# A filename is caller-controlled text that becomes a path component, so it is
# an allow-list, not a blocklist. Everything outside it collapses to "_".
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME_LEN = 100


class UploadsError(Exception):
    """Something about this upload is refused. Every subclass says what to do."""


class NotStaged(UploadsError):
    """This path is not a file we wrote down. The refusal the whole module exists for."""


class Expired(UploadsError):
    """A real staged file, past its TTL. Deliberately NOT the same error as
    NotStaged: this one is the status check, arriving at the only moment it
    matters, and "upload it again" is a different instruction from "you made
    that path up"."""


class UnsupportedType(UploadsError):
    """The bytes are not one of the accepted image/PDF formats."""


class TooLarge(UploadsError):
    """A per-file or per-ticket byte cap tripped mid-stream."""


class TooMany(UploadsError):
    """This ticket already holds MAX_FILES_PER_TICKET files."""


class NoRoom(UploadsError):
    """The uploads folder is at its global budget; minting is refused."""


class NoPublicUrl(UploadsError):
    """This server cannot tell a client what address to POST bytes to.

    The whole ticket is an instruction to go somewhere, so a ticket without a
    usable address is not a degraded answer — it is a wrong one. Reachable
    because the address comes from the `Host` header and nothing upstream
    guarantees one: a request with no Host, or one carrying something that is
    not a hostname at all, would otherwise produce `https:///uploads/...` or
    worse, a URL that ends up inside a shell command we tell a model to run.
    """


@dataclass(frozen=True)
class StagedFile:
    """One file on the volume, as the caller is told about it.

    `path` is absolute and container-side because that is what
    `agent-browser upload` takes, and a path reads to a model far better than an
    opaque handle would. It is safe to hand out only because `resolve_for` will
    not accept it back without finding it in a manifest first.
    """

    path: str
    name: str
    bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class Ticket:
    """A freshly minted staging slot: where to POST, and the bearer that opens it."""

    handle: str
    token: str
    expires_at: float


@dataclass(frozen=True)
class UploadsView:
    """What staged uploads are costing, and when that was measured."""

    handles: int
    files: int
    bytes: int
    expired: int  # tickets past their TTL that no sweep has reached yet
    measured_at: float


@dataclass(frozen=True)
class SweptUploads:
    """What a sweep actually removed — never what it set out to remove."""

    handles: int
    files: int
    bytes: int
    kept: int      # tickets still live, left exactly as they were
    refused: int   # entries reclaim.Unsafe would not vouch for


# ── Tokens ───────────────────────────────────────────────────────────────────


def audience(handle: str) -> str:
    return f"{AUD}:{handle}"


def issue_ticket(handle: str, secret: str, *, subject: str,
                 ttl_sec: int = TTL_SEC, now: float | None = None) -> str:
    """The bearer for one staging slot and one subject. Minted per ticket."""
    return signing.issue(
        {"aud": audience(handle), "sub": subject}, secret, ttl_sec=ttl_sec, now=now
    )


def ticket_subject(token: str | None, handle: str, secret: str | None, *,
                   now: float | None = None) -> str | None:
    """The subject a live ticket for *this* handle was minted for, else None.

    The upload route sits outside the OAuth gate, so it has no other identity to
    work from: the subject comes out of the signed bytes, and the store then
    matches it against the one recorded in the ticket's manifest. Only APP_SECRET
    can produce those bytes, so a caller cannot name a subject of their choosing.

    The audience check inside is what stops a ticket for one slot writing into
    another, and what stops every other bearer this app mints — CDP, VNC, the
    session cookie, an OAuth access or refresh token — from writing at all.
    """
    if not handle or not _HANDLE_RE.match(handle):
        return None
    claims = signing.verify(token, secret, audience=audience(handle), now=now)
    if claims is None:
        return None
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None


def verify_ticket(token: str | None, handle: str, secret: str | None, *,
                  subject: str | None, now: float | None = None) -> bool:
    """True only for a live ticket minted for this handle AND this subject.

    `subject=None` means "any subject", matching tokens.verify. Nothing in the
    upload path passes None — the route reads the subject out of the token with
    `ticket_subject` and the manifest is what it is checked against.
    """
    actual = ticket_subject(token, handle, secret, now=now)
    if actual is None:
        return False
    return subject is None or actual == subject


# ── Names, magic bytes, manifests ────────────────────────────────────────────


def sniff(head: bytes) -> str | None:
    """The content type these first bytes actually are, or None.

    Not the extension and not the declared Content-Type — both are things the
    caller said. This is the check that makes "upload my .env, call it
    photo.jpg" fail, whatever it was named and whatever header rode with it.
    """
    for magic, content_type in _MAGIC:
        if head.startswith(magic):
            return content_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _require_type(head: bytes) -> str:
    content_type = sniff(head)
    if content_type is None:
        raise UnsupportedType(
            "that file is not an image or a PDF — this endpoint accepts "
            + ", ".join(ACCEPTS)
            + ", checked by content rather than by filename"
        )
    return content_type


def _split_ext(name: str) -> tuple[str, str]:
    """`("photo", ".jpg")`, or the whole name and no extension.

    A non-alphanumeric or very long tail is not an extension. An empty stem IS
    one — `.jpg` is a name with nothing but an extension, and `safe_name` gives
    it a stem rather than dropping the half that carries meaning. That case is
    reachable from ordinary input: `a;rm -rf /.jpg` has the basename `.jpg`, and
    it used to arrive called `jpg`, with no extension at all, on a feature whose
    whole job is posting files to forms that check them.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not ext.isalnum() or len(ext) > 8:
        return name, ""
    return stem, f".{ext}"


def safe_name(raw: str, content_type: str) -> str:
    """A caller-supplied filename reduced to one safe path component.

    Basename only (both separators, because a Windows client sends
    `C:\\photos\\a.jpg`), an allow-list of characters, and a length cap that
    keeps the extension. Leading dots are stripped, which is not cosmetic: it is
    what guarantees a staged file can never be named `.ticket.json` or shadow an
    in-flight `.incoming-*` temp file, so the manifest cannot be overwritten by
    something it is supposed to be describing.

    **The extension is separated BEFORE the stem is cleaned up**, and that
    ordering is the whole of a bug worth remembering. Doing it the other way
    round, `写真.jpg` collapses to `_.jpg`, whose leading `_` and `.` are then
    stripped as junk, and the file arrives called `jpg` with no extension at
    all. Harmless for containment — nothing escapes either way — but this
    feature exists to post photos to somebody else's upload form, and half of
    those validate the extension. A stem that survives nothing is replaced;
    an extension that was really there is kept.
    """
    basename = re.split(r"[\\/]", raw or "")[-1]
    stem, ext = _split_ext(_UNSAFE_CHARS.sub("_", basename))
    stem = stem.strip("._-")
    if not stem:
        stem = "upload"
        # Nothing usable was given at all, so name it after what it turned out
        # to be. The type is the sniffed one, never the caller's claim.
        ext = ext or _EXTENSION.get(content_type, "")
    return f"{stem[: max(1, MAX_NAME_LEN - len(ext))]}{ext}"


def _unique_name(name: str, taken: Iterable[str]) -> str:
    """`photo.jpg`, `photo-1.jpg`, `photo-2.jpg` — two different files that
    arrived under one name are two files, not one overwritten one."""
    taken = set(taken)
    if name not in taken:
        return name
    stem, ext = _split_ext(name)
    stem = stem[: max(1, MAX_NAME_LEN - len(ext) - 5)]
    for n in range(1, 1000):
        candidate = f"{stem}-{n}{ext}"
        if candidate not in taken:
            return candidate
    return f"{stem}-{secrets.token_hex(4)}{ext}"  # pragma: no cover - 999 collisions


def _read_manifest(ticket_dir: Path) -> dict | None:
    """The ticket's record, or None for anything we cannot read as one.

    Fail closed and never raise: a missing, truncated or hand-edited manifest
    means nothing in that directory resolves, which is the safe reading. A
    half-written one cannot happen — see _write_manifest — but a volume can
    still hand back garbage, and the answer to garbage is "not staged".
    """
    try:
        raw = (ticket_dir / MANIFEST_NAME).read_bytes()
    except OSError:
        return None
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        logger.warning("unreadable manifest in %s; treating it as empty", ticket_dir.name)
        return None
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("sub"), str)
        or not isinstance(manifest.get("expires"), (int, float))
        or not isinstance(manifest.get("files"), list)
    ):
        logger.warning("malformed manifest in %s; treating it as empty", ticket_dir.name)
        return None
    return manifest


def _write_manifest(ticket_dir: Path, manifest: dict) -> None:
    """Replace the manifest atomically.

    Temp file plus os.replace, because the crash window matters here: the
    manifest IS the allow-list, and one truncated by a container that died
    mid-write would make every file in the ticket unresolvable — the exact
    "staged file evaporated between two tool calls" bug this store exists to
    avoid. os.replace is atomic within a directory, so a reader sees the old
    record or the new one and never a partial one.
    """
    tmp = ticket_dir / f"{MANIFEST_NAME}.tmp-{secrets.token_hex(4)}"
    tmp.write_text(json.dumps(manifest, separators=(",", ":"), sort_keys=True))
    os.replace(tmp, ticket_dir / MANIFEST_NAME)


def _volume_bytes(root: Path) -> int:
    """Bytes under the uploads root, EXCLUDING in-flight temp files.

    The exclusion is what makes the budget arithmetic exact rather than merely
    conservative. A partially-written `.incoming-*` file is already covered by
    its writer's reservation; counting it as well would charge those bytes
    twice, and the effective budget would shrink and grow with traffic.

    **That reasoning holds only while the writer is alive.** After `kill -9`, an
    OOM, or a Railway redeploy mid-upload, the in-memory ledger dies and the
    file does not — and this walk then genuinely cannot see bytes that are
    genuinely there, which breaks the invariant rather than merely relaxing it:
    measured at 1.20x budget with three such orphans. Nothing sweeps them
    either, because they live inside tickets that are not expired yet. So the
    startup sweep deletes every `.incoming-*` it finds: nothing can be in flight
    at boot, so anything wearing that name is by definition abandoned.

    Never raises, for the same reason `measure_dir` does not: a volume that
    cannot be walked is a reason to refuse an upload, not to crash one.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, onerror=None, followlinks=False):
        for filename in filenames:
            if filename.startswith(_INCOMING_PREFIX):
                continue
            try:
                total += os.stat(
                    os.path.join(dirpath, filename), follow_symlinks=False
                ).st_size
            except OSError:  # it vanished mid-walk
                continue
    return total


def _expired(manifest: dict, now: float) -> bool:
    return float(manifest.get("expires") or 0) <= now


def _record(ticket_dir: Path, entry: dict) -> StagedFile:
    return StagedFile(
        path=str(ticket_dir / entry["name"]),
        name=entry["name"],
        bytes=int(entry.get("bytes", 0)),
        sha256=str(entry.get("sha256", "")),
        content_type=str(entry.get("content_type", "")),
    )


_NOT_STAGED = (
    "{path!r} is not an uploaded file — call create_upload_url first, POST the file to "
    "the URL it hands back, and use the path from that response"
)
_EXPIRED = (
    "{path!r} has expired — staged files last two hours. Call create_upload_url and "
    "upload it again."
)


# ── The store ────────────────────────────────────────────────────────────────


class StagedWrite:
    """One file being written into one ticket, checked byte by byte.

    Deliberately a push interface rather than "hand me a stream": the multipart
    parser is callback-driven, and inverting it into an async iterator would
    mean buffering a whole part somewhere first — which is the one thing this
    class exists to avoid. `stage()` wraps it for callers that do have a stream.

    **A refusal cleans up after itself.** Every path out of feed/commit that
    raises unlinks the partial file before the exception leaves, so a tripped
    cap cannot leave 250 MB on the volume for a sweep to find in two hours. That
    is here rather than in the caller because a caller can forget.
    """

    def __init__(self, service: "UploadService", ticket_dir: Path, *,
                 filename: str, allowance: int, refusal: UploadsError) -> None:
        self._service = service
        self._ticket_dir = ticket_dir
        self._filename = filename
        # What `begin` reserved for these bytes, and therefore the most this
        # write may put on disk — the smallest of the per-file cap, what the
        # caller declared, the room left in the ticket, and the room left on the
        # volume. `refusal` is the message belonging to whichever of those it
        # was, prepared at admission so the streaming path does not have to
        # re-derive which limit it just hit.
        self._allowance = allowance
        self._refusal = refusal
        self._tmp = ticket_dir / f"{_INCOMING_PREFIX}{secrets.token_hex(8)}"
        self.reservation = self._tmp.name
        self._out = open(self._tmp, "wb")
        self._digest = sha256()
        self._written = 0
        self._head = b""
        self._closed = False
        self.content_type: str | None = None

    # ── writing ──
    def feed(self, data: bytes) -> None:
        """Take the next slice of the part. Raises the moment a rule is broken."""
        if not data:
            return
        try:
            if self.content_type is None:
                self._head += data
                if len(self._head) < _SNIFF_BYTES:
                    return  # still nothing on disk: we have not decided what this is
                data, self._head = self._head, b""
                self.content_type = _require_type(data)
            self._admit(data)
        except UploadsError:
            self.abort()
            raise

    def _admit(self, data: bytes) -> None:
        """One slice onto disk, or the refusal that stops it going there.

        A single comparison, because `begin` already worked out which limit
        binds. That matters for more than tidiness: every byte of every upload
        passes through here, and the number it is checked against is the number
        the volume was reserved for — so what lands can never exceed what was
        promised, whatever the client said or did.
        """
        self._written += len(data)
        if self._written > self._allowance:
            raise self._refusal
        self._digest.update(data)
        self._out.write(data)

    async def commit(self) -> StagedFile:
        """Name the file, record it in the manifest, and hand back its path.

        Keyed by sha256: the same bytes staged twice return the file that is
        already there, at the same path, which makes a retried curl idempotent
        for free rather than leaving two copies of one photo on the volume.

        The caps are re-checked HERE against a freshly read manifest, not
        against the count this write started with — two POSTs to one ticket can
        overlap, and the check that decides is the one holding the lock.
        """
        try:
            if self.content_type is None:
                # The whole part was shorter than the sniff window. A six-byte
                # GIF header is a legitimate (if useless) file; an empty part is
                # not, and `sniff(b"")` says so.
                self.content_type = _require_type(self._head)
                self._admit(self._head)
                self._head = b""
            self._out.close()
            checksum = self._digest.hexdigest()

            async with self._service._lock:
                manifest = _read_manifest(self._ticket_dir)
                if manifest is None:
                    raise NotStaged("that upload URL is no longer valid")
                if _expired(manifest, time.time()):
                    raise Expired(
                        "that upload URL has expired — call create_upload_url for a new one"
                    )
                entries = list(manifest.get("files") or [])
                for entry in entries:
                    if entry.get("sha256") == checksum:
                        self.abort()
                        return _record(self._ticket_dir, entry)
                if len(entries) >= MAX_FILES_PER_TICKET:
                    raise TooMany(
                        f"this upload URL already holds {MAX_FILES_PER_TICKET} files, which "
                        "is its limit. Call create_upload_url for another."
                    )
                total = sum(int(e.get("bytes", 0)) for e in entries)
                if total + self._written > MAX_BYTES_PER_TICKET:
                    raise TooLarge(
                        f"this upload URL can hold {human_size(MAX_BYTES_PER_TICKET)} in total "
                        "and that file would take it over. Get a fresh upload URL for "
                        "the rest."
                    )
                name = _unique_name(
                    safe_name(self._filename, self.content_type),
                    {str(e.get("name")) for e in entries},
                )
                try:
                    os.replace(self._tmp, self._ticket_dir / name)
                except OSError as exc:
                    # The ticket was swept or cleared while these bytes were
                    # arriving. `busy_handles` is what normally prevents it;
                    # this is what stops the race that remains from being a 500.
                    raise NotStaged(
                        "that upload URL was cleared while the file was arriving — "
                        "call create_upload_url for a new one"
                    ) from exc
                self._closed = True
                entry = {
                    "name": name, "bytes": self._written, "sha256": checksum,
                    "content_type": self.content_type,
                }
                manifest["files"] = entries + [entry]
                _write_manifest(self._ticket_dir, manifest)
                # Released inside the lock, in the same breath as the bytes
                # becoming committed: for one instant they are counted twice,
                # never zero times, which is the direction an invariant may err.
                self._service._release(self.reservation)
                self._service._revision += 1
        except UploadsError:
            self.abort()
            raise
        logger.info(
            "staged %s (%d bytes, %s) in %s",
            name, self._written, self.content_type, self._ticket_dir.name,
        )
        return _record(self._ticket_dir, entry)

    def abort(self) -> None:
        """Close and remove the partial file, and give the reservation back.

        Idempotent, and safe after commit. Releasing here is what stops a
        refused or abandoned upload from holding volume budget for the rest of
        the process's life — an upload that fails must cost nothing, or a run of
        failures becomes its own outage.
        """
        self._service._release(self.reservation)
        if not self._closed:
            self._closed = True
            try:
                self._out.close()
            except OSError:  # pragma: no cover - defensive
                pass
            self._tmp.unlink(missing_ok=True)


class UploadService:
    """The staging store: mint a ticket, stream bytes into it, resolve a path back.

    Holds the root and nothing else. The signing secret is passed in per call
    rather than captured, exactly as services/tokens.py takes it: APP_SECRET is
    read from the environment on every use so a rotation takes effect on the
    redeploy that restarts the process, and a captured copy would quietly
    outlive it.

    The root is created lazily, on the first mint. A volume that cannot be
    written to must not be discovered at construction time, where it would take
    down boot for a feature nobody has used yet.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # Guards the three read-decide-write sequences that must not interleave:
        # minting a ticket, admitting a write, and committing one. All three
        # read the volume and then change it, and a check that is not holding
        # this lock is a check eight callers can pass at once.
        self._lock = asyncio.Lock()
        # The admission ledger: reservation id -> (handle, file bytes, overhead).
        # Bytes that are SPOKEN FOR but not yet on disk. The two numbers are kept
        # apart because they answer different questions — the per-ticket cap is
        # about the caller's files, the volume budget is about everything. A
        # plain dict, mutated without awaiting, which is atomic under one event
        # loop; the lock is what makes the surrounding decide-then-reserve atomic.
        self._reserved: dict[str, tuple[str, int, int]] = {}
        # Bumped whenever the volume changes. `StagedUploads` caches a
        # measurement against it, so a cached number cannot outlive the bytes it
        # describes. A counter rather than a back-reference to the measurement:
        # the store must not need to know who is watching, and a sweep triggered
        # by a mint has no idea a settings page exists.
        self._revision = 0
        # Serialises reclaims. Two clears running at once each walked the volume,
        # each removed part of it, and each reported the whole amount freed —
        # which is exactly the promise `SweptUploads` is documented not to make.
        self._reclaim_lock = asyncio.Lock()

    @property
    def revision(self) -> int:
        """How many times the bytes under this root have changed.

        Cheap, monotonic, and deliberately coarse: a reader that saw revision N
        knows nothing has moved only while it still reads N. Anything finer
        would be a cache-invalidation protocol, and this needs a "is my number
        stale" answer, not a diff.
        """
        return self._revision

    # ── the admission ledger ──
    def _reserved_total(self) -> int:
        """Every promised byte, bookkeeping included — the volume's question."""
        return sum(size + overhead for _h, size, overhead in self._reserved.values())

    def _reserved_for(self, handle: str) -> int:
        """Promised FILE bytes for one ticket — the per-ticket cap's question."""
        return sum(size for owner, size, _o in self._reserved.values() if owner == handle)

    def _release(self, reservation: str) -> None:
        """Give bytes back. Idempotent: commit and abort may both reach here."""
        self._reserved.pop(reservation, None)

    def _in_flight_note(self, handle: str) -> str:
        """Why a nearly-empty volume can still refuse, and what to do about it.

        An upload that sends no Content-Length has to reserve the per-file
        maximum, because a bound that only holds for clients which declare their
        size is not a bound. The cost is that two of those can use up a ticket's
        room while holding almost nothing — and "no room" on a volume that is
        visibly empty is a baffling thing to be told. So when what is in the way
        is bytes PROMISED rather than bytes written, the refusal says so and
        says what to do instead.
        """
        mine = sum(1 for owner, _size, _o in self._reserved.values() if owner == handle)
        others = len(self._reserved) - mine
        if not mine and not others:
            return ""
        # Count and attribution have to agree. The first version of this reported
        # the GLOBAL total under a LOCAL description — one write here and two
        # elsewhere came out as "3 other uploads to this upload URL" — and the
        # difference is not cosmetic: a model reads this and chooses between
        # waiting and fetching a fresh URL.
        parts = []
        if mine:
            parts.append(f"{mine} other upload{'' if mine == 1 else 's'} to this "
                         "upload URL")
        if others:
            parts.append(f"{others} upload{'' if others == 1 else 's'} elsewhere on "
                         "this server")
        plural = (mine + others) > 1
        return (
            f" {' and '.join(parts)} {'are' if plural else 'is'} still in flight, "
            f"holding room until {'they finish' if plural else 'it finishes'}; an "
            "upload that sends no Content-Length has to reserve the maximum. Retry in "
            "a moment, or send a Content-Length so yours reserves only what it needs."
        )

    def busy_handles(self) -> set[str]:
        """Tickets with a write in flight right now.

        The owner's clear reads this and leaves them alone, the same way
        `clear_history` keeps a run that is still working. Without it the clear
        would delete a directory out from under an open file handle: on POSIX
        the writer keeps writing to an unlinked inode and only discovers it at
        the rename, which is a 500 for something that is nobody's mistake.

        In memory rather than on disk, and that is a real limitation worth
        stating: a restart forgets every reservation. It is safe in the
        direction that matters — a forgotten reservation frees budget it was
        holding, and the writer it belonged to died with the process — but it
        means this is not a lock, only a courtesy between callers in one
        process, which is all there is on a single-container deployment.
        """
        return {handle for handle, _size, _o in self._reserved.values()}

    def _volume_committed_and_reserved(self) -> int:
        """Everything the budget has to cover: bytes on disk plus bytes promised.

        The walk is done fresh rather than cached. A counter would have to be
        kept in step with the sweep, with the owner's clear, and with anything
        else that ever touches this volume — and a counter that drifts high
        refuses uploads nobody can explain, while one that drifts low is a
        broken invariant. The volume is capped at a gigabyte across a few dozen
        files, so the walk is cheap, and paying for it is how this stays true by
        construction rather than by everyone remembering.
        """
        return _volume_bytes(self.root) + self._reserved_total()

    # ── minting ──
    async def mint(self, *, subject: str, secret: str | None,
                   now: float | None = None) -> Ticket:
        """A fresh ticket: its own directory, its own manifest, its own bearer.

        Sweeps first, then checks the global budget, and the order is the point:
        expired tickets are the space a new one is entitled to reuse, and with
        no manual release the sweep is the only thing that frees any. Doing both
        HERE rather than in the caller is what stops a future second door onto
        this store from forgetting one of them.
        """
        if not secret:
            raise UploadsError(
                "this server has no APP_SECRET set, so it cannot mint an upload URL"
            )
        # Outside the lock: a sweep only removes tickets that are already
        # expired, and an expired ticket is one `begin` refuses anyway, so it can
        # never take a directory a live writer is holding.
        await self.sweep(now=now)
        now = time.time() if now is None else now
        handle = f"{HANDLE_PREFIX}{secrets.token_hex(8)}"
        ticket_dir = self.root / handle
        async with self._lock:
            # Nothing in here awaits, and that is load-bearing rather than
            # incidental: without an await, measure-then-create is already
            # atomic under one event loop, and the lock is what keeps it that
            # way the day somebody makes the walk `await asyncio.to_thread(...)`
            # for latency. Verified by mutation — removing the lock alone
            # changes nothing; removing it AND yielding here hands the same last
            # free slot to twenty-five callers at once.
            #
            # A ticket confers no bytes on its own — `begin` is what admits
            # those — so this check is the FRIENDLY one: it stops a caller
            # collecting URLs that will refuse every file, and it names the one
            # place a person can free space. What it strictly bounds is the
            # ticket's own bookkeeping, which is why it reserves manifest
            # headroom and why it is inside the lock: twenty-five concurrent
            # mints must not each write a manifest against one measurement.
            used = self._volume_committed_and_reserved()
            if used + _MANIFEST_SEED_BYTES > UPLOADS_BUDGET_BYTES:
                raise NoRoom(
                    f"uploaded files are already using {human_size(used)}, which is the "
                    f"{human_size(UPLOADS_BUDGET_BYTES)} this server keeps for them. "
                    "They expire on their own within two hours; to free the space now, open "
                    "Settings \u2192 Disk space and clear uploaded files."
                )
            ticket_dir.mkdir(parents=True)
            _write_manifest(ticket_dir, {
                "sub": subject, "created": now, "expires": now + TTL_SEC, "files": [],
            })
        self._revision += 1
        logger.info("minted upload ticket %s for %s", handle, subject)
        return Ticket(
            handle=handle,
            token=issue_ticket(handle, secret, subject=subject, now=now),
            expires_at=now + TTL_SEC,
        )

    # ── writing ──
    async def begin(self, handle: str, *, subject: str, filename: str,
                    declared_bytes: int | None = None,
                    now: float | None = None) -> StagedWrite:
        """Admit one write into a live, subject-owned ticket, or refuse it.

        **This is where the volume is bounded.** Not by measuring and then
        granting — eight writers can all measure before any of them commits, and
        they did: the measured peak was 8.00x the per-ticket cap. Admission
        takes a RESERVATION for the bytes this write may put on disk, under the
        lock that later commits, so what the next caller measures already
        includes what this one is about to write.

        `declared_bytes` is the caller's Content-Length when it has one — curl
        always sends it, so the reservation is tight in the case that matters.
        Without one (a chunked body) the reservation is the pessimistic
        `MAX_BYTES_PER_FILE`, which is what makes a bound hold for a client that
        did not say. It is a ceiling as well as a promise: a body longer than
        what it declared is refused at the declared number, or the lie would buy
        back exactly the slack the reservation removed.

        Everything else checkable before a byte arrives is still checked before
        a byte arrives — an unknown handle, a ticket that is gone, one belonging
        to somebody else, one past its TTL, one already full.
        """
        now = time.time() if now is None else now
        ticket_dir = self._ticket_dir(handle)

        async with self._lock:
            manifest = self._live_manifest(ticket_dir, subject, now)
            entries = manifest.get("files") or []
            if len(entries) >= MAX_FILES_PER_TICKET:
                raise TooMany(
                    f"this upload URL already holds {MAX_FILES_PER_TICKET} files, which is "
                    "its limit. Call create_upload_url for another."
                )
            committed = sum(int(e.get("bytes", 0)) for e in entries)

            # Four limits, each with the sentence that belongs to it. Both room
            # figures are computed against committed PLUS promised; against
            # committed alone — which is what this used to do — eight callers
            # all pass before any of them commits.
            room_in_ticket = MAX_BYTES_PER_TICKET - committed - self._reserved_for(handle)
            room_on_volume = (
                UPLOADS_BUDGET_BYTES
                - self._volume_committed_and_reserved()
                - _MANIFEST_ENTRY_BYTES
            )
            # Both room figures can be short because of bytes that are promised
            # rather than written, and a refusal that does not say so is a
            # mystery. The note is empty when nothing is in flight, so the
            # ordinary "you sent too much" case reads exactly as before.
            in_flight = self._in_flight_note(handle)
            limits: list[tuple[int, UploadsError]] = [
                (MAX_BYTES_PER_FILE, TooLarge(
                    f"that file is over the {human_size(MAX_BYTES_PER_FILE)} limit for a "
                    "single upload")),
                (room_in_ticket, TooLarge(
                    f"this upload URL can hold {human_size(MAX_BYTES_PER_TICKET)} in total "
                    "and that file would take it over. Get a fresh upload URL for the "
                    f"rest.{in_flight}")),
                (room_on_volume, NoRoom(
                    "there is no room on this server for another upload right now — "
                    f"uploaded files may use {human_size(UPLOADS_BUDGET_BYTES)} in total. "
                    "They expire on their own within two hours; to free the space now, open "
                    f"Settings \u2192 Disk space and clear uploaded files.{in_flight}")),
            ]
            if declared_bytes is not None:
                limits.append((max(0, declared_bytes), TooLarge(
                    "that upload sent more data than its Content-Length declared; send "
                    "the length you mean to send")))
            allowance, refusal = min(limits, key=lambda pair: pair[0])
            # Nothing at all fits: refuse now rather than open a file and a
            # reservation for a write that cannot take a single byte.
            if allowance <= 0:
                raise refusal

            write = StagedWrite(self, ticket_dir, filename=filename,
                                allowance=allowance, refusal=refusal)
            self._reserved[write.reservation] = (
                handle, allowance, _MANIFEST_ENTRY_BYTES
            )
        return write

    async def stage(self, handle: str, *, subject: str, filename: str,
                    stream: AsyncIterator[bytes], now: float | None = None) -> StagedFile:
        """`begin` + feed a whole stream + `commit`, for callers that have one.

        The HTTP route does not use this — its parser pushes — but a stream is
        the natural shape for a test and for the deferred server-side fetch, and
        both must go through exactly the same caps and sniffing as the route.
        """
        write = await self.begin(handle, subject=subject, filename=filename, now=now)
        try:
            async for chunk in stream:
                write.feed(chunk)
            return await write.commit()
        finally:
            write.abort()

    # ── the security function ──
    def resolve_for(self, subject: str, paths: Iterable[str],
                    *, now: float | None = None) -> list[Path]:
        """The real files behind caller-supplied paths, or raise. **Both gates.**

        1. **Manifest allow-list.** The path must name a file recorded in a
           ticket that is live and owned by this subject. Not "a path that looks
           like ours" — a path we wrote down.
        2. **Containment.** The caller's path and the path built from the
           manifest must both resolve inside that ticket's own directory.
           `resolve()` follows a symlink, so a link planted in a ticket dir and
           pointing at `/data/.dek` lands outside the root and is refused.

        Gate 2 is redundant given gate 1 and stays anyway — the same
        belt-and-braces shape as parse_command's verb allow-list plus its
        per-verb flag whitelist.

        What comes back is built from the manifest, so a caller that hands these
        to a subprocess is never handing over its own string. The two refusals
        read differently on purpose: "not an uploaded file" and "expired" call
        for different fixes, and the second is the only status check this
        feature has.
        """
        now = time.time() if now is None else now
        return [self._resolve_one(str(raw), subject, now) for raw in paths]

    def _resolve_one(self, raw: str, subject: str, now: float) -> Path:
        candidate = Path(raw)
        handle = candidate.parent.name
        if not _HANDLE_RE.match(handle):
            # `/etc/passwd`, `/data/.dek` and `.../upl_x/../../.dek` all land
            # here: the parent is not one of our handles, so there is no
            # manifest to look in and nothing to return.
            raise NotStaged(_NOT_STAGED.format(path=raw))
        ticket_dir = self.root / handle
        manifest = _read_manifest(ticket_dir)
        if manifest is None or manifest.get("sub") != subject:
            # One refusal for "no such ticket" and "somebody else's ticket".
            # Which of the two it was is only useful to a caller who should not
            # have been holding the path.
            raise NotStaged(_NOT_STAGED.format(path=raw))
        if _expired(manifest, now):
            raise Expired(_EXPIRED.format(path=raw))
        entry = next(
            (e for e in manifest.get("files") or [] if e.get("name") == candidate.name),
            None,
        )
        if entry is None:
            raise NotStaged(_NOT_STAGED.format(path=raw))
        try:
            root = ticket_dir.resolve()
            claimed = candidate.resolve()
            target = (ticket_dir / str(entry["name"])).resolve()
        except (OSError, RuntimeError):  # pragma: no cover - defensive
            raise NotStaged(_NOT_STAGED.format(path=raw)) from None
        if not claimed.is_relative_to(root) or not target.is_relative_to(root):
            raise NotStaged(_NOT_STAGED.format(path=raw))
        if not target.is_file():
            raise NotStaged(_NOT_STAGED.format(path=raw))
        return target

    # ── expiry ──
    async def sweep(self, *, now: float | None = None,
                    reclaim_incoming: bool = False) -> SweptUploads:
        """Remove every ticket past its expiry. Returns what it actually freed.

        Deliberately not a timer. services/heartbeat.py documents that Railway
        sleeps on the absence of outbound packets and that a heartbeat running
        unconditionally "would reset the sleep timer forever and quietly bill
        the user 24/7 for an idle service". A periodic cleanup task would do
        exactly that, so this runs only when something already asked: startup,
        a mint, or the settings page.
        """
        return await self._reclaim(expired_only=True, now=now,
                                   reclaim_incoming=reclaim_incoming)

    async def clear(self, *, expired_only: bool = True,
                    now: float | None = None) -> SweptUploads:
        """The owner's override, from Settings \u2192 Disk space.

        `expired_only` is the default because a live ticket may be mid-flight —
        the same reason `clear_history` keeps a run that is still working. The
        full clear is the human's escape hatch for a volume filling up, and it
        still refuses to touch a ticket with a write in flight, because that is
        not a judgement call the button can make from the dashboard.
        """
        return await self._reclaim(expired_only=expired_only, now=now)

    async def _reclaim(self, *, expired_only: bool, now: float | None,
                       reclaim_incoming: bool = False) -> SweptUploads:
        # One reclaim at a time. Without this, two clears walk the same volume,
        # each removes part of it, and each reports the total — two banners
        # claiming to have freed 5 MB when 5 MB existed. `SweptUploads` says in
        # its own docstring that it reports what was actually removed, so this
        # is the stated contract failing rather than a cosmetic overlap.
        async with self._reclaim_lock:
            swept = await asyncio.to_thread(
                self._sweep, time.time() if now is None else now, expired_only,
                reclaim_incoming,
            )
        if swept.handles or swept.bytes:
            self._revision += 1
        if swept.handles or swept.refused:
            logger.info(
                "swept %d expired upload ticket(s), freeing %d bytes across %d file(s); "
                "%d live ticket(s) kept, %d entr(ies) refused",
                swept.handles, swept.bytes, swept.files, swept.kept, swept.refused,
            )
        return swept

    def _sweep(self, now: float, expired_only: bool = True,
               reclaim_incoming: bool = False) -> SweptUploads:
        busy = self.busy_handles()
        handles = files = freed = kept = refused = 0
        if reclaim_incoming:
            freed += self._drop_abandoned_writes()
        for entry in reclaim.children(self.root):
            if entry.name in busy:
                # A write is streaming into this one. Removing it would pull the
                # directory out from under an open file handle.
                kept += 1
                continue
            manifest = None if entry.is_symlink() else _read_manifest(entry)
            if manifest is not None:
                if expired_only and not _expired(manifest, now):
                    kept += 1
                    continue
            elif expired_only and not self._older_than_any_ticket(entry, now):
                # No readable manifest: a directory caught mid-mint, or one
                # whose record was damaged. Nothing can resolve out of it, so it
                # is inert — and leaving it until it is older than any live
                # ticket could be is what stops a sweep racing a mint from
                # deleting the ticket being created.
                kept += 1
                continue
            size, count = (0, 0) if entry.is_symlink() else measure_dir(entry)
            try:
                gone = reclaim.remove_child(self.root, entry)
            except reclaim.Unsafe as exc:
                # Never delete on a guess: an entry the containment rule will
                # not vouch for is left exactly where it is and reported.
                logger.warning("left the upload entry %s alone: %s", entry.name, exc)
                refused += 1
                continue
            if not gone:
                logger.warning("could not fully remove the upload ticket %s", entry.name)
                continue
            handles += 1
            files += count
            freed += size
        return SweptUploads(
            handles=handles, files=files, bytes=freed, kept=kept, refused=refused
        )

    def _drop_abandoned_writes(self) -> int:
        """Remove every `.incoming-*` file. **Startup only.**

        Safe there and nowhere else: nothing can be in flight before the first
        request is served, so anything wearing that name is a write whose
        process died — after a crash, an OOM kill, or a redeploy. Those files
        are the one thing the budget walk cannot see and the sweep would not
        reach, because they live inside tickets that have not expired; without
        this they sit on the volume, uncounted, until their ticket's TTL.

        Returns the bytes it actually removed, like everything else here.
        """
        freed = 0
        for entry in reclaim.children(self.root):
            if entry.is_symlink() or not entry.is_dir():
                continue
            for leftover in reclaim.children(entry):
                if not leftover.name.startswith(_INCOMING_PREFIX):
                    continue
                try:
                    size = leftover.lstat().st_size
                    leftover.unlink()
                except OSError:  # pragma: no cover - it vanished under us
                    continue
                logger.info("removed an abandoned upload of %d bytes from %s",
                            size, entry.name)
                freed += size
        return freed

    @staticmethod
    def _older_than_any_ticket(entry: Path, now: float) -> bool:
        try:
            return (now - entry.lstat().st_mtime) > TTL_SEC
        except OSError:  # pragma: no cover - it vanished under us
            return False

    # ── internals ──
    def _ticket_dir(self, handle: str) -> Path:
        if not _HANDLE_RE.match(handle or ""):
            raise NotStaged("that is not an upload URL this server minted")
        return self.root / handle

    def _live_manifest(self, ticket_dir: Path, subject: str, now: float) -> dict:
        manifest = _read_manifest(ticket_dir)
        if manifest is None or manifest.get("sub") != subject:
            raise NotStaged("that is not an upload URL this server minted")
        if _expired(manifest, now):
            raise Expired(
                "that upload URL has expired — call create_upload_url for a new one"
            )
        return manifest


class StagedUploads:
    """Cached, off-the-event-loop size of what is staged on the volume.

    The same shape as TaskHistory and ProfileSizes, for the same reason: this is
    a walk of the volume and the settings page must never wait on one. The store
    is reached through a callable because it is swapped at runtime and under
    tests, and a captured one would report sizes for a root nobody is writing to.

    `expired` is reported rather than hidden, following TaskHistory's honesty
    about orphans: bytes whose ticket is dead are still bytes on the volume, and
    a number that quietly excluded them would understate the problem it exists
    to explain.
    """

    def __init__(self, uploads: "UploadService | Callable[[], UploadService]") -> None:
        self._get_uploads = uploads if callable(uploads) else lambda: uploads
        self._lock = threading.Lock()
        self._cache: UploadsView | None = None
        self._cached_revision = -1

    def invalidate(self) -> None:
        """Forget the measurement — uploads were swept or cleared, so the number
        the page is showing is now a lie."""
        with self._lock:
            self._cache = None
            self._cached_revision = -1

    async def snapshot(self, *, refresh: bool = False) -> UploadsView:
        """The cached view, re-measured whenever the volume has moved under it.

        Cached on the store's revision rather than on a timer or on nothing:
        uploads are the most volatile of the three things this page reports, and
        a sweep runs on every mint — so a number cached the way the browser
        cache's is would be wrong within one tool call, and stay wrong until
        somebody pressed Clear. `invalidate()` still exists for a caller that
        knows better; this is what covers the callers that do not know at all.
        """
        revision = self._get_uploads().revision
        with self._lock:
            cached, cached_at = self._cache, self._cached_revision
        if cached is not None and not refresh and cached_at == revision:
            return cached
        view = await asyncio.to_thread(self._measure)
        with self._lock:
            self._cache, self._cached_revision = view, revision
        return view

    def _measure(self) -> UploadsView:
        """Runs in a worker thread, never the loop.

        `bytes` is what the volume is actually holding, manifests included —
        that is the number the Disk space row exists to explain. `files` counts
        what was uploaded, from the manifests, because a manifest is our
        bookkeeping and not something the user put there.
        """
        root = self._get_uploads().root
        now = time.time()
        handles = files = total = expired = 0
        for entry in reclaim.children(root):
            if entry.is_symlink():
                # Somebody else's disk. reclaim's rule: identified as a link,
                # never followed.
                continue
            if not entry.is_dir():
                try:
                    total += entry.lstat().st_size
                except OSError:  # pragma: no cover - it vanished mid-walk
                    pass
                continue
            handles += 1
            size, _count = measure_dir(entry)
            total += size
            manifest = _read_manifest(entry)
            if manifest is None or _expired(manifest, now):
                expired += 1
            if manifest is not None:
                files += len(manifest.get("files") or [])
        return UploadsView(
            handles=handles, files=files, bytes=total, expired=expired,
            measured_at=now,
        )
