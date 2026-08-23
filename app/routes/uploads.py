"""`POST /uploads/{handle}` — the one door bytes come in through.

**Why this is outside the OAuth gate.** routes/guard.py protects `/mcp` and
`/api/*`, and a curl running in an agent's sandbox cannot do an OAuth dance —
in ChatGPT and Claude connectors the model never even sees the access token, and
this feature must not be the thing that changes that. So this route is
authenticated by its own short-lived ticket instead, exactly as
`/instances/{id}/cdp` and `/vnc` are. The ticket grants "add bytes to this one
staging slot" and nothing else: its audience is `upload:<handle>`, so it cannot
open a browser, read a sweep, or write into a different slot.

**Header only. There is no `?t=` form here.** services/tokens.py explains why
the CDP and VNC tokens ride in a URL — WebSocket clients frequently cannot set
headers — and says plainly that a URL is a leaky place for a credential. curl
can set a header, so the ticket stays out of URLs, proxy logs and agent
transcripts.

**The body is parsed as it arrives, not after it lands.** Starlette's own form
parsing spools each part into a temp file with no ceiling, so a 2 GB POST would
be written to disk in full and only then measured. This drives the multipart
parser directly instead: bytes go straight into the ticket's directory through
`StagedWrite`, which sniffs the first twelve of them and trips the per-file and
per-ticket caps mid-stream, deleting the partial file on the way out. Nothing
oversized is ever held anywhere.

Verify-Origin-when-present, per routes/mcp.py: curl sends no `Origin` and that
is not the attack, but a page on evil.example driving this endpoint through a
logged-in user's browser is.

**Any multipart part carrying a filename is a file here, whatever its field name
is.** The curl this server hands out says `-F file=@...`, and a stricter reading
would refuse `-F photo=@...` with a message about field names — which is a
confusing failure for a caller who did exactly the right thing in slightly the
wrong words. The field name is never used for anything: the stored name comes
from `filename` through `services.uploads.safe_name`, and the path comes from
the manifest. A part with no filename is a plain form field and is dropped
without being buffered.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from ..models import StagedFile as StagedFileView
from ..models import StagedUpload
from ..services import uploads as uploads_service
from ..services.uploads import StagedFile, StagedWrite
from .mcp import origin_allowed

logger = logging.getLogger("cloakbiz.uploads.http")

router = APIRouter()

# A courtesy refusal before a byte is read, from the length the client declared.
# The real caps are the ones enforced as the bytes arrive; this only stops an
# obviously hopeless POST from spending a minute on the wire first. The slack is
# multipart's own framing overhead.
_BODY_SLACK = 1024 * 1024
_MAX_BODY = uploads_service.MAX_BYTES_PER_TICKET + _BODY_SLACK

# Status codes per refusal. Each says something a client can act on, which is
# the whole reason the service raises distinct exceptions rather than one.
_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (uploads_service.UnsupportedType, 415),
    (uploads_service.TooLarge, 413),
    (uploads_service.TooMany, 409),
    (uploads_service.NoRoom, 507),
    (uploads_service.Expired, 410),
    (uploads_service.NotStaged, 404),
)


def _fields(staged: StagedFile) -> dict[str, Any]:
    """The service's record as the wire model's fields.

    Two types for one thing on purpose: the service dataclass is what the store
    hands back, and the pydantic model is what the schema is published from —
    the same split `ScrapeResult` and a `Job` already live on.
    """
    return {"path": staged.path, "name": staged.name, "bytes": staged.bytes,
            "sha256": staged.sha256, "content_type": staged.content_type}


def _bearer(headers) -> str | None:
    auth = headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _status_for(exc: uploads_service.UploadsError) -> int:
    for kind, status in _STATUS:
        if isinstance(exc, kind):
            return status
    return 400


def _decode(raw: bytes) -> str:
    """A filename off the wire. Never trusted — services.uploads.safe_name is
    what turns it into a path component — so a hostile encoding is a cosmetic
    problem here, not a security one."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


