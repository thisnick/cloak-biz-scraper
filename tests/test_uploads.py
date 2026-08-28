"""Staged uploads: the path a model hands back, and why it is not a capability.

The bytes an agent POSTs here end up as an argument to `setInputFiles`, which
reads whatever it is pointed at — and two directories above the uploads root sit
`settings.json` and the `.dek` that decrypts them. So the properties under test
are, in order of how much they matter: a path we did not write down never
resolves, one belonging to another subject never resolves, an expired one never
resolves, and nothing that resolves can escape its own ticket directory. After
that: the caps bind while the bytes are still arriving, the content type comes
from the bytes rather than the name, and a sweep only ever removes what it can
prove is a dead ticket.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import time

import pytest
import pytest_asyncio
from conftest import isolate_auth
from fastapi.testclient import TestClient

from app.main import app
from app.services import reclaim, signing, tokens
from app.services import uploads as uploads_service
from app.services.uploads import (
    Expired,
    NoRoom,
    NotStaged,
    StagedUploads,
    TooLarge,
    TooMany,
    UnsupportedType,
    UploadService,
    safe_name,
    sniff,
    verify_ticket,
)

SECRET = "test-secret-value-long-enough"
SUBJECT = "owner"
OTHER = "somebody-else"

# Real headers, minimum length each. The point of every one of these is that the
# FIRST BYTES are what decide the type; the rest is padding nobody looks at.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 60
GIF = b"GIF89a" + b"\x02" * 60
WEBP = b"RIFF" + b"\x40\x00\x00\x00" + b"WEBP" + b"\x03" * 60
PDF = b"%PDF-1.4\n" + b"\x04" * 60
NOT_AN_IMAGE = b"NOTION_API_TOKEN=secret_abcdef\nPROXY_PASSWORD=hunter2\n"


async def _stream(data: bytes, *, chunk: int = 7):
    """Feed bytes the way a socket does: in pieces, none of them aligned to
    anything the parser or the sniffer would find convenient."""
    for i in range(0, len(data), chunk):
        yield data[i:i + chunk]


@pytest.fixture
def store(tmp_path) -> UploadService:
    return UploadService(tmp_path / "uploads")


async def _mint(store: UploadService, *, subject: str = SUBJECT):
    return await store.mint(subject=subject, secret=SECRET)


async def _stage(store: UploadService, handle: str, data: bytes = JPEG, *,
                 name: str = "photo.jpg", subject: str = SUBJECT):
    return await store.stage(
        handle, subject=subject, filename=name, stream=_stream(data)
    )


def _manifest(store: UploadService, handle: str) -> dict:
    return json.loads((store.root / handle / ".ticket.json").read_text())


def _fill(store: UploadService, handle: str, size: int) -> None:
    """Put bytes on the volume without going through the store.

    How they got there is not what these tests are about — "the volume already
    holds this much" is the precondition, and staging cannot produce it any
    more, because staging is now the thing that refuses to.
    """
    (store.root / handle / "filler.bin").write_bytes(b"x" * size)


def _rewrite_expiry(store: UploadService, handle: str, expires: float) -> None:
    """Age a ticket without waiting two hours. The manifest is the record every
    gate reads, so moving its clock is the honest way to simulate one."""
    path = store.root / handle / ".ticket.json"
    manifest = json.loads(path.read_text())
    manifest["expires"] = expires
    path.write_text(json.dumps(manifest))


# ── The token ────────────────────────────────────────────────────────────────


class TestTicketToken:
    """`aud` is the type system (services/signing.py). These are the confusions
    it exists to refuse."""

    def test_a_fresh_ticket_verifies_for_its_own_handle_and_subject(self):
        token = uploads_service.issue_ticket("upl_" + "a" * 16, SECRET, subject=SUBJECT)
        assert verify_ticket(token, "upl_" + "a" * 16, SECRET, subject=SUBJECT)

    def test_a_ticket_for_another_handle_is_refused(self):
        token = uploads_service.issue_ticket("upl_" + "a" * 16, SECRET, subject=SUBJECT)
        assert not verify_ticket(token, "upl_" + "b" * 16, SECRET, subject=SUBJECT)

    def test_another_subjects_ticket_is_refused(self):
        token = uploads_service.issue_ticket("upl_" + "a" * 16, SECRET, subject=OTHER)
        assert not verify_ticket(token, "upl_" + "a" * 16, SECRET, subject=SUBJECT)

    def test_an_expired_ticket_is_refused(self):
        token = uploads_service.issue_ticket(
            "upl_" + "a" * 16, SECRET, subject=SUBJECT, now=time.time() - 10_000
        )
        assert not verify_ticket(token, "upl_" + "a" * 16, SECRET, subject=SUBJECT)

    @pytest.mark.parametrize("kind", [tokens.CDP, tokens.VNC])
    def test_a_browser_token_cannot_write_bytes(self, kind):
        """The whole point of a separate audience: a token that drives or
        watches a browser is not a token that puts files on the volume."""
        handle = "upl_" + "a" * 16
        token = tokens.issue(handle, SECRET, kind=kind, subject=SUBJECT)
        assert not verify_ticket(token, handle, SECRET, subject=SUBJECT)

    def test_a_ticket_cannot_drive_a_browser(self):
        """And the reverse, which is the direction that would actually hurt."""
        handle = "upl_" + "a" * 16
        token = uploads_service.issue_ticket(handle, SECRET, subject=SUBJECT)
        assert not tokens.verify(token, handle, SECRET, kind=tokens.CDP, subject=SUBJECT)

    def test_a_forged_signature_is_refused(self):
        handle = "upl_" + "a" * 16
        token = uploads_service.issue_ticket(handle, "some-other-secret", subject=SUBJECT)
        assert not verify_ticket(token, handle, SECRET, subject=SUBJECT)

    def test_a_handle_that_is_not_ours_is_refused_before_any_signature_check(self):
        payload = signing.issue(
            {"aud": "upload:../../etc", "sub": SUBJECT}, SECRET, ttl_sec=3600
        )
        assert not verify_ticket(payload, "../../etc", SECRET, subject=SUBJECT)


# ── Sniffing ─────────────────────────────────────────────────────────────────


class TestSniffing:
    @pytest.mark.parametrize("data,expected", [
        (JPEG, "image/jpeg"), (PNG, "image/png"), (GIF, "image/gif"),
        (WEBP, "image/webp"), (PDF, "application/pdf"),
    ])
    def test_each_accepted_format_is_recognised_by_its_first_bytes(self, data, expected):
        assert sniff(data) == expected

    @pytest.mark.parametrize("data", [b"", b"hello", NOT_AN_IMAGE, b"RIFF" + b"\x00" * 20])
    def test_anything_else_is_not_a_type(self, data):
        assert sniff(data) is None


@pytest.mark.asyncio
class TestContentIsCheckedNotClaimed:
    async def test_a_secrets_file_named_photo_jpg_is_refused(self, store):
        """"Upload my .env" dies here, whatever it is called and whatever
        Content-Type rode in with it."""
        ticket = await _mint(store)
        with pytest.raises(UnsupportedType):
            await _stage(store, ticket.handle, NOT_AN_IMAGE, name="photo.jpg")

    async def test_the_refused_bytes_are_not_left_on_the_volume(self, store):
        ticket = await _mint(store)
        with pytest.raises(UnsupportedType):
            await _stage(store, ticket.handle, NOT_AN_IMAGE, name="photo.jpg")
        assert [p.name for p in (store.root / ticket.handle).iterdir()] == [".ticket.json"]

    async def test_an_empty_part_is_refused(self, store):
        ticket = await _mint(store)
        with pytest.raises(UnsupportedType):
            await _stage(store, ticket.handle, b"", name="empty.jpg")

    async def test_a_file_shorter_than_the_sniff_window_still_gets_decided(self, store):
        """Six bytes is a whole GIF header. The sniff must not silently accept
        whatever it could not finish reading."""
        ticket = await _mint(store)
        with pytest.raises(UnsupportedType):
            await _stage(store, ticket.handle, b"hi", name="tiny.gif")
        staged = await _stage(store, ticket.handle, b"GIF89a", name="tiny.gif")
        assert staged.content_type == "image/gif" and staged.bytes == 6


# ── Filenames ────────────────────────────────────────────────────────────────


class TestSafeName:
    @pytest.mark.parametrize("raw,expected", [
        ("photo.jpg", "photo.jpg"),
        ("../evil", "evil"),
        # A name that is nothing BUT an extension keeps it and gains a stem.
        # `.dek` and `.env` are not extensions anyone wants, but the rule that
        # produces them is the one that stops `a;rm -rf /.jpg` — basename
        # `.jpg` — arriving with no extension at all.
        ("../../.dek", "upload.dek"),
        ("a;rm -rf /.jpg", "upload.jpg"),
        ("/etc/passwd", "passwd"),
        ("C:\\photos\\a.jpg", "a.jpg"),
        (".ticket.json", "ticket.json"),
        (".env", "upload.env"),
        ("..", "upload.jpg"),
        ("", "upload.jpg"),
        ("caf\u00e9 \u2014 photo.jpg", "caf_photo.jpg"),
        # The extension has to survive a stem that does not. This feature exists
        # to post photos to somebody else's upload form, and half of those
        # validate the extension — a file called `jpg` fails them all.
        ("\u5199\u771f.jpg", "upload.jpg"),
        ("\u65e5\u672c\u8a9e.pdf", "upload.pdf"),
        ("\U0001f389.jpg", "upload.jpg"),
        ("\u00dcnter.png", "nter.png"),
    ])
    def test_a_filename_becomes_one_harmless_path_component(self, raw, expected):
        assert safe_name(raw, "image/jpeg") == expected

    def test_a_very_long_name_is_capped_and_keeps_its_extension(self):
        name = safe_name("x" * 300 + ".jpg", "image/jpeg")
        assert len(name) <= uploads_service.MAX_NAME_LEN
        assert name.endswith(".jpg")

    def test_no_safe_name_can_shadow_the_manifest_or_a_temp_file(self):
        """The invariant that makes the manifest un-overwritable by the files it
        describes: nothing that comes out of here starts with a dot."""
        for raw in (".ticket.json", ".incoming-aaaa", "...", "./.ticket.json"):
            assert not safe_name(raw, "image/jpeg").startswith(".")


@pytest.mark.asyncio
class TestFilenamesOnDisk:
    @pytest.mark.parametrize("raw", ["../evil.jpg", "/etc/passwd.jpg", "", "..",
                                     "caf\u00e9.jpg", "y" * 300 + ".jpg"])
    async def test_a_hostile_name_stays_inside_the_ticket_directory(self, store, raw):
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle, JPEG, name=raw)
        assert pathlib.Path(staged.path).parent == store.root / ticket.handle
        assert pathlib.Path(staged.path).is_file()

    async def test_two_different_files_with_one_name_are_two_files(self, store):
        ticket = await _mint(store)
        first = await _stage(store, ticket.handle, JPEG, name="photo.jpg")
        second = await _stage(store, ticket.handle, PNG, name="photo.jpg")
        assert first.name == "photo.jpg" and second.name == "photo-1.jpg"
        assert first.path != second.path
        assert pathlib.Path(first.path).read_bytes() == JPEG
        assert pathlib.Path(second.path).read_bytes() == PNG


# ── Dedupe ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDedupe:
    async def test_the_same_bytes_twice_is_one_file_at_one_path(self, store):
        """What makes a retried curl idempotent instead of doubling the volume."""
        ticket = await _mint(store)
        first = await _stage(store, ticket.handle, JPEG, name="photo.jpg")
        second = await _stage(store, ticket.handle, JPEG, name="different-name.jpg")

        assert second.path == first.path
        assert len(_manifest(store, ticket.handle)["files"]) == 1
        staged_files = [p.name for p in (store.root / ticket.handle).iterdir()]
        assert sorted(staged_files) == [".ticket.json", "photo.jpg"]


# ── Caps ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCaps:
    async def test_the_per_file_cap_trips_while_the_bytes_are_still_arriving(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 64)
        ticket = await _mint(store)
        with pytest.raises(TooLarge, match="single upload"):
            await _stage(store, ticket.handle, JPEG + b"\x00" * 5000)

    async def test_a_tripped_cap_leaves_no_partial_file_behind(self, store, monkeypatch):
        """A 250 MB partial sitting on the volume until the next sweep is the
        failure this is written to prevent."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 64)
        ticket = await _mint(store)
        with pytest.raises(TooLarge):
            await _stage(store, ticket.handle, JPEG + b"\x00" * 5000)

        left = sorted(p.name for p in (store.root / ticket.handle).iterdir())
        assert left == [".ticket.json"], f"partial bytes survived: {left}"
        assert _manifest(store, ticket.handle)["files"] == []

    async def test_the_per_ticket_total_binds_across_files(self, store, monkeypatch):
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 200)
        ticket = await _mint(store)
        await _stage(store, ticket.handle, JPEG + b"\x05" * 60, name="a.jpg")
        with pytest.raises(TooLarge, match="in total"):
            await _stage(store, ticket.handle, PNG + b"\x06" * 120, name="b.png")
        assert len(_manifest(store, ticket.handle)["files"]) == 1

    async def test_the_thirteenth_file_is_refused(self, store):
        """The real constant, not a patched one: twelve small files fit and the
        next one does not."""
        ticket = await _mint(store)
        for n in range(uploads_service.MAX_FILES_PER_TICKET):
            await _stage(store, ticket.handle, JPEG + bytes([n]), name=f"p{n}.jpg")
        with pytest.raises(TooMany, match="12 files"):
            await _stage(store, ticket.handle, JPEG + b"\xfe", name="p13.jpg")
        assert len(_manifest(store, ticket.handle)["files"]) == 12

    async def test_minting_past_the_global_budget_is_refused_and_says_where_to_look(
        self, store, monkeypatch
    ):
        """The budget is now measured in numbers a manifest fits inside: the
        server's own bookkeeping is charged to the volume too, so a "budget" of
        128 bytes is smaller than an empty ticket and no longer a coherent
        value to test with."""
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 4096)
        ticket = await _mint(store)
        _fill(store, ticket.handle, 4096)

        with pytest.raises(NoRoom) as refused:
            await _mint(store)
        assert "Settings" in str(refused.value) and "Disk space" in str(refused.value)

    async def test_the_budget_counts_only_what_is_still_there(self, store, monkeypatch):
        """Refusing to mint has to be recoverable, and the sweep inside mint is
        what recovers it — otherwise one big expired ticket wedges the feature."""
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 4096)
        ticket = await _mint(store)
        _fill(store, ticket.handle, 4096)
        _rewrite_expiry(store, ticket.handle, time.time() - 1)

        fresh = await _mint(store)  # the sweep inside mint frees the space first
        assert fresh.handle != ticket.handle
        assert not (store.root / ticket.handle).exists()


