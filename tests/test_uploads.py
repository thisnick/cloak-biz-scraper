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

import json
import pathlib
import time

import pytest
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
        ("../../.dek", "dek"),
        ("/etc/passwd", "passwd"),
        ("C:\\photos\\a.jpg", "a.jpg"),
        (".ticket.json", "ticket.json"),
        (".env", "env"),
        ("..", "upload.jpg"),
        ("", "upload.jpg"),
        ("caf\u00e9 \u2014 photo.jpg", "caf_photo.jpg"),
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
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 128)
        ticket = await _mint(store)
        await _stage(store, ticket.handle, JPEG + b"\x07" * 200)

        with pytest.raises(NoRoom) as refused:
            await _mint(store)
        assert "Settings" in str(refused.value) and "Disk space" in str(refused.value)

    async def test_the_budget_counts_only_what_is_still_there(self, store, monkeypatch):
        """Refusing to mint has to be recoverable, and the sweep inside mint is
        what recovers it — otherwise one big expired ticket wedges the feature."""
        monkeypatch.setattr(uploads_service, "UPLOADS_BUDGET_BYTES", 128)
        ticket = await _mint(store)
        await _stage(store, ticket.handle, JPEG + b"\x07" * 200)
        _rewrite_expiry(store, ticket.handle, time.time() - 1)

        fresh = await _mint(store)  # the sweep inside mint frees the space first
        assert fresh.handle != ticket.handle
        assert not (store.root / ticket.handle).exists()


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

    async def test_the_measurement_is_cached_until_invalidated(self, store):
        sizes = StagedUploads(lambda: store)
        first = await sizes.snapshot()
        ticket = await _mint(store)
        await _stage(store, ticket.handle)

        assert (await sizes.snapshot()).handles == 0, "cached, as the page expects"
        sizes.invalidate()
        assert (await sizes.snapshot()).handles == 1
        assert first.measured_at > 0


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

    def test_the_thirteenth_file_in_one_post_is_refused(self, client):
        ticket = _mint_sync(client)
        r = _post(client, ticket.handle, ticket.token, [
            ("file", (f"p{n}.jpg", JPEG + bytes([n]), "image/jpeg")) for n in range(13)
        ])
        assert r.status_code == 409, r.text


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
