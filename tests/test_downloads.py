"""Kept downloads: bytes a website chose, on this volume, behind a link.

The properties under test, in order of how much they matter: the caller never
names a path on this disk; a fetch link opens exactly one file, for the subject
it was minted for, until it expires, and nothing else stands in for it; the
file goes out as an attachment that cannot run as this origin; a download that
outgrows its reservation is cancelled while it is still arriving; and after any
`download` the browser refuses downloads, so a plain click writes nothing here.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from urllib.parse import urlsplit

import pytest
from conftest import isolate_auth, mint_access
from fastapi.testclient import TestClient

from app.main import app
from app.services import agent_browser as ab
from app.services import downloads as downloads_service
from app.services import tokens, uploads
from app.services.agent_browser import (
    AgentBrowserError,
    AgentBrowserService,
    DriveOutcome,
    _SuggestedName,
    parse_command,
)
from app.services.downloads import (
    DownloadService,
    Expired,
    NoRoom,
    NotFound,
    NothingSaved,
    TooLarge,
    issue_ticket,
    sniff,
)

SECRET = "test-secret-value-long-enough"
SUBJECT = "owner"
OTHER = "somebody-else"

PDF = b"%PDF-1.4\n" + b"\x04" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 60


def _run(coro):
    return asyncio.run(coro)


async def _keep(store: DownloadService, data: bytes, *, name: str | None = "report.pdf",
                subject: str = SUBJECT, now: float | None = None):
    landing = await store.begin(now=now)
    landing.target.write_bytes(data)
    return await store.commit(landing, subject=subject, secret=SECRET,
                              suggested_name=name, source_url="https://x.example/r.pdf",
                              now=now)


# ── The store ────────────────────────────────────────────────────────────────


class TestLanding:
    def test_a_landing_directory_is_a_dot_named_child_of_the_root(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        assert landing.dir.parent == store.root
        assert landing.dir.name.startswith(downloads_service.LANDING_PREFIX)
        assert landing.target.parent == landing.dir
        assert landing.allowance == downloads_service.MAX_BYTES_PER_FILE
        assert landing.reservation in store.busy()

    def test_a_full_budget_refuses_before_anything_is_created(self, tmp_path, monkeypatch):
        store = DownloadService(tmp_path / "downloads")
        monkeypatch.setattr(downloads_service, "DOWNLOADS_BUDGET_BYTES", 200)
        _run(_keep(store, PDF))  # the file and its manifest take the rest
        with pytest.raises(NoRoom, match="Disk space"):
            _run(store.begin())
        assert not store.busy()

    def test_the_allowance_shrinks_to_what_the_budget_has_left(self, tmp_path, monkeypatch):
        """Reserved but unwritten bytes count: two downloads in flight cannot
        both be promised the last of the budget."""
        store = DownloadService(tmp_path / "downloads")
        monkeypatch.setattr(downloads_service, "DOWNLOADS_BUDGET_BYTES", 1000)
        monkeypatch.setattr(downloads_service, "MAX_BYTES_PER_FILE", 600)
        first = _run(store.begin())
        second = _run(store.begin())
        assert (first.allowance, second.allowance) == (600, 400)
        with pytest.raises(NoRoom):
            _run(store.begin())
        store.abort(first)
        assert _run(store.begin()).allowance == 600

    def test_landed_bytes_names_the_partial_downloads_by_guid(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        (landing.dir / "0a1b-guid.crdownload").write_bytes(b"x" * 10)
        (landing.dir / "other").write_bytes(b"y" * 5)
        assert store.landed_bytes(landing) == (15, ["0a1b-guid"])


class TestCommit:
    def test_the_file_moves_into_its_own_ticket_and_the_landing_goes(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        kept = _run(_keep(store, PDF))
        ticket = store.root / kept.handle
        assert (ticket / "report.pdf").read_bytes() == PDF
        manifest = json.loads((ticket / ".ticket.json").read_text())
        assert manifest["sub"] == SUBJECT
        assert manifest["files"][0]["content_type"] == "application/pdf"
        assert manifest["expires"] == pytest.approx(time.time() + downloads_service.TTL_SEC, abs=5)
        assert [p.name for p in store.root.iterdir()] == [kept.handle]
        assert not store.busy()
        assert (kept.bytes, kept.content_type) == (len(PDF), "application/pdf")

    @pytest.mark.parametrize("suggested, expected", [
        ("../../etc/passwd", "passwd"),
        ("Q3 report (final).pdf", "Q3_report_final.pdf"),
        ("", "download.pdf"),
        (None, "download.pdf"),
        ("写真.pdf", "download.pdf"),
    ])
    def test_the_site_suggested_name_is_third_party_text(self, tmp_path, suggested, expected):
        store = DownloadService(tmp_path / "downloads")
        kept = _run(_keep(store, PDF, name=suggested))
        assert kept.name == expected
        assert (store.root / kept.handle / expected).is_file()

    def test_only_the_named_target_is_taken_never_a_stray(self, tmp_path):
        """A click can drop a GUID-named file into the folder the browser last
        downloaded to. commit takes the one file the CLI was told to write."""
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        (landing.dir / "9f1c-guid").write_bytes(b"stray")
        with pytest.raises(NothingSaved):
            _run(store.commit(landing, subject=SUBJECT, secret=SECRET))
        assert not landing.dir.exists()
        assert list(store.root.iterdir()) == []

    def test_a_link_in_place_of_the_file_is_refused(self, tmp_path):
        secret_file = tmp_path / ".dek"
        secret_file.write_bytes(b"the key")
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        landing.target.symlink_to(secret_file)
        with pytest.raises(NothingSaved):
            _run(store.commit(landing, subject=SUBJECT, secret=SECRET))
        assert secret_file.read_bytes() == b"the key"
        assert list(store.root.iterdir()) == []

    def test_a_file_over_its_allowance_is_refused_whatever_the_watchdog_missed(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(downloads_service, "MAX_BYTES_PER_FILE", 10)
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        landing.target.write_bytes(PDF)
        with pytest.raises(TooLarge, match="cancelled"):
            _run(store.commit(landing, subject=SUBJECT, secret=SECRET))
        assert list(store.root.iterdir()) == []
        assert not store.busy()

    def test_no_secret_no_link(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        landing = _run(store.begin())
        landing.target.write_bytes(PDF)
        with pytest.raises(downloads_service.DownloadsError, match="APP_SECRET"):
            _run(store.commit(landing, subject=SUBJECT, secret=None))
        assert list(store.root.iterdir()) == []


class TestSniff:
    @pytest.mark.parametrize("head, name, expected", [
        (PDF, "x.bin", "application/pdf"),
        (PNG, "x.pdf", "image/png"),
        (b"PK\x03\x04rest", "book.xlsx",
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (b"PK\x03\x04rest", "a.zip", "application/zip"),
        (b"a,b\n1,2\n", "data.csv", "text/csv"),
        (b"plain words", "notes", "text/plain"),
        (b"\x00\x01\x02binary", "a.csv", "application/octet-stream"),
        (b"<html><script>alert(1)</script>", "page.html", "text/plain"),
    ])
    def test_the_bytes_decide_and_html_is_never_named(self, head, name, expected):
        assert sniff(head, name) == expected

    def test_an_extension_cannot_make_non_zip_bytes_a_spreadsheet(self):
        assert sniff(b"\x00\x01binary", "evil.xlsx") == "application/octet-stream"


class TestOpenFor:
    """The security function: what a fetch URL may open."""

    @pytest.fixture
    def store(self, tmp_path):
        return DownloadService(tmp_path / "downloads")

    def test_the_ticket_opens_its_own_file(self, store):
        kept = _run(_keep(store, PDF))
        path, entry = store.open_for(kept.handle, kept.name, kept.token, SECRET)
        assert path.read_bytes() == PDF and entry["name"] == kept.name

    def test_a_ticket_for_one_download_does_not_open_another(self, store):
        mine = _run(_keep(store, PDF))
        theirs = _run(_keep(store, PNG, name="p.png"))
        with pytest.raises(NotFound):
            store.open_for(theirs.handle, theirs.name, mine.token, SECRET)

    def test_a_ticket_minted_for_another_subject_does_not_open_it(self, store):
        kept = _run(_keep(store, PDF))
        forged = issue_ticket(kept.handle, SECRET, subject=OTHER)
        with pytest.raises(NotFound):
            store.open_for(kept.handle, kept.name, forged, SECRET)

    def test_no_other_kind_of_token_stands_in_for_it(self, store):
        kept = _run(_keep(store, PDF))
        others = [
            tokens.issue(kept.handle, SECRET, subject=SUBJECT),
            uploads.issue_ticket(kept.handle, SECRET, subject=SUBJECT),
            None, "", "garbage",
        ]
        for token in others:
            with pytest.raises(NotFound):
                store.open_for(kept.handle, kept.name, token, SECRET)

    def test_a_ticket_signed_with_another_secret_does_not_open_it(self, store):
        kept = _run(_keep(store, PDF))
        with pytest.raises(NotFound):
            store.open_for(kept.handle, kept.name, kept.token, "a-different-secret-entirely")

    @pytest.mark.parametrize("name", ["../.ticket.json", ".ticket.json", "report.pdf/..",
                                      "../../settings.json", "other.pdf"])
    def test_only_the_manifest_name_opens(self, store, name):
        kept = _run(_keep(store, PDF))
        with pytest.raises(NotFound):
            store.open_for(kept.handle, name, kept.token, SECRET)

    def test_an_expired_download_says_so(self, store):
        kept = _run(_keep(store, PDF))
        manifest_path = store.root / kept.handle / ".ticket.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["expires"] = time.time() - 1
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(Expired, match="two hours"):
            store.open_for(kept.handle, kept.name, kept.token, SECRET)

    def test_the_ticket_dies_with_the_file(self, store):
        now = time.time()
        kept = _run(_keep(store, PDF, now=now))
        later = now + downloads_service.TTL_SEC + 1
        with pytest.raises(NotFound):
            store.open_for(kept.handle, kept.name, kept.token, SECRET, now=later)


class TestSweep:
    def test_expired_go_live_stay_and_abandoned_landings_go(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        dead = _run(_keep(store, PDF))
        live = _run(_keep(store, PNG, name="p.png"))
        in_flight = _run(store.begin())
        # Expired only now: `begin` sweeps too, and would have taken it already.
        manifest_path = store.root / dead.handle / ".ticket.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["expires"] = time.time() - 1
        manifest_path.write_text(json.dumps(manifest))
        abandoned = store.root / f"{downloads_service.LANDING_PREFIX}deadbeefdeadbeef"
        abandoned.mkdir()
        (abandoned / "0a-guid").write_bytes(b"stray bytes from a click")
        old = time.time() - 120
        os.utime(abandoned, (old, old))
        just_made = store.root / f"{downloads_service.LANDING_PREFIX}0000000000000000"
        just_made.mkdir()

        swept = _run(store.sweep())

        assert not (store.root / dead.handle).exists()
        assert (store.root / live.handle).is_dir()
        assert in_flight.dir.is_dir(), "a download still arriving must not be swept"
        assert not abandoned.exists()
        assert just_made.is_dir(), (
            "an unreserved landing this young may be a `begin` that has not recorded "
            "its reservation yet")
        assert swept.handles == 1

    def test_at_startup_every_landing_goes(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        store.root.mkdir(parents=True)
        fresh = store.root / f"{downloads_service.LANDING_PREFIX}0000000000000000"
        fresh.mkdir()
        (fresh / "download").write_bytes(b"half a file from before the restart")
        _run(store.sweep(at_startup=True))
        assert list(store.root.iterdir()) == []

    def test_the_full_clear_still_leaves_a_download_in_flight(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        live = _run(_keep(store, PDF))
        in_flight = _run(store.begin())
        cleared = _run(store.clear(expired_only=False))
        assert not (store.root / live.handle).exists()
        assert in_flight.dir.is_dir()
        assert (cleared.handles, cleared.kept) == (1, 1)

    def test_measured_by_the_uploads_view_without_counting_landings_as_files(self, tmp_path):
        store = DownloadService(tmp_path / "downloads")
        _run(_keep(store, PDF))
        landing = _run(store.begin())
        landing.target.write_bytes(b"x" * 1000)
        view = _run(uploads.StagedUploads(lambda: store).snapshot())
        assert (view.handles, view.files, view.expired) == (1, 1, 0)
        assert view.bytes >= len(PDF) + 1000


# ── The verb ─────────────────────────────────────────────────────────────────


class TestTheVerbIsParsed:
    def test_one_selector_is_accepted(self):
        assert parse_command("download @e5") == ["download", "@e5"]
        assert parse_command('download "a[href$=\'.pdf\']"') == ["download", "a[href$='.pdf']"]

    @pytest.mark.parametrize("cmd", [
        "download", "download @e5 /data/.dek", "download @e5 ./x.pdf",
        "download @e5 --cdp 9222", "download --path /tmp/x @e5",
    ])
    def test_a_path_or_a_flag_never_gets_through(self, cmd):
        with pytest.raises(AgentBrowserError):
            parse_command(cmd)


class _Inst:
    def __init__(self, context=None):
        self.id = "i1"
        self.origin = "interactive"
        self.subject = None
        self.cdp_port = 9999
        self.context = context


class _Instances:
    def __init__(self, inst):
        self._inst = inst

    def get(self, iid):
        return self._inst if iid == self._inst.id else None


def _fake_cli(tmp_path, body: str) -> pathlib.Path:
    """A stand-in `agent-browser`. `$last` is the path the service chose."""
    script = tmp_path / "fake-ab.sh"
    script.write_text("#!/bin/sh\nlast=\"\"\nfor a in \"$@\"; do last=\"$a\"; done\n" + body)
    script.chmod(0o755)
    return script


class TestDownloadVerb:
    @pytest.fixture
    def rig(self, tmp_path, monkeypatch):
        store = DownloadService(tmp_path / "downloads")
        svc = AgentBrowserService(_Instances(_Inst()), None, store, secret=lambda: SECRET)
        calls: list[tuple[str, dict]] = []

        async def browser_command(port, method, params):
            calls.append((method, params))
            return True

        monkeypatch.setattr(svc, "_browser_command", browser_command)
        return svc, store, calls

    def _use(self, monkeypatch, tmp_path, body):
        monkeypatch.setenv("AGENT_BROWSER_BIN", str(_fake_cli(tmp_path, body)))

    @pytest.mark.asyncio
    async def test_a_download_is_kept_and_the_browser_then_refuses_downloads(
            self, rig, tmp_path, monkeypatch):
        svc, store, calls = rig
        self._use(monkeypatch, tmp_path,
                  'case " $* " in *" download "*) printf "%%PDF-1.7 body" > "$last" ;; esac\n'
                  'echo "saved $last"\n')
        out = await svc.drive("i1", "download @e5")
        assert out.ok, out.output
        assert out.download is not None and out.download.content_type == "application/pdf"
        assert (store.root / out.download.handle / out.download.name).read_bytes() == \
            b"%PDF-1.7 body"
        assert calls[-1] == ("Browser.setDownloadBehavior", {"behavior": "deny"})
        assert [p.name for p in store.root.iterdir()] == [out.download.handle]

    @pytest.mark.asyncio
    async def test_the_path_handed_to_the_cli_is_ours(self, rig, tmp_path, monkeypatch):
        svc, store, _calls = rig
        argv_file = tmp_path / "argv"
        self._use(monkeypatch, tmp_path,
                  f'echo "$@" > {argv_file}\nprintf data > "$last"\n')
        out = await svc.drive("i1", "download @e5")
        argv = argv_file.read_text().split()
        assert argv[-3:-1] == ["download", "@e5"]
        landed = pathlib.Path(argv[-1])
        assert landed.parent.parent == store.root
        assert landed.parent.name.startswith(downloads_service.LANDING_PREFIX)
        assert out.ok and out.download.name == "download.txt"

    @pytest.mark.asyncio
    async def test_a_failed_download_leaves_nothing_and_still_denies(
            self, rig, tmp_path, monkeypatch):
        svc, store, calls = rig
        self._use(monkeypatch, tmp_path, 'echo "Element not found: @e5" >&2\nexit 1\n')
        out = await svc.drive("i1", "download @e5")
        assert not out.ok and "Element not found" in out.output
        assert out.download is None
        assert list(store.root.iterdir()) == []
        assert not store.busy()
        assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in calls

    @pytest.mark.asyncio
    async def test_an_oversized_download_is_cancelled_while_it_arrives(
            self, rig, tmp_path, monkeypatch):
        svc, store, calls = rig
        monkeypatch.setattr(downloads_service, "MAX_BYTES_PER_FILE", 1024)
        monkeypatch.setattr(ab, "_WATCH_INTERVAL", 0.05)
        # Grows a .crdownload the way Chromium does, and would take a minute.
        self._use(monkeypatch, tmp_path,
                  'dir=$(dirname "$last")\n'
                  'i=0; while [ $i -lt 600 ]; do\n'
                  '  head -c 512 /dev/zero >> "$dir/5f2a-guid.crdownload"; i=$((i+1)); sleep 0.1\n'
                  'done\n')
        started = time.monotonic()
        out = await svc.drive("i1", "download @e5")
        assert time.monotonic() - started < 10, "the watchdog must cut it, not wait it out"
        assert not out.ok and "bigger than" in out.output
        assert ("Browser.cancelDownload", {"guid": "5f2a-guid"}) in calls
        assert calls[-1] == ("Browser.setDownloadBehavior", {"behavior": "deny"})
        await asyncio.sleep(0.3)
        assert list(store.root.iterdir()) == [] or all(
            not p.name.startswith("dl_") for p in store.root.iterdir())
        assert not store.busy()

    @pytest.mark.asyncio
    async def test_a_download_that_never_finishes_times_out_cleanly(
            self, rig, tmp_path, monkeypatch):
        svc, store, calls = rig
        monkeypatch.setattr(ab, "_DOWNLOAD_TIMEOUT", 0.5)
        monkeypatch.setattr(ab, "_WATCH_INTERVAL", 0.05)
        self._use(monkeypatch, tmp_path, "exec sleep 30\n")
        out = await svc.drive("i1", "download @e5")
        assert not out.ok and "did not finish" in out.output
        assert not store.busy()
        assert not any(p.name.startswith(downloads_service.LANDING_PREFIX)
                       for p in store.root.iterdir())
        assert calls[-1] == ("Browser.setDownloadBehavior", {"behavior": "deny"})

    @pytest.mark.asyncio
    async def test_a_full_volume_refuses_without_running_anything(
            self, rig, tmp_path, monkeypatch):
        svc, store, calls = rig
        monkeypatch.setattr(downloads_service, "DOWNLOADS_BUDGET_BYTES", 0)
        ran = tmp_path / "ran"
        self._use(monkeypatch, tmp_path, f"touch {ran}\n")
        out = await svc.drive("i1", "download @e5")
        assert not out.ok and "Disk space" in out.output
        assert not ran.exists() and calls == []

    @pytest.mark.asyncio
    async def test_a_server_without_a_store_refuses_the_verb(self, tmp_path, monkeypatch):
        svc = AgentBrowserService(_Instances(_Inst()))
        with pytest.raises(AgentBrowserError, match="cannot keep downloads"):
            await svc.drive("i1", "download @e5")


class TestSuggestedName:
    class _Page:
        def __init__(self):
            self.handlers: dict[str, list] = {}

        def on(self, event, fn):
            self.handlers.setdefault(event, []).append(fn)

        def remove_listener(self, event, fn):
            self.handlers[event].remove(fn)

    class _Context(_Page):
        def __init__(self, pages):
            super().__init__()
            self.pages = pages

    class _Download:
        suggested_filename = "Q3 report.pdf"
        url = "https://x.example/q3.pdf"

    def test_the_first_download_event_names_the_file_and_listeners_come_off(self):
        page = self._Page()
        context = self._Context([page])
        names = _SuggestedName(context)
        page.handlers["download"][0](self._Download())
        later = self._Download()
        later.suggested_filename = "something-else.pdf"
        page.handlers["download"][0](later)
        assert (names.name, names.url) == ("Q3 report.pdf", "https://x.example/q3.pdf")
        names.stop()
        assert page.handlers["download"] == [] and context.handlers["page"] == []

    def test_no_context_is_simply_no_name(self):
        names = _SuggestedName(None)
        names.stop()
        assert names.name is None and names.url == ""


# ── The HTTP doors ───────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        monkeypatch.setattr(app.state, "downloads", DownloadService(tmp_path / "downloads"))
        yield c


def _kept(client, data=PDF, name="report.pdf"):
    return _run(_keep(app.state.downloads, data, name=name))


class TestFetch:
    def test_the_link_fetches_the_file_as_an_attachment(self, client):
        kept = _kept(client)
        r = client.get(f"/downloads/{kept.handle}/{kept.name}", params={"t": kept.token})
        assert r.status_code == 200 and r.content == PDF
        assert r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == 'attachment; filename="report.pdf"'
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"].startswith("sandbox")
        assert r.headers["cache-control"] == "no-store"

    def test_a_bearer_header_works_as_well(self, client):
        kept = _kept(client)
        r = client.get(f"/downloads/{kept.handle}/{kept.name}",
                       headers={"Authorization": f"Bearer {kept.token}"})
        assert r.status_code == 200 and r.content == PDF

    def test_nothing_else_opens_it(self, client):
        kept = _kept(client)
        other = _kept(client, PNG, "p.png")
        url = f"/downloads/{kept.handle}/{kept.name}"
        assert client.get(url).status_code == 404
        assert client.get(url, params={"t": other.token}).status_code == 404
        # The OAuth access token is the dashboard's key to everything else, and
        # still not a key to this.
        assert client.get(url, headers={"Authorization": f"Bearer {mint_access(app)}"}
                          ).status_code == 404

    def test_an_expired_download_is_a_410(self, client):
        kept = _kept(client)
        manifest_path = app.state.downloads.root / kept.handle / ".ticket.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["expires"] = time.time() - 1
        manifest_path.write_text(json.dumps(manifest))
        r = client.get(f"/downloads/{kept.handle}/{kept.name}", params={"t": kept.token})
        assert r.status_code == 410 and "two hours" in r.json()["detail"]

    def test_the_query_token_is_redacted_from_logs(self):
        from app.services.log_safety import redact_log_text

        # Uvicorn's access log carries the bare path, no scheme or host.
        path = "/downloads/dl_0123456789abcdef/report.pdf?t=eyJzecret.sig"
        line = redact_log_text(f'127.0.0.1:5000 - "GET {path} HTTP/1.1" 200')
        assert "eyJzecret" not in line
        assert "/downloads/dl_0123456789abcdef/report.pdf" in line


def _outcome(kept):
    return DriveOutcome("i1", "download @e5", True, "downloaded", None, download=kept)


class TestFacades:
    def test_rest_returns_the_link_and_the_curl(self, client, monkeypatch):
        kept = _kept(client)

        async def fake_drive(instance_id, command, *, subject=tokens.OWNER):
            return _outcome(kept)

        monkeypatch.setattr(app.state.agent_browser, "drive", fake_drive)
        r = client.post("/api/instances/i1/agent-browser", json={"command": "download @e5"},
                        headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 200, r.text
        body = r.json()
        link = body["download"]["url"]
        assert link.startswith(f"https://testserver/downloads/{kept.handle}/report.pdf?t=")
        assert link in body["output"] and "give the link to the user" in body["output"]
        assert body["download"]["curl"].startswith("curl -fsS -o report.pdf ")
        parts = urlsplit(link)
        assert client.get(f"{parts.path}?{parts.query}").content == PDF

    def _mcp(self, client, command):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "agent_browser",
                             "arguments": {"instance_id": "i1", "command": command}}},
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Authorization": f"Bearer {mint_access(app)}"},
        )
        assert r.status_code == 200, r.text
        return r.json()["result"]["content"]

    def test_mcp_returns_the_link_and_a_small_image_inline(self, client, monkeypatch):
        kept = _kept(client, PNG, "photo.png")

        async def fake_drive(instance_id, command, *, subject=tokens.OWNER):
            return _outcome(kept)

        monkeypatch.setattr(app.state.agent_browser, "drive", fake_drive)
        blocks = self._mcp(client, "download @e5")
        assert blocks[0]["type"] == "text" and "/downloads/" in blocks[0]["text"]
        assert blocks[1]["type"] == "image" and blocks[1]["mimeType"] == "image/png"

    def test_mcp_never_inlines_anything_but_a_small_image(self, client, monkeypatch):
        kept = _kept(client)

        async def fake_drive(instance_id, command, *, subject=tokens.OWNER):
            return _outcome(kept)

        monkeypatch.setattr(app.state.agent_browser, "drive", fake_drive)
        blocks = self._mcp(client, "download @e5")
        assert [b["type"] for b in blocks] == ["text"]

    def test_a_host_that_is_not_a_hostname_gets_no_link(self):
        from app.services.views import downloaded_file

        kept = _run(_keep(DownloadService(pathlib.Path(os.environ["DATA_DIR"]) / "dl-host"),
                          PDF))
        with pytest.raises(downloads_service.DownloadsError, match="own address"):
            downloaded_file(kept, base_url="https://evil.example$(id)")