# ── The volume bound ─────────────────────────────────────────────────────────


def _disk(root: pathlib.Path) -> int:
    """Every byte under the root, temp files included. What the volume holds —
    not what the manifests say it holds, which is a different question and the
    one that was already right when this was wrong."""
    import os

    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


async def _sample_peak(root, peak, stop):
    """Watch the volume while writers run. Without this the tests could only see
    the tidy state afterwards, and the failure being guarded against was a PEAK
    that no final measurement would ever show."""
    import asyncio

    while not stop.is_set():
        peak[0] = max(peak[0], _disk(root))
        await asyncio.sleep(0)
    peak[0] = max(peak[0], _disk(root))


async def _slow_body(total: int, marker: int):
    """Bytes in pieces, yielding between them, so writers really interleave.
    Distinct per writer so the sha256 dedupe cannot quietly collapse them."""
    import asyncio

    head = JPEG + bytes([marker % 256]) * 8
    yield head
    sent = len(head)
    while sent < total:
        piece = min(4096, total - sent)
        yield bytes([marker % 256]) * piece
        sent += piece
        await asyncio.sleep(0)


@pytest.mark.asyncio
class TestTheVolumeIsBounded:
    """The invariant: the bytes under the uploads root can never exceed
    UPLOADS_BUDGET_BYTES, under any amount of concurrency.

    The first version of this store measured the volume and then granted, which
    is not a bound — eight writers all measure before any of them commits. It
    was measured at 8.00x the per-ticket cap. Admission is a reservation now,
    and these are the tests that would notice if it stopped being one.
    """

    async def test_concurrent_writers_cannot_exceed_the_per_ticket_cap(
        self, store, monkeypatch
    ):
        import asyncio

        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 64 * 1024)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 64 * 1024)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 1024 * 1024)
        ticket = await _mint(store)
        peak, stop = [0], asyncio.Event()
        watch = asyncio.create_task(_sample_peak(store.root, peak, stop))

        async def writer(i):
            try:
                await store.stage(ticket.handle, subject=SUBJECT, filename=f"f{i}.jpg",
                                  stream=_slow_body(64 * 1024, i))
            except uploads_service.UploadsError:
                pass

        await asyncio.gather(*[writer(i) for i in range(8)])
        stop.set()
        await watch

        assert peak[0] <= uploads_service.MAX_BYTES_PER_TICKET + 4096, (
            f"eight writers put {peak[0]} bytes on a volume whose ticket cap is "
            f"{uploads_service.MAX_BYTES_PER_TICKET}"
        )

    async def test_concurrent_writers_across_many_tickets_cannot_exceed_the_budget(
        self, store, monkeypatch
    ):
        """A ticket confers no bytes — so however many are minted, the volume is
        still bounded. This is the composite case: many tickets, many writers."""
        import asyncio

        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 32 * 1024)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 32 * 1024)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 128 * 1024)
        handles = [(await _mint(store)).handle for _ in range(10)]
        peak, stop = [0], asyncio.Event()
        watch = asyncio.create_task(_sample_peak(store.root, peak, stop))

        async def writer(i):
            try:
                await store.stage(handles[i % len(handles)], subject=SUBJECT,
                                  filename=f"f{i}.jpg",
                                  stream=_slow_body(32 * 1024, i))
            except uploads_service.UploadsError:
                pass

        await asyncio.gather(*[writer(i) for i in range(20)])
        stop.set()
        await watch

        assert peak[0] <= uploads_service.UPLOADS_BUDGET_BYTES, (
            f"twenty writers across ten tickets put {peak[0]} bytes on a volume "
            f"budgeted at {uploads_service.UPLOADS_BUDGET_BYTES}"
        )

    async def test_many_mints_at_once_on_a_full_volume_are_all_refused(
        self, store, monkeypatch
    ):
        """Twenty-five callers measuring the same full volume at the same time
        used to be twenty-five grants."""
        import asyncio

        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 8192)
        first = await _mint(store)
        _fill(store, first.handle, 8192)

        async def ask():
            try:
                await _mint(store)
                return True
            except NoRoom:
                return False

        granted = sum(await asyncio.gather(*[ask() for _ in range(25)]))
        assert granted == 0, f"{granted} of 25 mints were granted on a full volume"

    async def test_the_last_ticket_that_fits_is_granted_to_exactly_one_caller(
        self, store, monkeypatch
    ):
        """The narrow case the lock exists for.

        A volume with room for ONE more ticket's bookkeeping, and twenty-five
        callers asking at once. Deciding and then creating without holding the
        lock lets all twenty-five past the same measurement — which is the same
        shape of bug as the one this whole rework is about, just in bytes small
        enough to be easy to wave away.
        """
        import asyncio

        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 1024 * 1024)
        await _mint(store)
        one = _disk(store.root)
        await _mint(store)
        manifest_bytes = _disk(store.root) - one
        assert manifest_bytes > 0, "a ticket really does cost bytes"

        # Room for one more manifest, and not for two.
        monkeypatch.setattr(
            uploads_service, "UPLOADS_BUDGET_BYTES",
            _disk(store.root) + uploads_service._MANIFEST_SEED_BYTES + manifest_bytes // 2,
        )

        async def ask():
            try:
                await _mint(store)
                return True
            except NoRoom:
                return False

        granted = sum(await asyncio.gather(*[ask() for _ in range(25)]))
        assert granted <= 1, f"{granted} callers were handed the last free slot"
        assert _disk(store.root) <= uploads_service.UPLOADS_BUDGET_BYTES

    async def test_bytes_already_written_are_not_charged_twice(self, store, monkeypatch):
        """A reservation covers the temp file it is writing, so counting that
        file as well would charge those bytes twice and the usable budget would
        shrink and grow with traffic. Two writes that fit must both be admitted
        even while the first one's bytes are on disk."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 8192)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 64 * 1024)
        ticket = await _mint(store)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES",
                            _disk(store.root) + 2 * 8192 + 2048)

        first = await store.begin(ticket.handle, subject=SUBJECT, filename="a.jpg",
                                  declared_bytes=8192)
        async for chunk in _slow_body(8192, 1):
            first.feed(chunk)  # on disk, not yet committed

        second = await store.begin(ticket.handle, subject=SUBJECT, filename="b.jpg",
                                   declared_bytes=8192)
        async for chunk in _slow_body(8192, 2):
            second.feed(chunk)
        assert (await second.commit()).bytes == 8192
        assert (await first.commit()).bytes == 8192
        assert _disk(store.root) <= uploads_service.UPLOADS_BUDGET_BYTES

    async def test_a_refusal_caused_by_an_upload_in_flight_says_so(
        self, store, monkeypatch
    ):
        """"No room" on a volume that is visibly empty is a baffling thing to be
        told. When what is in the way is bytes PROMISED rather than bytes
        written, the refusal has to name the cause and the fix — the same
        standard the two `upload` refusals are held to."""
        # Equal caps, so one pessimistic reservation uses the ticket's whole
        # room and the next caller has none. Note how narrow that is: while ANY
        # room is left, a second write is admitted with a smaller allowance
        # rather than refused, so this is the edge and not the common case.
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 32 * 1024)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 32 * 1024)
        ticket = await _mint(store)

        holding = await store.begin(ticket.handle, subject=SUBJECT, filename="a.jpg")
        try:
            with pytest.raises(TooLarge) as refused:
                await store.begin(ticket.handle, subject=SUBJECT, filename="b.jpg")
        finally:
            holding.abort()

        message = str(refused.value)
        assert "still in flight" in message
        assert "Content-Length" in message
        assert "Retry" in message

    async def test_an_ordinary_too_large_refusal_does_not_mention_other_uploads(
        self, store, monkeypatch
    ):
        """The note is conditional. With nothing in flight, the plain refusal
        must read exactly as it did — an explanation nobody needs is noise."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 64)
        ticket = await _mint(store)
        with pytest.raises(TooLarge) as refused:
            await _stage(store, ticket.handle, JPEG + b"\x00" * 5000)
        assert "in flight" not in str(refused.value)

    async def test_committing_gives_the_reservation_back(self, store, monkeypatch):
        """Driven through begin/commit rather than `stage`, deliberately.

        `stage` aborts in a `finally`, and abort releases too — so a commit that
        stopped releasing was invisible to every test that went through it. The
        release has to be watched where it happens.
        """
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 4096)
        ticket = await _mint(store)

        write = await store.begin(ticket.handle, subject=SUBJECT, filename="a.jpg")
        assert store._reserved, "the write is holding budget while it runs"
        write.feed(JPEG)
        staged = await write.commit()

        assert staged.bytes == len(JPEG)
        assert store._reserved == {}, "a committed upload is still holding budget"

    async def test_successive_uploads_reuse_the_space_the_last_one_released(
        self, store, monkeypatch
    ):
        """The behaviour that release-on-commit buys: without it the ledger
        climbs with every upload and the fourth is refused on a volume holding
        almost nothing."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 100_000)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 250_000)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 250_000)
        ticket = await _mint(store)

        for n in range(6):
            write = await store.begin(ticket.handle, subject=SUBJECT,
                                      filename=f"p{n}.jpg")
            write.feed(JPEG + bytes([n]))
            await write.commit()
            assert store._reserved == {}, f"upload {n} kept its reservation"

        assert len(_manifest(store, ticket.handle)["files"]) == 6
        assert _disk(store.root) < 10_000, "six tiny files, and the volume knows it"

    async def test_a_crash_orphan_is_reclaimed_at_startup(self, store):
        """The one thing the budget walk cannot see.

        `.incoming-*` is excluded from the volume measurement because its
        writer's reservation already covers it — true while the writer lives,
        false after a kill -9, an OOM, or a redeploy mid-upload. The ledger dies
        with the process and the file does not, so those bytes are on the disk
        and in no total. Nothing sweeps them either: they sit inside tickets
        that have not expired.
        """
        ticket = await _mint(store)
        orphan = store.root / ticket.handle / ".incoming-deadbeefdeadbeef"
        orphan.write_bytes(b"x" * 40_000)
        live = await _stage(store, ticket.handle, JPEG, name="real.jpg")

        # The state the reviewer measured: on the disk, invisible to the budget.
        assert _disk(store.root) > 40_000
        assert store._volume_committed_and_reserved() < 40_000

        swept = await store.sweep(reclaim_incoming=True)

        assert not orphan.exists()
        assert swept.bytes >= 40_000, "the sweep did not report what it freed"
        assert pathlib.Path(live.path).is_file(), "a real staged file was taken too"
        assert store._volume_committed_and_reserved() == _disk(store.root)

    async def test_a_ticket_cleared_mid_write_is_a_refusal_not_a_traceback(
        self, store, monkeypatch
    ):
        """The race `busy_handles` normally prevents, forced open.

        A naive version of this — rmtree the directory and commit — cannot reach
        the guard at all: `commit` re-reads the manifest first and raises the
        OTHER `NotStaged`. So the directory is removed at the last possible
        moment, between that read and the rename, which is exactly where a
        concurrent clear would land if the busy check ever stopped covering it.
        """
        import shutil

        ticket = await _mint(store)
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="a.jpg")
        write.feed(JPEG)

        real_unique = uploads_service._unique_name

        def vanish(name, taken):
            shutil.rmtree(store.root / ticket.handle)
            return real_unique(name, taken)

        monkeypatch.setattr(uploads_service, "_unique_name", vanish)

        with pytest.raises(NotStaged) as refused:
            await write.commit()

        assert "cleared while the file was arriving" in str(refused.value)
        assert isinstance(refused.value.__cause__, OSError), (
            "the underlying failure was swallowed rather than wrapped"
        )
        assert store._reserved == {}, "the abandoned write kept its reservation"

    async def test_reclaiming_refuses_to_run_while_a_write_is_in_flight(self, store):
        """`reclaim_incoming=True` deletes part-written files, which is safe at
        startup and nowhere else. It is a public parameter, so the precondition
        is asserted rather than left to a caller reading the comment — measured
        before this guard existed, a mid-flight call freed 50,000 bytes and took
        the live temp file with it."""
        ticket = await _mint(store)
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="big.jpg")
        try:
            write.feed(JPEG + b"\x00" * 8192)
            partial = next(p for p in (store.root / ticket.handle).iterdir()
                           if p.name.startswith(".incoming-"))

            with pytest.raises(AssertionError, match="startup-only"):
                await store.sweep(reclaim_incoming=True)

            assert partial.exists(), "the live write was reclaimed anyway"
        finally:
            write.abort()

        # …and with nothing in flight it runs, which is the case it exists for.
        assert (await store.sweep(reclaim_incoming=True)) is not None

    async def test_an_ordinary_sweep_leaves_an_in_flight_write_alone(self, store):
        """`reclaim_incoming` is a startup-only door. At any other moment a
        `.incoming-*` file belongs to a write that is still happening."""
        ticket = await _mint(store)
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="big.jpg")
        try:
            write.feed(JPEG + b"\x00" * 8192)
            partial = next(p for p in (store.root / ticket.handle).iterdir()
                           if p.name.startswith(".incoming-"))

            await store.sweep()

            assert partial.exists(), "a sweep took a file that was still arriving"
        finally:
            write.abort()

    async def test_the_in_flight_note_counts_what_it_attributes(self, store,
                                                                monkeypatch):
        """A global count under a local description sends a model the wrong way:
        "3 other uploads to this upload URL" when two of them are elsewhere is
        the difference between waiting and fetching a fresh URL."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 32 * 1024)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 32 * 1024)
        here, there = await _mint(store), await _mint(store)

        mine = await store.begin(here.handle, subject=SUBJECT, filename="a.jpg")
        theirs = [await store.begin(there.handle, subject=SUBJECT, filename="b.jpg")]
        try:
            with pytest.raises(TooLarge) as refused:
                await store.begin(here.handle, subject=SUBJECT, filename="c.jpg")
        finally:
            mine.abort()
            for w in theirs:
                w.abort()

        message = str(refused.value)
        assert "1 other upload to this upload URL" in message
        assert "1 upload elsewhere on this server" in message
        assert "3 other uploads to this upload URL" not in message

    async def test_the_note_names_only_the_place_that_has_uploads(self, store,
                                                                  monkeypatch):
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 32 * 1024)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 32 * 1024)
        ticket = await _mint(store)

        holding = await store.begin(ticket.handle, subject=SUBJECT, filename="a.jpg")
        try:
            with pytest.raises(TooLarge) as refused:
                await store.begin(ticket.handle, subject=SUBJECT, filename="b.jpg")
        finally:
            holding.abort()

        message = str(refused.value)
        assert "1 other upload to this upload URL" in message
        assert "elsewhere on this server" not in message

    async def test_a_refused_upload_gives_its_reservation_back(self, store, monkeypatch):
        """An upload that fails must cost nothing. Otherwise a run of failures
        is its own outage: the volume looks full and nothing can be freed."""
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 4096)
        ticket = await _mint(store)

        with pytest.raises(UnsupportedType):
            await _stage(store, ticket.handle, NOT_AN_IMAGE, name="x.jpg")
        assert store._reserved == {}, "a refused upload is still holding budget"

        staged = await _stage(store, ticket.handle, JPEG, name="ok.jpg")
        assert pathlib.Path(staged.path).is_file()
        assert store._reserved == {}, "a committed upload is still holding budget"

    async def test_a_body_longer_than_it_declared_is_cut_off_at_what_it_declared(
        self, store
    ):
        """The reservation is a ceiling as well as a promise. A client that
        declares a small length and then streams a large body would otherwise
        buy back exactly the slack the reservation removed."""
        ticket = await _mint(store)
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="liar.jpg",
                                  declared_bytes=100)
        with pytest.raises(TooLarge, match="Content-Length"):
            async for chunk in _slow_body(50_000, 1):
                write.feed(chunk)

        assert sorted(p.name for p in (store.root / ticket.handle).iterdir()) == [
            ".ticket.json"
        ]
        assert store._reserved == {}

    async def test_an_upload_that_would_pass_the_budget_is_refused_not_written(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 16 * 1024)
        ticket = await _mint(store)
        _fill(store, ticket.handle, 15 * 1024)

        with pytest.raises(NoRoom, match="Disk space"):
            await _stage(store, ticket.handle, JPEG + b"\x00" * 8192, name="big.jpg")

        assert _disk(store.root) <= uploads_service.UPLOADS_BUDGET_BYTES
        assert store._reserved == {}