class _Ingest:
    """Turns the multipart parser's synchronous callbacks into staged files.

    The parser hands data to plain functions, and staging is async (it takes the
    manifest lock to commit). Starlette solves the same problem the same way:
    the callbacks record what happened and the async loop performs it after each
    `parser.write()` returns. Holding events rather than acting inside the
    callback is also what keeps a part's commit ordered *before* the next part's
    open, so the second file in one POST sees the first in the manifest and the
    per-ticket cap counts it.

    Memory is bounded by one read from the socket: whatever a single chunk
    contains is drained immediately afterwards.
    """

    def __init__(self, store, *, handle: str, subject: str,
                 declared: int | None = None) -> None:
        self._store = store
        self._handle = handle
        self._subject = subject
        self._declared = declared
        self._staged_bytes = 0
        self._events: list[tuple[str, Any]] = []
        self._disposition = b""
        self._field = b""
        self._value = b""
        self._filename: str | None = None
        self._files = 0
        self._write: StagedWrite | None = None
        self.staged: list[StagedFile] = []

    # ── parser callbacks: record, never act ──
    def on_part_begin(self) -> None:
        self._disposition = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        if self._field.lower() == b"content-disposition":
            self._disposition = self._value
        self._field = b""
        self._value = b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        raw = options.get(b"filename")
        # A part with a filename is a file, whatever its field name. The curl
        # this server hands out says `-F file=@…`, but a model that typed
        # `-F photo=@…` meant the same thing and should not be told otherwise.
        self._filename = _decode(raw) if raw is not None else None
        if self._filename is None:
            return
        self._files += 1
        if self._files > uploads_service.MAX_FILES_PER_TICKET:
            raise uploads_service.TooMany(
                f"one upload URL takes at most {uploads_service.MAX_FILES_PER_TICKET} "
                "files. Call create_upload_url for another."
            )
        self._events.append(("begin", self._filename))

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._filename is None:
            return  # a plain form field: nothing to stage, and nothing buffered
        self._events.append(("data", bytes(data[start:end])))

    def on_part_end(self) -> None:
        if self._filename is not None:
            self._events.append(("end", None))
        self._filename = None

    def on_end(self) -> None:
        pass

    def callbacks(self) -> dict:
        return {
            name: getattr(self, name) for name in (
                "on_part_begin", "on_part_data", "on_part_end", "on_header_field",
                "on_header_value", "on_header_end", "on_headers_finished", "on_end",
            )
        }

    # ── the async half ──
    async def drain(self) -> None:
        events, self._events = self._events, []
        for kind, payload in events:
            if kind == "begin":
                # Whatever the client said the WHOLE body was, less what this
                # request has already staged, is an upper bound on the next
                # part — so the reservation stays tight for the ordinary
                # one-file curl and honest for a multi-part one.
                remaining = (
                    None if self._declared is None
                    else max(1, self._declared - self._staged_bytes)
                )
                self._write = await self._store.begin(
                    self._handle, subject=self._subject, filename=payload,
                    declared_bytes=remaining,
                )
            elif kind == "data" and self._write is not None:
                self._write.feed(payload)
            elif kind == "end" and self._write is not None:
                staged = await self._write.commit()
                self._staged_bytes += staged.bytes
                self.staged.append(staged)
                self._write = None

    def abort(self) -> None:
        """Drop whatever part was in flight. Idempotent; a no-op after a commit."""
        if self._write is not None:
            self._write.abort()
            self._write = None


async def _consume(request: Request, store, handle: str, subject: str,
                   declared: int | None) -> list[StagedFile]:
    _, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if not boundary:
        raise HTTPException(
            status_code=400,
            detail="send the file as multipart/form-data, e.g. curl -F file=@photo.jpg",
        )
    ingest = _Ingest(store, handle=handle, subject=subject, declared=declared)
    parser = MultipartParser(boundary, ingest.callbacks())
    read = 0
    try:
        async for chunk in request.stream():
            read += len(chunk)
            if read > _MAX_BODY:
                # A client that lied about (or omitted) Content-Length. The cap
                # still binds, it just costs the bytes already on the wire.
                raise uploads_service.TooLarge(
                    "that upload sent more than the "
                    f"{uploads_service.human_size(_MAX_BODY)} one upload URL can take in "
                    "total (it declared no length)"
                )
            parser.write(chunk)
            await ingest.drain()
        parser.finalize()
        await ingest.drain()
    finally:
        ingest.abort()
    return ingest.staged


@router.post("/uploads/{handle}", response_model=StagedUpload)
async def stage_upload(request: Request, handle: str) -> StagedUpload:
    """Stage one or more files against a ticket, and answer with their paths.

    The response carries the first file's record at the top level and every
    file's in `files`. That is not sloppiness: the pre-baked curl posts one file
    and the tool's own docstring promises a `path` a model can read without
    indexing, while `-F file=@a -F file=@b` is a natural thing to type and
    deserves a complete answer rather than a silent one.
    """
    if not origin_allowed(request.headers):
        logger.warning("rejected an upload from origin %r", request.headers.get("origin"))
        raise HTTPException(status_code=403, detail="origin not allowed")

    secret = request.app.state.secret.current()
    subject = uploads_service.ticket_subject(_bearer(request.headers), handle, secret)
    if subject is None:
        # Deliberately one message for missing, forged, expired, and minted-for-
        # another-handle. Which one it was is only useful to someone who should
        # not have been here.
        raise HTTPException(status_code=401, detail="invalid or expired upload token")

    raw_length = request.headers.get("content-length")
    declared = int(raw_length) if raw_length and raw_length.isdigit() else None
    if declared is not None and declared > _MAX_BODY:
        raise HTTPException(
            status_code=413,
            detail=f"that request is over the {uploads_service.human_size(_MAX_BODY)} one "
                   "upload URL can take in total",
        )

    try:
        staged = await _consume(
            request, request.app.state.uploads, handle, subject, declared
        )
    except uploads_service.UploadsError as exc:
        raise HTTPException(status_code=_status_for(exc), detail=str(exc)) from exc
    except FormParserError as exc:
        raise HTTPException(
            status_code=400, detail=f"that is not a well-formed multipart body: {exc}"
        ) from exc

    if not staged:
        raise HTTPException(
            status_code=400,
            detail="no file in that request — send it as a multipart part, "
                   "e.g. curl -F file=@photo.jpg",
        )
    first = staged[0]
    return StagedUpload(
        **_fields(first),
        files=[StagedFileView(**_fields(f)) for f in staged],
    )