# ── resolve_for: the security function ───────────────────────────────────────


@pytest.mark.asyncio
class TestResolveForContainment:
    async def test_a_staged_path_resolves_to_the_file_we_wrote(self, store):
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)

        resolved = store.resolve_for(SUBJECT, [staged.path])

        assert resolved == [pathlib.Path(staged.path).resolve()]
        assert resolved[0].read_bytes() == JPEG

    @pytest.mark.parametrize("attack", [
        "/data/.dek",
        "/etc/passwd",
        "../../.dek",
        "/data/uploads/../../.dek",
        "%2e%2e%2f%2e%2e%2f.dek",
        "/data/settings.json",
    ])
    async def test_a_path_we_never_wrote_down_is_refused(self, store, attack):
        await _mint(store)  # a live ticket exists; it still buys the caller nothing
        with pytest.raises(NotStaged, match="not an uploaded file"):
            store.resolve_for(SUBJECT, [attack])

    async def test_the_dek_two_directories_up_from_a_real_ticket_is_refused(
        self, store, tmp_path
    ):
        """The concrete threat, spelled out: the volume root holds the key that
        decrypts the licence, proxy and Notion credentials."""
        dek = tmp_path / ".dek"
        dek.write_bytes(b"the key that decrypts settings.json")
        ticket = await _mint(store)
        await _stage(store, ticket.handle)

        for attack in (str(dek),
                       f"{store.root / ticket.handle}/../../.dek",
                       f"{store.root / ticket.handle}/../.dek"):
            with pytest.raises(NotStaged):
                store.resolve_for(SUBJECT, [attack])

    async def test_a_name_that_is_not_in_the_manifest_is_refused(self, store):
        """Gate 1 on its own: the right ticket, a file that is really there, and
        no manifest entry for it."""
        ticket = await _mint(store)
        await _stage(store, ticket.handle)
        planted = store.root / ticket.handle / "planted.jpg"
        planted.write_bytes(JPEG)

        with pytest.raises(NotStaged):
            store.resolve_for(SUBJECT, [str(planted)])

    async def test_a_symlink_inside_a_ticket_pointing_out_is_refused(self, store, tmp_path):
        """Gate 2 earning its keep: the manifest says yes, and resolve() lands
        outside the ticket, so the answer is still no."""
        dek = tmp_path / ".dek"
        dek.write_bytes(b"the key that decrypts settings.json")
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)

        real = pathlib.Path(staged.path)
        real.unlink()
        real.symlink_to(dek)
        assert real.read_bytes() == dek.read_bytes(), "the link really does reach it"

        with pytest.raises(NotStaged):
            store.resolve_for(SUBJECT, [staged.path])

    async def test_the_returned_path_comes_from_the_manifest_not_the_caller(self, store):
        """`reclaim.removable_child`'s rule, applied here: callers use the Path
        returned, never the one they passed in. A caller string with a `.` in it
        must come back canonical, or something downstream is using the input."""
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)
        noisy = f"{store.root / ticket.handle}/./photo.jpg"

        assert store.resolve_for(SUBJECT, [noisy]) == [pathlib.Path(staged.path).resolve()]

    async def test_every_path_in_one_call_is_checked(self, store):
        """The variadic case: one bad path poisons the whole call rather than
        being quietly dropped from the list handed to the subprocess."""
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)
        with pytest.raises(NotStaged):
            store.resolve_for(SUBJECT, [staged.path, "/data/.dek"])


@pytest.mark.asyncio
class TestResolveForOwnership:
    async def test_another_subject_cannot_resolve_this_subjects_file(self, store):
        ticket = await _mint(store, subject=SUBJECT)
        staged = await _stage(store, ticket.handle, subject=SUBJECT)

        with pytest.raises(NotStaged):
            store.resolve_for(OTHER, [staged.path])
        assert store.resolve_for(SUBJECT, [staged.path])  # still fine for its owner

    async def test_another_subject_cannot_stage_into_this_subjects_ticket(self, store):
        ticket = await _mint(store, subject=SUBJECT)
        with pytest.raises(NotStaged):
            await _stage(store, ticket.handle, subject=OTHER)


@pytest.mark.asyncio
class TestResolveForExpiry:
    async def test_a_path_stops_resolving_at_its_ttl_with_the_bytes_still_there(
        self, store
    ):
        """Expiry is checked on read, which is what makes the sweep non-urgent —
        and what makes "that file has expired" the only status check this
        feature needs."""
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)
        past_ttl = time.time() + uploads_service.TTL_SEC + 1

        with pytest.raises(Expired, match="upload it again"):
            store.resolve_for(SUBJECT, [staged.path], now=past_ttl)
        assert pathlib.Path(staged.path).is_file(), "no sweep has run; the bytes are there"

    async def test_expired_and_never_staged_are_different_refusals(self, store):
        """Unit B surfaces both to a model and they call for different fixes, so
        they must not collapse into one message."""
        ticket = await _mint(store)
        staged = await _stage(store, ticket.handle)
        past_ttl = time.time() + uploads_service.TTL_SEC + 1

        with pytest.raises(Expired):
            store.resolve_for(SUBJECT, [staged.path], now=past_ttl)
        with pytest.raises(NotStaged):
            store.resolve_for(SUBJECT, ["/data/.dek"], now=past_ttl)

    async def test_staging_into_an_expired_ticket_is_refused(self, store):
        ticket = await _mint(store)
        _rewrite_expiry(store, ticket.handle, time.time() - 1)
        with pytest.raises(Expired):
            await _stage(store, ticket.handle)


# ── Sweep ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSweep:
    async def test_it_removes_expired_tickets_and_keeps_live_ones(self, store):
        dead = await _mint(store)
        await _stage(store, dead.handle, JPEG + b"\x08" * 100)
        live = await _mint(store)
        await _stage(store, live.handle, PNG + b"\x09" * 100)
        _rewrite_expiry(store, dead.handle, time.time() - 1)

        swept = await store.sweep()

        assert swept.handles == 1 and swept.kept == 1
        assert swept.files == 2, "the manifest counts as a file on the volume"
        assert swept.bytes > 100
        assert not (store.root / dead.handle).exists()
        assert (store.root / live.handle).is_dir()

    async def test_an_empty_root_sweeps_to_nothing(self, store):
        swept = await store.sweep()
        assert (swept.handles, swept.bytes, swept.kept, swept.refused) == (0, 0, 0, 0)

    async def test_an_entry_the_containment_rule_refuses_is_left_alone(
        self, store, monkeypatch
    ):
        """Never delete on a guess. If `reclaim` will not vouch for an entry, it
        stays where it is and is reported — and the sweep still finishes its
        other work."""
        odd = await _mint(store)
        also_dead = await _mint(store)
        for handle in (odd.handle, also_dead.handle):
            _rewrite_expiry(store, handle, time.time() - 1)

        real_remove = reclaim.remove_child

        def refuse_one(root, entry):
            if pathlib.Path(entry).name == odd.handle:
                raise reclaim.Unsafe("refusing to remove it: it resolves outside")
            return real_remove(root, entry)

        monkeypatch.setattr(uploads_service.reclaim, "remove_child", refuse_one)
        swept = await store.sweep()

        assert swept.refused == 1 and swept.handles == 1
        assert (store.root / odd.handle).is_dir(), "left exactly where it was"
        assert not (store.root / also_dead.handle).exists()

    async def test_a_directory_with_no_manifest_is_left_until_it_is_older_than_any_ticket(
        self, store
    ):
        """A ticket caught between mkdir and its first manifest write. Removing
        it would delete a ticket somebody is being handed right now."""
        store.root.mkdir(parents=True, exist_ok=True)
        mid_mint = store.root / "upl_00112233445566aa"
        mid_mint.mkdir()

        assert (await store.sweep()).kept == 1
        assert mid_mint.is_dir()

        swept = await store.sweep(now=time.time() + uploads_service.TTL_SEC + 60)
        assert swept.handles == 1 and not mid_mint.exists()

    async def test_a_symlink_in_the_root_is_unlinked_never_followed(self, store, tmp_path):
        outside = tmp_path / "somebody-elses"
        outside.mkdir()
        (outside / "huge").write_bytes(b"x" * 5000)
        store.root.mkdir(parents=True, exist_ok=True)
        link = store.root / "upl_00112233445566bb"
        link.symlink_to(outside)
        import os
        long_ago = time.time() - uploads_service.TTL_SEC - 60
        os.utime(link, (long_ago, long_ago), follow_symlinks=False)

        swept = await store.sweep()

        assert swept.bytes == 0, "somebody else's disk was never counted as ours"
        assert not link.exists() and not link.is_symlink()
        assert (outside / "huge").is_file(), "the target was never touched"


# ── Measurement ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStagedUploads:
    async def test_it_counts_handles_files_and_bytes(self, store):
        ticket = await _mint(store)
        await _stage(store, ticket.handle, JPEG + b"\x0a" * 100, name="a.jpg")
        await _stage(store, ticket.handle, PNG + b"\x0b" * 100, name="b.png")

        view = await StagedUploads(lambda: store).snapshot()

        assert view.handles == 1 and view.files == 2
        assert view.bytes > 200 and view.expired == 0

    async def test_expired_bytes_are_reported_not_hidden(self, store):
        """TaskHistory's honesty about orphans, applied here: bytes whose ticket
        is dead are still bytes on the volume."""
        ticket = await _mint(store)
        await _stage(store, ticket.handle)
        _rewrite_expiry(store, ticket.handle, time.time() - 1)

        view = await StagedUploads(lambda: store).snapshot()

        assert view.handles == 1 and view.expired == 1 and view.bytes > 0

    async def test_an_empty_volume_measures_as_nothing(self, store):
        view = await StagedUploads(lambda: store).snapshot()
        assert (view.handles, view.files, view.bytes, view.expired) == (0, 0, 0, 0)

    async def test_a_repeat_visit_costs_nothing_when_nothing_has_moved(self, store):
        """The reason there is a cache at all: this is a walk of the volume and
        a page visit must not pay for it twice."""
        sizes = StagedUploads(lambda: store)
        ticket = await _mint(store)
        await _stage(store, ticket.handle)

        first = await sizes.snapshot()
        assert first.handles == 1
        assert (await sizes.snapshot()) is first, "the second visit re-walked"

    async def test_the_number_never_outlives_the_bytes_it_describes(self, store):
        """Uploads are the most volatile of the three things Disk space reports,
        and a sweep runs on every mint — so a view cached the way the browser
        cache's is would be wrong within one tool call and stay wrong until
        somebody pressed Clear."""
        sizes = StagedUploads(lambda: store)
        ticket = await _mint(store)
        await _stage(store, ticket.handle, JPEG + b"\x0e" * 900)
        loaded = await sizes.snapshot()
        assert loaded.handles == 1 and loaded.bytes > 900

        _rewrite_expiry(store, ticket.handle, time.time() - 1)
        await store.sweep()          # …as a mint would

        after = await sizes.snapshot()
        assert after.handles == 0, "the page is still showing a volume that is gone"
        assert after.bytes == 0
        assert after.measured_at >= loaded.measured_at

    async def test_a_removal_that_half_failed_is_reported_not_swallowed(
        self, store, monkeypatch
    ):
        """The banner's job is to explain, at the moment of action, anything the
        row could not. A failure counted nowhere breaks that: reclaim.Unsafe is
        reported and `not gone` was not, so a sweep that failed on every entry
        returned all zeros — indistinguishable from an empty volume. The row
        then said `1 expired` while the banner said there was nothing to clear.
        """
        ticket = await _mint(store)
        await _stage(store, ticket.handle)
        _rewrite_expiry(store, ticket.handle, time.time() - 1)
        monkeypatch.setattr(uploads_service.reclaim, "remove_child",
                            lambda root, entry: False)

        swept = await store.clear()

        assert swept.refused == 1, "a failed removal was counted nowhere"
        assert swept.handles == 0, "it must not claim to have removed anything"
        assert (store.root / ticket.handle).is_dir(), "the entry should still be there"

    async def test_a_fresh_upload_shows_up_without_anyone_invalidating(self, store):
        sizes = StagedUploads(lambda: store)
        assert (await sizes.snapshot()).handles == 0

        ticket = await _mint(store)
        await _stage(store, ticket.handle)

        assert (await sizes.snapshot()).handles == 1

    async def test_every_way_the_volume_moves_bumps_the_revision(self, store):
        """The three sites individually, because the row's freshness is only as
        good as the least-remembered one — and `abort` was missed exactly this
        way: the caching tests observe the EFFECT through mint and sweep and
        never the mechanism, so a site with no bump was invisible."""
        before = store.revision
        ticket = await _mint(store)
        assert store.revision > before, "minting did not bump the revision"

        after_mint = store.revision
        await _stage(store, ticket.handle)
        assert store.revision > after_mint, "committing did not bump the revision"

        after_commit = store.revision
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="x.jpg")
        write.feed(JPEG + b"\x00" * 4096)
        write.abort()
        assert store.revision > after_commit, "abandoning a write did not bump"

    async def test_an_abandoned_upload_does_not_leave_a_stale_number(self, store):
        """The user-visible half: a stalled 40 KB upload is dropped and the row
        must stop counting it."""
        sizes = StagedUploads(lambda: store)
        ticket = await _mint(store)
        write = await store.begin(ticket.handle, subject=SUBJECT, filename="big.jpg")
        write.feed(JPEG + b"\x00" * 40_000)
        during = await sizes.snapshot()
        assert during.bytes > 40_000

        write.abort()

        assert (await sizes.snapshot()).bytes < 40_000

    async def test_invalidate_still_forces_a_re_walk(self, store):
        """Kept for a caller that knows the volume moved by some route the store
        did not see — the clear uses it, and it costs nothing to honour."""
        sizes = StagedUploads(lambda: store)
        first = await sizes.snapshot()
        sizes.invalidate()
        assert (await sizes.snapshot()) is not first


# ── The endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        app.state.uploads = UploadService(tmp_path / "uploads")
        yield c


def _mint_sync(client):
    """A ticket minted through the real service the running app is holding.

    `client` is taken so the fixture has already swapped app.state.uploads onto
    this test's own tmp_path — minting against the shared one would put a real
    directory on whatever DATA_DIR the suite happens to have.
    """
    import asyncio

    return asyncio.run(app.state.uploads.mint(subject=SUBJECT, secret=SECRET))


def _post(client, handle, token, files, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers.update(kwargs.pop("headers", {}))
    return client.post(f"/uploads/{handle}", files=files, headers=headers, **kwargs)


class TestEndpointAuth:
    def test_a_ticket_stages_the_file_and_answers_with_its_path(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("photo.jpg", JPEG, "image/jpeg")})

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["path"].endswith(f"/{ticket.handle}/photo.jpg")
        assert body["content_type"] == "image/jpeg" and body["bytes"] == len(JPEG)
        assert pathlib.Path(body["path"]).read_bytes() == JPEG
        assert body["files"] == [
            {k: body[k] for k in ("path", "name", "bytes", "sha256", "content_type")}
        ]

    def test_no_token_is_refused(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, None, {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401
        assert not list((app.state.uploads.root / ticket.handle).glob("*.jpg"))

    def test_the_token_is_not_accepted_in_the_url(self, client):
        """tokens.py takes the URL form only because WebSocket clients cannot
        set headers. curl can, so the ticket stays out of URLs and logs."""
        ticket = _mint_sync(client)
        r = client.post(
            f"/uploads/{ticket.handle}?t={ticket.token}",
            files={"file": ("photo.jpg", JPEG, "image/jpeg")},
        )
        assert r.status_code == 401

    def test_a_ticket_for_another_handle_cannot_write_here(self, client):
        mine = _mint_sync(client)
        theirs = _mint_sync(client)
        r = _post(client, mine.handle, theirs.token,
                  {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401
        assert not list((app.state.uploads.root / mine.handle).glob("*.jpg"))

    def test_another_subjects_ticket_cannot_write_into_this_one(self, client):
        """Cross-subject at the door: a validly signed ticket for the same
        handle but a different subject is refused by the manifest."""
        import asyncio
        mine = _mint_sync(client)
        forged = uploads_service.issue_ticket(mine.handle, SECRET, subject=OTHER)
        r = _post(client, mine.handle, forged, {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 404, r.text
        assert not list((app.state.uploads.root / mine.handle).glob("*.jpg"))
        assert asyncio.run(_nothing_staged(mine.handle))

    @pytest.mark.parametrize("kind", [tokens.CDP, tokens.VNC])
    def test_a_browser_token_cannot_write_bytes(self, client, kind):
        ticket = _mint_sync(client)
        token = tokens.issue(ticket.handle, SECRET, kind=kind, subject=SUBJECT)
        r = _post(client, ticket.handle, token, {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401

    def test_a_session_cookie_buys_nothing_here(self, client):
        """The UI session is for the UI. This door takes one kind of bearer."""
        from app.services import sessions
        ticket = _mint_sync(client)
        client.cookies.set("cbs_session", sessions.issue(SECRET))
        r = client.post(
            f"/uploads/{ticket.handle}", files={"file": ("photo.jpg", JPEG, "image/jpeg")}
        )
        assert r.status_code == 401

    def test_an_oauth_access_token_is_not_an_upload_ticket(self, client):
        from conftest import mint_access
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, mint_access(app),
                  {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401

    @pytest.mark.parametrize("handle", ["..", "\x00upl_", "upl_00...", "not-a-handle"])
    def test_a_handle_that_is_not_ours_is_refused_by_us_not_by_the_router(
        self, client, handle
    ):
        """Proof the request ARRIVED: a router 404 would prove nothing about the
        guard. These reach the handler and get our own 401."""
        encoded = handle.replace(".", "%2e").replace("\x00", "%00")
        r = client.post(f"/uploads/{encoded}",
                        headers={"Authorization": "Bearer whatever"},
                        files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401, r.text
        assert r.json()["detail"] == "invalid or expired upload token"

    def test_percent_encoded_traversal_never_reaches_the_handler(self, client):
        """Stated as what it is rather than claimed as a guard: Starlette
        unquotes the path before routing, so `..%2F..%2Fetc` becomes a
        multi-segment path that `/uploads/{handle}` does not match at all. The
        handle regex is what would refuse it if it ever did — see the test above,
        which proves that path with a value that does arrive."""
        r = client.post("/uploads/..%2F..%2Fetc%2Fpasswd",
                        headers={"Authorization": "Bearer whatever"},
                        files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 404 and r.json()["detail"] == "Not Found"


async def _nothing_staged(handle: str) -> bool:
    manifest = json.loads(
        (app.state.uploads.root / handle / ".ticket.json").read_text()
    )
    return manifest["files"] == []


class TestEndpointOrigin:
    def test_a_foreign_origin_is_refused(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("photo.jpg", JPEG, "image/jpeg")},
                  headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_no_origin_is_allowed_because_that_is_every_curl(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 200

    def test_our_own_origin_is_allowed(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("photo.jpg", JPEG, "image/jpeg")},
                  headers={"Origin": "https://testserver"})
        assert r.status_code == 200


class TestEndpointBody:
    def test_a_text_file_named_photo_jpg_with_an_image_content_type_is_refused(
        self, client
    ):
        """Every claim the caller could make says image; the bytes do not."""
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("photo.jpg", NOT_AN_IMAGE, "image/jpeg")})

        assert r.status_code == 415, r.text
        left = sorted(p.name for p in (app.state.uploads.root / ticket.handle).iterdir())
        assert left == [".ticket.json"]

    def test_several_files_in_one_post_are_all_staged(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token, [
            ("file", ("a.jpg", JPEG, "image/jpeg")),
            ("file", ("b.png", PNG, "image/png")),
            ("file", ("c.pdf", PDF, "application/pdf")),
        ])

        assert r.status_code == 200, r.text
        body = r.json()
        assert [f["name"] for f in body["files"]] == ["a.jpg", "b.png", "c.pdf"]
        assert body["path"] == body["files"][0]["path"]
        for f in body["files"]:
            assert pathlib.Path(f["path"]).is_file()

    def test_the_same_bytes_posted_twice_answer_with_one_path(self, client):
        ticket = _mint_sync(client)
        first = _post(client, ticket.handle, ticket.token,
                      {"file": ("photo.jpg", JPEG, "image/jpeg")}).json()
        second = _post(client, ticket.handle, ticket.token,
                       {"file": ("retry.jpg", JPEG, "image/jpeg")}).json()
        assert second["path"] == first["path"]

    def test_a_hostile_filename_cannot_escape_the_ticket_directory(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("../../evil.jpg", JPEG, "image/jpeg")})

        assert r.status_code == 200, r.text
        path = pathlib.Path(r.json()["path"])
        assert path.parent == app.state.uploads.root / ticket.handle
        assert path.name == "evil.jpg"

    def test_a_body_with_no_file_part_says_what_to_send(self, client):
        ticket = _mint_sync(client)
        r = client.post(f"/uploads/{ticket.handle}",
                        headers={"Authorization": f"Bearer {ticket.token}"},
                        data={"note": "no file here"})
        assert r.status_code == 400
        assert "curl -F file=@" in r.json()["detail"]

    def test_a_body_that_is_not_multipart_says_what_to_send(self, client):
        ticket = _mint_sync(client)
        r = client.post(f"/uploads/{ticket.handle}",
                        headers={"Authorization": f"Bearer {ticket.token}"},
                        json={"file": "photo.jpg"})
        assert r.status_code == 400
        assert "multipart/form-data" in r.json()["detail"]

    def test_a_declared_length_over_the_ticket_cap_is_refused_before_the_body(
        self, client
    ):
        ticket = _mint_sync(client)
        r = client.post(
            f"/uploads/{ticket.handle}",
            headers={
                "Authorization": f"Bearer {ticket.token}",
                "Content-Type": "multipart/form-data; boundary=xyz",
                "Content-Length": str(uploads_service.MAX_BYTES_PER_TICKET * 4),
            },
            content=b"",
        )
        assert r.status_code == 413

    def test_the_byte_cap_trips_mid_stream_over_http(self, client, monkeypatch):
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 64)
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token,
                  {"file": ("big.jpg", JPEG + b"\x00" * 100_000, "image/jpeg")})

        assert r.status_code == 413, r.text
        left = sorted(p.name for p in (app.state.uploads.root / ticket.handle).iterdir())
        assert left == [".ticket.json"], f"partial bytes survived: {left}"

    @pytest.mark.parametrize("body,detail", [
        (b"this is not multipart at all", "well-formed multipart"),
        (b"--abc--\r\n", "no file in that request"),
    ])
    def test_a_malformed_body_is_a_400_that_says_what_to_send(self, client, body, detail):
        ticket = _mint_sync(client)
        r = client.post(
            f"/uploads/{ticket.handle}",
            headers={"Authorization": f"Bearer {ticket.token}",
                     "Content-Type": "multipart/form-data; boundary=abc"},
            content=body,
        )
        assert r.status_code == 400, r.text
        assert detail in r.json()["detail"]

    def test_an_upload_cut_off_mid_part_leaves_nothing_behind(self, client):
        """The container napping, or the agent's shell dying, halfway through.
        The part never ends, so it is never committed — and the bytes that did
        arrive go with it rather than sitting on the volume unreferenced."""
        ticket = _mint_sync(client)
        r = client.post(
            f"/uploads/{ticket.handle}",
            headers={"Authorization": f"Bearer {ticket.token}",
                     "Content-Type": "multipart/form-data; boundary=abc"},
            content=b"--abc\r\nContent-Disposition: form-data; name=\"file\"; "
                    b"filename=\"a.jpg\"\r\n\r\n" + JPEG,
        )

        assert r.status_code == 400, r.text
        left = sorted(p.name for p in (app.state.uploads.root / ticket.handle).iterdir())
        assert left == [".ticket.json"], f"a half-arrived file survived: {left}"
        assert json.loads(
            (app.state.uploads.root / ticket.handle / ".ticket.json").read_text()
        )["files"] == []

    def test_a_body_with_no_content_length_is_still_capped_mid_stream(
        self, client, monkeypatch
    ):
        """The guard for a client that will not say how much it is sending.

        `Content-Length` is a courtesy: a chunked body has none, so the
        header check cannot fire and the only thing standing between the volume
        and an endless POST is the counter in the read loop. The refusal text
        differs between the two guards precisely so this test can prove WHICH
        one fired — a 413 alone would not distinguish them.
        """
        from app.routes import uploads as upload_routes

        monkeypatch.setattr(upload_routes, "_MAX_BODY", 40_000)
        ticket = _mint_sync(client)
        head = (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; "
                b"filename=\"big.jpg\"\r\n\r\n")

        def chunked():
            yield head + JPEG
            for _ in range(20):
                yield b"\x00" * 8192
            yield b"\r\n--abc--\r\n"

        r = client.post(
            f"/uploads/{ticket.handle}",
            headers={"Authorization": f"Bearer {ticket.token}",
                     "Content-Type": "multipart/form-data; boundary=abc"},
            content=chunked(),
        )

        assert r.status_code == 413, r.text
        assert "declared no length" in r.json()["detail"], (
            "the header guard fired, not the in-stream one — this test is not "
            f"exercising what it claims: {r.json()['detail']}"
        )
        left = sorted(p.name for p in (app.state.uploads.root / ticket.handle).iterdir())
        assert left == [".ticket.json"], f"a partial body survived: {left}"

    def test_the_thirteenth_file_in_one_post_is_refused(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token, [
            ("file", (f"p{n}.jpg", JPEG + bytes([n]), "image/jpeg")) for n in range(13)
        ])
        assert r.status_code == 409, r.text


@pytest.mark.asyncio
class TestAStalledConnection:
    """The way the reservation created to hold the volume without spending.

    Send the request headers and the first part header — enough to reach `begin`
    and take a reservation — then stop writing. Measured before the fix: four
    such connections, 859 bytes between them on disk, holding 1,999,389 of a
    2,000,000 budget, and a legitimate upload got a 507.

    Driven over a real ASGI transport with an ASYNC body generator, because that
    is the only shape that reproduces it: a synchronous generator sleeping in
    the test's own thread blocks the event loop, so the timeout can never fire
    and the test passes for entirely the wrong reason.
    """

    @pytest_asyncio.fixture
    async def live(self, tmp_path, monkeypatch):
        import httpx

        from app.main import app as real_app
        from app.routes import uploads as upload_routes

        from app.services.secret import SecretService

        monkeypatch.setenv("APP_SECRET", SECRET)
        monkeypatch.setattr(upload_routes, "IDLE_TIMEOUT_SEC", 0.25)
        # No lifespan: this route sits outside the OAuth gate, so the only state
        # it reads is the store and the secret. Running the real lifespan here
        # would also start the MCP session manager's task group, which pytest's
        # fixture teardown then tries to exit from a different task than entered
        # it — an error about this test's plumbing, not about uploads.
        # setattr with raising=False: app.state is populated by the lifespan,
        # which this fixture deliberately does not run, so on a cold module
        # neither attribute exists yet. monkeypatch still restores whatever was
        # (or was not) there afterwards.
        monkeypatch.setattr(real_app.state, "secret", SecretService(), raising=False)
        store = UploadService(tmp_path / "uploads")
        monkeypatch.setattr(real_app.state, "uploads", store, raising=False)
        ticket = await store.mint(subject=SUBJECT, secret=SECRET)
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            yield client, ticket, store

    @staticmethod
    async def _stalls(pause: float):
        yield (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; "
               b"filename=\"big.jpg\"\r\n\r\n") + JPEG
        await asyncio.sleep(pause)      # …and then nothing ever comes
        yield b"\r\n--abc--\r\n"

    @staticmethod
    async def _trickles(gap: float, chunks: int):
        yield (b"--abc\r\nContent-Disposition: form-data; name=\"file\"; "
               b"filename=\"slow.jpg\"\r\n\r\n") + JPEG
        for _ in range(chunks):
            await asyncio.sleep(gap)
            yield b"\x00" * 512
        yield b"\r\n--abc--\r\n"

    def _headers(self, ticket):
        return {"Authorization": f"Bearer {ticket.token}",
                "Content-Type": "multipart/form-data; boundary=abc"}

    async def test_it_is_abandoned_and_gives_its_space_back(self, live):
        client, ticket, store = live

        r = await client.post(f"/uploads/{ticket.handle}",
                              headers=self._headers(ticket),
                              content=self._stalls(1.0))

        assert r.status_code == 408, r.text
        assert "abandoned" in r.json()["detail"]
        assert store._reserved == {}, "the abandoned upload still holds volume budget"
        left = sorted(p.name for p in (store.root / ticket.handle).iterdir())
        assert left == [".ticket.json"], f"a part-written file survived: {left}"

    async def test_the_space_it_held_is_usable_again(self, live, monkeypatch):
        """The consequence that matters. Without the timeout the hold lasts for
        the life of the process, at no cost to whoever opened it."""
        client, ticket, store = live
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 200_000)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 260_000)

        stalled = await client.post(f"/uploads/{ticket.handle}",
                                    headers=self._headers(ticket),
                                    content=self._stalls(1.0))
        assert stalled.status_code == 408, stalled.text

        ok = await client.post(f"/uploads/{ticket.handle}",
                               headers={"Authorization": f"Bearer {ticket.token}"},
                               files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert ok.status_code == 200, ok.text

    async def test_several_stalls_cannot_wedge_the_volume(self, live, monkeypatch):
        """The measured attack, scaled down: enough concurrent holds to cover
        the whole budget, then a legitimate upload."""
        client, ticket, store = live
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_FILE", 200_000)
        monkeypatch.setattr(uploads_service, "MAX_BYTES_PER_TICKET", 900_000)
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 900_000)

        stalls = [client.post(f"/uploads/{ticket.handle}", headers=self._headers(ticket),
                              content=self._stalls(1.0)) for _ in range(4)]
        assert {r.status_code for r in await asyncio.gather(*stalls)} == {408}

        assert store._reserved == {}
        ok = await client.post(f"/uploads/{ticket.handle}",
                               headers={"Authorization": f"Bearer {ticket.token}"},
                               files={"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert ok.status_code == 200, ok.text

    async def test_a_slow_but_progressing_upload_is_never_abandoned(self, live):
        """The victim this must not create. The timeout is between chunks, so a
        client that keeps sending is welcome however long it takes in total —
        here nearly five times the idle limit, spread over chunks that keep
        arriving."""
        client, ticket, _store = live

        r = await client.post(f"/uploads/{ticket.handle}",
                              headers=self._headers(ticket),
                              content=self._trickles(0.2, 6))

        assert r.status_code == 200, r.text
        assert r.json()["bytes"] == len(JPEG) + 6 * 512


class TestRouteIsOutsideTheOAuthGate:
    def test_the_upload_route_is_not_behind_the_bearer_gate(self, client):
        """The guard's docstring now lists this route; this is the behaviour
        that claim describes. Without a ticket it is 401 — but OUR 401, with no
        `WWW-Authenticate` pointing at OAuth discovery, because OAuth is not how
        a curl in an agent sandbox gets in."""
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, None, {"file": ("photo.jpg", JPEG, "image/jpeg")})
        assert r.status_code == 401
        assert "www-authenticate" not in r.headers
        assert r.json()["detail"] == "invalid or expired upload token"

    def test_the_guard_still_covers_the_tool_surface(self, client):
        """The neighbouring doors did not move."""
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/api/instances").status_code == 401
