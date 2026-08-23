"""The agent_browser passthrough — and the one thing that must not break.

This shells out on a command an LLM wrote, so the security surface is the
shell-out. The tests below prove the command is tokenised (not shell-parsed),
allow-listed by verb, argv-passed, and bound to the caller's own non-sweep
browser. The injection test uses a harmless stand-in binary and a canary file:
if the metacharacters were ever handed to a shell, the canary would be created.
"""
from __future__ import annotations

import pathlib

import pytest
from conftest import isolate_auth, mint_access
from fastapi.testclient import TestClient

from app.main import app
from app.services.agent_browser import (
    AgentBrowserError,
    AgentBrowserService,
    InstanceNotDrivable,
    parse_command,
)
from app.services.tokens import OWNER

SECRET = "test-secret-value-long-enough"


class _FakeInst:
    def __init__(self, iid="i1", origin="interactive", subject=None, cdp_port=9999):
        self.id = iid
        self.origin = origin
        self.subject = subject
        self.cdp_port = cdp_port


class _FakeInstances:
    def __init__(self, inst):
        self._inst = inst

    def get(self, iid):
        return self._inst if (self._inst and iid == self._inst.id) else None


# ── the allow-list / parser ───────────────────────────────────────────────────
class TestParseCommand:
    def test_allows_a_whitelisted_verb(self):
        assert parse_command("navigate https://example.com") == ["navigate", "https://example.com"]

    def test_keeps_quoted_arguments_together(self):
        assert parse_command("fill @e3 'hello world'") == ["fill", "@e3", "hello world"]

    def test_rejects_a_verb_not_on_the_list(self):
        for bad in ("rm -rf /", "state save x", "mcp", "command.run echo", "install"):
            with pytest.raises(AgentBrowserError):
                parse_command(bad)

    def test_rejects_empty(self):
        with pytest.raises(AgentBrowserError):
            parse_command("   ")

    def test_rejects_unbalanced_quotes(self):
        with pytest.raises(AgentBrowserError):
            parse_command("fill @e1 'unterminated")

    def test_screenshot_is_not_an_input_verb(self):
        """`screenshot` writes a file to a caller-named path; the service takes its
        own screenshot to a path it controls, so the verb is not offered."""
        with pytest.raises(AgentBrowserError):
            parse_command("screenshot /etc/passwd")

    def test_shell_metacharacters_survive_as_literal_tokens(self):
        # Non-option metacharacters are inert tokens, never operators. (Tokens
        # starting with "-" are refused separately; see TestOptionInjection.)
        assert parse_command("navigate a; touch b") == ["navigate", "a;", "touch", "b"]
        assert parse_command("navigate $(touch x)") == ["navigate", "$(touch", "x)"]


# ── the shell-out is exec, not a shell — the crux ─────────────────────────────
class TestInjectionIsInert:
    @pytest.mark.asyncio
    async def test_metacharacters_do_not_execute(self, tmp_path, monkeypatch):
        """Point the binary at /bin/echo (harmless) and try to smuggle a command
        via `;`, `$()`, and `&&`. If any reached a shell, the canary would exist.
        With argv exec, echo just prints them and nothing runs."""
        monkeypatch.setenv("AGENT_BROWSER_BIN", "/bin/echo")
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))
        canary = tmp_path / "pwned"
        for payload in (
            f"navigate https://x ; touch {canary}",
            f"navigate $(touch {canary})",
            f"navigate x && touch {canary}",
            f"navigate `touch {canary}`",
        ):
            out = await svc.drive("i1", payload, subject=OWNER)
            assert not canary.exists(), f"a shell executed the payload: {payload!r}"
            # The verb still ran (echo returned 0); it just did nothing dangerous.
            assert out.instance_id == "i1"

    @pytest.mark.asyncio
    async def test_it_uses_exec_not_a_shell(self, monkeypatch):
        """Belt to the canary's suspenders: prove create_subprocess_exec is the
        call, with the metacharacters as separate argv items — and that no
        create_subprocess_shell path exists."""
        import asyncio

        calls = []

        async def spy_exec(program, *args, **kwargs):
            calls.append((program, args))

            class _P:
                returncode = 0

                async def communicate(self):
                    return (b"ok", b"")

            return _P()

        def forbidden_shell(*a, **k):  # noqa: ANN001
            raise AssertionError("create_subprocess_shell must never be used")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
        monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_shell)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=4242)))
        await svc.drive("i1", "navigate a; touch b", subject=OWNER)
        program, args = calls[0]  # the single command (no auto-screenshot)
        assert program == "agent-browser"
        # --cdp <port> then the tokens, each its own argv item (";" is glued to "a")
        assert args[:3] == ("--cdp", "4242", "navigate")
        # The metacharacters are literal argv items, passed to agent-browser as
        # arguments — never a shell operator.
        assert "a;" in args and "touch" in args and "b" in args


# ── option-injection: a smuggled --cdp must not redirect to another browser ───
class TestOptionInjection:
    """The refutation the Reviewer found: agent-browser parses --cdp (and other
    global options) from anywhere in the argv, so a verb-only allow-list lets
    `navigate --cdp <otherport>` drive a DIFFERENT instance — a cross-instance
    scoping bypass around the subject-bound port. The per-verb flag whitelist
    refuses every form."""

    @pytest.mark.parametrize("payload", [
        "navigate --cdp 59999 http://x",            # leading
        "navigate http://x --cdp 59999",            # trailing — position-independent
        "navigate --cdp=59999 http://x",            # the = form
        "navigate --proxy http://evil:1 http://x",  # any global option, not just --cdp
        "navigate --executable-path /bin/sh",
        "navigate --init-script /tmp/x.js http://x",
        "snapshot --cdp 59999",                     # even the one verb that takes flags
        "read --cdp 59999",
        "click @e1 --cdp 59999",
        "fill @e2 x --cdp 59999",
    ])
    def test_smuggled_options_are_refused(self, payload):
        with pytest.raises(AgentBrowserError):
            parse_command(payload)

    def test_combined_short_flags_are_refused(self):
        # Exact-match whitelist: combined shorts must be split (-i -c), so a
        # smuggled character can't ride in on a combined token.
        with pytest.raises(AgentBrowserError):
            parse_command("snapshot -ic")

    def test_the_allowed_snapshot_flags_still_work(self):
        assert parse_command("snapshot -i") == ["snapshot", "-i"]
        assert parse_command("snapshot -i -u") == ["snapshot", "-i", "-u"]
        assert parse_command("snapshot -c -d 3") == ["snapshot", "-c", "-d", "3"]
        assert parse_command("snapshot -s #main") == ["snapshot", "-s", "#main"]
        assert parse_command("snapshot --json") == ["snapshot", "--json"]

    def test_ordinary_positional_arguments_are_fine(self):
        assert parse_command("navigate https://example.com") == ["navigate", "https://example.com"]
        assert parse_command("get attr @e1 href") == ["get", "attr", "@e1", "href"]
        assert parse_command("click @e3") == ["click", "@e3"]

    @pytest.mark.asyncio
    async def test_the_redirect_is_refused_before_any_subprocess_runs(self, monkeypatch):
        """End-to-end: driving instance A with a --cdp for B's port must raise
        before agent-browser is ever spawned, so B is never touched."""
        import asyncio

        ran = []

        async def spy_exec(*a, **k):
            ran.append(a)

            class _P:
                returncode = 0

                async def communicate(self):
                    return (b"", b"")

            return _P()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(iid="A", cdp_port=1111)))
        with pytest.raises(AgentBrowserError):
            await svc.drive("A", "navigate --cdp 2222 http://x", subject=OWNER)
        assert ran == [], "a subprocess ran despite the smuggled --cdp"


# ── screenshots are opt-in, and the path stays ours ───────────────────────────
class TestScreenshotIsOptIn:
    """A screenshot is tens of thousands of tokens the user pays for, so it comes
    back ONLY for the explicit `screenshot` verb — never auto-attached to a
    read/get/snapshot. And `screenshot` is service-handled: the caller picks the
    geometry, the service picks the output path, so agent-browser's file-writing
    `screenshot <path>` is never reachable with caller input."""

    # A stand-in binary: if 'screenshot' is among the args it writes bytes to the
    # last arg (the path), else it just echoes — so both paths are exercised
    # without a real browser.
    FAKE = (
        "#!/bin/sh\n"
        'last=""\n'
        'for a in "$@"; do last="$a"; done\n'
        'case " $* " in\n'
        '  *" screenshot "*) printf FAKEPNG > "$last"; echo "saved $last" ;;\n'
        '  *) echo "ran: $*" ;;\n'
        "esac\n"
    )

    @pytest.fixture
    def svc(self, tmp_path, monkeypatch):
        script = tmp_path / "fake-ab.sh"
        script.write_text(self.FAKE)
        script.chmod(0o755)
        monkeypatch.setenv("AGENT_BROWSER_BIN", str(script))
        return AgentBrowserService(_FakeInstances(_FakeInst()))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["navigate https://x", "read", "get url", "snapshot -i"])
    async def test_ordinary_commands_return_no_screenshot(self, svc, cmd):
        out = await svc.drive("i1", cmd, subject=OWNER)
        assert out.screenshot is None, f"{cmd!r} returned a screenshot nobody asked for"

    @pytest.mark.asyncio
    async def test_the_screenshot_verb_returns_an_image(self, svc):
        out = await svc.drive("i1", "screenshot", subject=OWNER)
        assert out.ok and out.screenshot == b"FAKEPNG"

    @pytest.mark.asyncio
    async def test_full_and_annotate_flags_are_allowed(self, svc):
        assert (await svc.drive("i1", "screenshot --full", subject=OWNER)).screenshot == b"FAKEPNG"
        assert (await svc.drive("i1", "screenshot --annotate", subject=OWNER)).screenshot == b"FAKEPNG"

    def test_a_caller_supplied_path_or_element_is_refused(self):
        for bad in ("screenshot /etc/passwd", "screenshot ../../x.png",
                    "screenshot @e3", "screenshot shot.png --full"):
            with pytest.raises(AgentBrowserError):
                parse_command(bad)

    def test_an_unwhitelisted_screenshot_flag_is_refused(self):
        for bad in ("screenshot --output /etc/x", "screenshot --path /etc/x", "screenshot --cdp 5"):
            with pytest.raises(AgentBrowserError):
                parse_command(bad)

    @pytest.mark.asyncio
    async def test_the_service_owns_the_output_path(self, monkeypatch):
        """The exec argv for a screenshot ends in a temp path the SERVICE created;
        no caller string reaches it. This is the guard that reverting the
        interception would break."""
        import asyncio
        import pathlib

        calls = []

        async def spy_exec(program, *args, **kw):
            calls.append(args)
            pathlib.Path(args[-1]).write_bytes(b"PNG")  # emulate the capture

            class _P:
                returncode = 0

                async def communicate(self):
                    return (b"", b"")

            return _P()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=7777)))
        out = await svc.drive("i1", "screenshot --full", subject=OWNER)
        assert out.screenshot == b"PNG"
        args = calls[0]
        assert args[:3] == ("--cdp", "7777", "screenshot")
        assert "--full" in args
        # last arg is the path, under a temp dir the service made — not caller input
        assert args[-1].endswith("shot.png") and "ab-shot-" in args[-1]


# ── the same guards CDP carries ───────────────────────────────────────────────
class TestDrivingIsGuarded:
    @pytest.mark.asyncio
    async def test_unknown_instance_is_refused(self):
        svc = AgentBrowserService(_FakeInstances(None))
        with pytest.raises(InstanceNotDrivable):
            await svc.drive("nope", "snapshot", subject=OWNER)

    @pytest.mark.asyncio
    async def test_a_sweeps_browser_cannot_be_driven(self):
        svc = AgentBrowserService(_FakeInstances(_FakeInst(origin="task")))
        with pytest.raises(InstanceNotDrivable):
            await svc.drive("i1", "snapshot", subject=OWNER)

    @pytest.mark.asyncio
    async def test_another_subjects_browser_is_refused(self):
        svc = AgentBrowserService(_FakeInstances(_FakeInst(subject="alice")))
        with pytest.raises(InstanceNotDrivable):
            await svc.drive("i1", "snapshot", subject="mallory")

    @pytest.mark.asyncio
    async def test_a_bad_command_is_refused_before_the_instance_is_touched(self):
        # parse happens first: a disallowed verb fails even for a missing instance.
        svc = AgentBrowserService(_FakeInstances(None))
        with pytest.raises(AgentBrowserError):
            await svc.drive("nope", "rm -rf /", subject=OWNER)


# ── the first-call readiness race: warm on create + a scoped internal retry ───
def _fake_exec_sequence(results):
    """A create_subprocess_exec spy that yields queued (rc, stdout, stderr) in
    order. Returns (spy, calls) where calls records (program, args) per run."""
    calls = []
    seq = iter(results)

    async def spy(program, *args, **kwargs):
        calls.append((program, args))
        rc, out, err = next(seq)

        class _P:
            returncode = rc

            async def communicate(self):
                return (out, err)

            def kill(self):
                pass

            async def wait(self):
                return 0

        return _P()

    return spy, calls


# The exact stderr the agent-browser CLI prints when its daemon's first CDP
# attach loses the race — the signature we retry on (browser.ts / main.rs).
_CDP_RACE_STDERR = (
    b"\xe2\x9c\x97 Failed to connect via CDP on port 9999. "
    b"Make sure the app is running with --remote-debugging-port=9999"
)


class TestFirstCallRetry:
    """The first agent_browser call right after create_instance can lose the
    agent-browser daemon/CDP cold-start race. That transient failure is retried
    internally so the caller never sees it; a genuine command error is not."""

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        import app.services.agent_browser as ab
        monkeypatch.setattr(ab, "_RETRY_BACKOFF", (0.0, 0.0))

    @pytest.mark.asyncio
    async def test_a_transient_first_call_is_retried_until_success(self, monkeypatch):
        import asyncio
        spy, calls = _fake_exec_sequence([
            (1, b"", _CDP_RACE_STDERR),          # cold daemon loses the race
            (0, b"https://example.com", b""),    # warm daemon, a beat later
        ])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=9999)))

        out = await svc.drive("i1", "get url", subject=OWNER)

        assert out.ok is True
        assert out.output == "https://example.com"
        assert "Failed to connect via CDP" not in out.output, "the caller saw the race"
        assert len(calls) == 2, "the transient first call was not retried"

    @pytest.mark.asyncio
    async def test_a_genuine_command_error_is_surfaced_not_retried(self, monkeypatch):
        import asyncio
        # A real navigation failure: non-zero, but NOT a daemon/CDP race message.
        spy, calls = _fake_exec_sequence([
            (1, b"", b"net::ERR_NAME_NOT_RESOLVED at https://nope.invalid"),
        ])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        out = await svc.drive("i1", "navigate https://nope.invalid", subject=OWNER)

        assert out.ok is False
        assert "ERR_NAME_NOT_RESOLVED" in out.output
        assert len(calls) == 1, "a real command error must not be retried"

    @pytest.mark.asyncio
    async def test_a_dead_instance_fails_fast_after_a_bounded_number_of_retries(self, monkeypatch):
        import asyncio
        import app.services.agent_browser as ab
        # Every attempt is the transient signature — a daemon that can never
        # attach. It must stop after the bound and surface its own message.
        spy, calls = _fake_exec_sequence([(1, b"", _CDP_RACE_STDERR)] * 20)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=9999)))

        out = await svc.drive("i1", "get url", subject=OWNER)

        assert out.ok is False
        assert "Failed to connect via CDP" in out.output
        assert len(calls) == ab._RETRY_ATTEMPTS, "retries were not bounded"

    @pytest.mark.asyncio
    async def test_a_timeout_is_not_retried(self, monkeypatch):
        import asyncio
        runs = []

        async def slow_exec(program, *args, **kwargs):
            runs.append(args)

            class _P:
                returncode = None

                async def communicate(self):
                    await asyncio.sleep(10)  # forced past the wait_for timeout

                def kill(self):
                    pass

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_exec)
        monkeypatch.setattr("app.services.agent_browser._RUN_TIMEOUT", 0.05)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        out = await svc.drive("i1", "get url", subject=OWNER)

        assert out.ok is False
        assert "did not respond" in out.output
        assert len(runs) == 1, "a timeout must fail fast, not retry"

    @pytest.mark.asyncio
    async def test_a_real_error_whose_text_contains_connection_refused_is_not_retried(self, monkeypatch):
        """The markers must be CLI-emitted prefixes, not bare phrases that ride in
        on echoed caller input. A missing element whose selector literally says
        'Connection refused' is a genuine, permanent error — and `click` mutates,
        so a wrong retry would re-execute the action. It must run exactly once."""
        import asyncio
        spy, calls = _fake_exec_sequence([
            (1, b"", b"Element not found: text=Connection refused"),
        ])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        out = await svc.drive("i1", 'click "text=Connection refused"', subject=OWNER)

        assert out.ok is False
        assert "Element not found" in out.output
        assert len(calls) == 1, "a real error echoing 'Connection refused' was retried"

    @pytest.mark.asyncio
    async def test_a_dns_failure_on_a_url_containing_econnrefused_is_not_retried(self, monkeypatch):
        """A permanent DNS failure whose echoed URL contains 'econnrefused' must
        not be mistaken for the socket-level ECONNREFUSED transient."""
        import asyncio
        spy, calls = _fake_exec_sequence([
            (1, b"", b"net::ERR_NAME_NOT_RESOLVED at https://econnrefused.example.com/"),
        ])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        out = await svc.drive("i1", "navigate https://econnrefused.example.com/", subject=OWNER)

        assert out.ok is False
        assert "ERR_NAME_NOT_RESOLVED" in out.output
        assert len(calls) == 1, "a real DNS error echoing 'econnrefused' was retried"

    @pytest.mark.asyncio
    async def test_the_real_daemon_socket_refusal_is_still_retried(self, monkeypatch):
        """The genuine Unix/Docker socket refusal — 'Failed to connect: Connection
        refused (os error 111)' — must STILL be caught, via the 'failed to
        connect:' prefix, after dropping the two bare markers."""
        import asyncio
        spy, calls = _fake_exec_sequence([
            (1, b"", b"\xe2\x9c\x97 Failed to connect: Connection refused (os error 111)"),
            (0, b"https://example.com", b""),
        ])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        out = await svc.drive("i1", "get url", subject=OWNER)

        assert out.ok is True
        assert out.output == "https://example.com"
        assert len(calls) == 2, "the real socket-refusal transient was not retried"


class TestWarmOnCreate:
    """create_instance warms the daemon so the first command doesn't race it."""

    @pytest.mark.asyncio
    async def test_warm_runs_one_cheap_read_only_command(self, monkeypatch):
        import asyncio
        spy, calls = _fake_exec_sequence([(0, b"about:blank", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=5555)))

        await svc.warm(5555)

        assert len(calls) == 1
        program, args = calls[0]
        assert program == "agent-browser"
        assert args[:3] == ("--cdp", "5555", "get") and "url" in args

    @pytest.mark.asyncio
    async def test_warm_swallows_every_failure(self, monkeypatch):
        import asyncio

        async def boom(*a, **k):
            raise FileNotFoundError("agent-browser not installed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))

        await svc.warm(1234)  # must not raise

    def test_the_service_registers_its_warm_hook_when_the_pool_supports_it(self):
        class _Pool:
            def __init__(self):
                self.hook = None

            def get(self, iid):
                return None

            def set_launch_warm_hook(self, fn):
                self.hook = fn

        pool = _Pool()
        svc = AgentBrowserService(pool)
        assert pool.hook == svc.warm

    def test_a_pool_without_the_hook_setter_is_fine(self):
        # The unit-test double has no setter; construction must not blow up.
        AgentBrowserService(_FakeInstances(_FakeInst()))


# ── the REST mirror + its auth ────────────────────────────────────────────────
class TestRestEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        with TestClient(app, base_url="https://testserver") as c:
            isolate_auth(app, tmp_path)
            yield c

    def _stub(self, monkeypatch, outcome=None, raises=None):
        from app.services.agent_browser import DriveOutcome

        async def fake_drive(instance_id, command, *, subject=OWNER):
            if raises is not None:
                raise raises
            return outcome or DriveOutcome("i1", command, True, "@e1 [heading]", b"\x89PNG-bytes")

        monkeypatch.setattr(app.state.agent_browser, "drive", fake_drive)

    def test_authed_drive_returns_output_and_a_base64_screenshot(self, client, monkeypatch):
        self._stub(monkeypatch)
        r = client.post("/api/instances/i1/agent-browser",
                        json={"command": "snapshot -i"},
                        headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["output"] == "@e1 [heading]"
        import base64
        assert base64.b64decode(body["screenshot_png_base64"]) == b"\x89PNG-bytes"

    def test_a_bad_command_is_a_400(self, client, monkeypatch):
        self._stub(monkeypatch, raises=AgentBrowserError("'rm' is not an allowed action."))
        r = client.post("/api/instances/i1/agent-browser", json={"command": "rm -rf /"},
                        headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 400 and "allowed" in r.json()["detail"]

    def test_an_undrivable_instance_is_a_404(self, client, monkeypatch):
        self._stub(monkeypatch, raises=InstanceNotDrivable("belongs to another subject"))
        r = client.post("/api/instances/i1/agent-browser", json={"command": "snapshot"},
                        headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 404

    def test_no_token_is_refused(self, client, monkeypatch):
        self._stub(monkeypatch)
        r = client.post("/api/instances/i1/agent-browser", json={"command": "snapshot"})
        assert r.status_code == 401


# ── `upload`: the one verb that reads the container's disk ────────────────────

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 60
PDF = b"%PDF-1.4\n" + b"\x02" * 60


async def _one(data: bytes):
    yield data


async def _staging(root, *payloads, subject=OWNER):
    """A REAL staging store holding real files.

    Deliberately not a stub. The whole question this verb raises is *which paths
    the store will vouch for*, and a stub would only test our idea of that — the
    interesting failures are exactly where the two drift.
    """
    from app.services.uploads import UploadService

    store = UploadService(root)
    ticket = await store.mint(subject=subject, secret=SECRET)
    staged = [
        await store.stage(ticket.handle, subject=subject, filename=name, stream=_one(data))
        for name, data in payloads
    ]
    return store, ticket, staged


def _staging_sync(root, *payloads, subject=OWNER):
    """`_staging` for the synchronous REST tests, which have no loop of their own."""
    import asyncio

    return asyncio.run(_staging(root, *payloads, subject=subject))


def _expire(store, handle):
    """Age a ticket without waiting two hours: the manifest is what every gate
    reads, so moving its clock is the honest way to simulate one."""
    import json

    path = store.root / handle / ".ticket.json"
    manifest = json.loads(path.read_text())
    manifest["expires"] = 1.0
    path.write_text(json.dumps(manifest))


class TestUploadIsParsedLikeEveryOtherVerb:
    """`upload` gets no entry in _VERB_FLAGS and is not in _FLAGS_ONLY, so the
    parser's existing loop already refuses every option-looking token for it.
    Nothing in parse_command moved to make that true — these tests are what
    prove the claim rather than assuming it."""

    def test_the_verb_is_accepted_with_a_selector_and_a_path(self):
        assert parse_command("upload @e3 /data/uploads/upl_00112233445566aa/photo.jpg") == [
            "upload", "@e3", "/data/uploads/upl_00112233445566aa/photo.jpg"
        ]

    def test_several_files_are_just_more_positionals(self):
        """The CLI verb is variadic, so multi-photo needs no convention of ours."""
        assert parse_command("upload @e3 /a/one.jpg /a/two.png") == [
            "upload", "@e3", "/a/one.jpg", "/a/two.png"
        ]

    @pytest.mark.parametrize("payload", [
        "upload @e3 --cdp 9999 /a/x.jpg",
        "upload --cdp 9999 @e3 /a/x.jpg",
        "upload @e3 /a/x.jpg --cdp=9999",
        "upload @e3 --proxy http://evil /a/x.jpg",
        "upload @e3 --executable-path /bin/sh /a/x.jpg",
        "upload @e3 --user-data-dir /data/profiles /a/x.jpg",
        "upload -i @e3 /a/x.jpg",
    ])
    def test_no_option_looking_token_is_allowed_for_it(self, payload):
        with pytest.raises(AgentBrowserError, match="not allowed"):
            parse_command(payload)

    def test_a_quoted_path_with_spaces_survives_as_one_token(self):
        assert parse_command("upload @e3 '/data/uploads/upl_x/my photo.jpg'") == [
            "upload", "@e3", "/data/uploads/upl_x/my photo.jpg"
        ]

    def test_the_parser_still_refuses_everything_it_did_before(self):
        """Adding a verb must not widen the list by accident."""
        for bad in ("rm -rf /", "state save x", "mcp", "command.run echo", "install",
                    "uploads @e3 /a/x.jpg", "Upload @e3 /a/x.jpg"):
            with pytest.raises(AgentBrowserError):
                parse_command(bad)


@pytest.mark.asyncio
class TestUploadResolvesThroughTheStore:
    async def test_a_staged_path_reaches_the_browser(self, tmp_path, monkeypatch):
        import asyncio

        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        spy, calls = _fake_exec_sequence([(0, b"attached 1 file", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=4242)), store)

        # A caller string that is VALID but not character-identical to the path
        # the store returns. Without this the assertion normalises both sides and
        # cannot tell them apart — the review found exactly that, and this test
        # was passing for a reason it did not intend.
        noisy = f"{pathlib.Path(staged[0].path).parent}/./photo.jpg"
        out = await svc.drive("i1", f"upload @e3 {noisy}", subject=OWNER)

        assert out.ok and out.output == "attached 1 file"
        program, args = calls[0]
        assert program == "agent-browser"
        assert args[:4] == ("--cdp", "4242", "upload", "@e3")
        assert args[4:] == (str(pathlib.Path(staged[0].path).resolve()),)
        assert "/./" not in args[4], "the caller's own string reached argv"

    async def test_the_subprocess_gets_the_path_the_store_returned_not_the_callers_string(
        self, tmp_path, monkeypatch
    ):
        """The single most important assertion in this unit.

        The uploads root is reached through a symlink, so the path the caller
        writes and the path the store vouches for are DIFFERENT STRINGS for the
        same file. A service that validated the caller's string and then re-used
        it would pass every other test here and fail this one — which is exactly
        the bug `reclaim.removable_child`'s docstring warns about.
        """
        import asyncio

        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        store, ticket, staged = await _staging(link / "uploads", ("photo.jpg", JPEG))
        caller_path = f"{link}/uploads/{ticket.handle}/photo.jpg"
        assert "/link/" in caller_path, "the caller's string really does go via the link"

        spy, calls = _fake_exec_sequence([(0, b"ok", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        await svc.drive("i1", f"upload @e3 {caller_path}", subject=OWNER)

        passed = calls[0][1][4]
        assert "/link/" not in passed, f"the caller's own string reached argv: {passed}"
        assert passed.startswith(str(real.resolve()))
        assert pathlib.Path(passed).read_bytes() == JPEG

    async def test_a_noisy_but_valid_path_comes_back_canonical(self, tmp_path, monkeypatch):
        import asyncio

        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        noisy = f"{store.root / ticket.handle}/./photo.jpg"
        spy, calls = _fake_exec_sequence([(0, b"ok", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        await svc.drive("i1", f"upload @e3 {noisy}", subject=OWNER)

        assert "/./" not in calls[0][1][4]

    async def test_every_file_in_a_variadic_upload_is_resolved_in_order(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        store, ticket, staged = await _staging(
            tmp_path / "uploads",
            ("a.jpg", JPEG), ("b.png", PNG), ("c.pdf", PDF),
        )
        spy, calls = _fake_exec_sequence([(0, b"attached 3 files", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        noisy = [f"{pathlib.Path(f.path).parent}/./{pathlib.Path(f.path).name}"
                 for f in staged]
        out = await svc.drive("i1", "upload @e7 " + " ".join(noisy), subject=OWNER)

        assert out.ok
        args = calls[0][1]
        assert args[3] == "@e7", "the selector is passed through, like every other verb"
        assert list(args[4:]) == [str(pathlib.Path(f.path).resolve()) for f in staged]
        assert not any("/./" in a for a in args[4:]), "a caller string reached argv"

    async def test_the_selector_is_never_treated_as_a_path(self, tmp_path, monkeypatch):
        """`@e3` is a page ref. If it were run through the store it would be
        refused, and the verb would be unusable."""
        import asyncio

        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        spy, calls = _fake_exec_sequence([(0, b"ok", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        await svc.drive("i1", f"upload @e3 {staged[0].path}", subject=OWNER)

        assert calls[0][1][3] == "@e3"


@pytest.mark.asyncio
class TestUploadRefusals:
    @pytest.fixture
    def spy(self, monkeypatch):
        """A subprocess spy that records every run. The assertion that matters
        for a refusal is that this list stays EMPTY — a refusal after the browser
        already read the file would be no refusal at all."""
        import asyncio

        spy, calls = _fake_exec_sequence([(0, b"ok", b"")] * 5)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        return calls

    @pytest.mark.parametrize("attack", [
        "/data/.dek",
        "/etc/passwd",
        "/data/settings.json",
        "../../.dek",
    ])
    async def test_a_path_the_store_never_wrote_is_refused_before_any_subprocess(
        self, tmp_path, spy, attack
    ):
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        with pytest.raises(AgentBrowserError) as refused:
            await svc.drive("i1", f"upload @e3 {attack}", subject=OWNER)

        assert "is not an uploaded file" in str(refused.value)
        assert "create_upload_url" in str(refused.value)
        assert spy == [], "the browser was asked to read it anyway"

    async def test_the_dek_two_directories_up_from_a_real_ticket_is_refused(
        self, tmp_path, spy
    ):
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        dek = tmp_path / ".dek"
        dek.write_bytes(b"the key that decrypts settings.json")
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        for attack in (str(dek), f"{store.root / ticket.handle}/../../.dek"):
            with pytest.raises(AgentBrowserError, match="not an uploaded file"):
                await svc.drive("i1", f"upload @e3 {attack}", subject=OWNER)
        assert spy == []

    async def test_one_bad_path_among_good_ones_refuses_the_whole_command(
        self, tmp_path, spy
    ):
        """Never a partial upload: a caller who slipped one path in must not get
        the others attached and a warning."""
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        with pytest.raises(AgentBrowserError):
            await svc.drive(
                "i1", f"upload @e3 {staged[0].path} /data/.dek", subject=OWNER
            )
        assert spy == []

    async def test_an_expired_file_says_to_upload_it_again_and_says_it_differently(
        self, tmp_path, spy
    ):
        """This refusal replaces a status API, so it has to be unmistakable —
        and it must not read like "you made that path up"."""
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)
        _expire(store, ticket.handle)

        with pytest.raises(AgentBrowserError) as expired:
            await svc.drive("i1", f"upload @e3 {staged[0].path}", subject=OWNER)

        message = str(expired.value)
        assert "expired" in message and "upload it again" in message
        assert "is not an uploaded file" not in message
        assert pathlib.Path(staged[0].path).is_file(), "the bytes are still there"
        assert spy == []

    async def test_another_subjects_staged_file_is_refused(self, tmp_path, spy):
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst(subject="somebody-else")), store)

        with pytest.raises(AgentBrowserError, match="not an uploaded file"):
            await svc.drive("i1", f"upload @e3 {staged[0].path}", subject="somebody-else")
        assert spy == []

    @pytest.mark.parametrize("command", ["upload", "upload @e3"])
    async def test_a_selector_and_at_least_one_path_are_required(
        self, tmp_path, spy, command
    ):
        store, _t, _s = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        with pytest.raises(AgentBrowserError, match="selector and at least one"):
            await svc.drive("i1", command, subject=OWNER)
        assert spy == []

    async def test_a_store_that_returned_fewer_paths_than_asked_is_refused_loudly(
        self, tmp_path, spy, monkeypatch
    ):
        """The cross-unit invariant, pinned.

        `resolve_for` is a 1:1 comprehension today, so this cannot happen — the
        assertion exists because that is a property of ANOTHER module and
        nothing declares it a contract. The failure it would otherwise produce
        is the quiet kind: a browser told to attach two files when three were
        named, and no error anywhere. Here it is made to happen on purpose.
        """
        store, ticket, staged = await _staging(
            tmp_path / "uploads", ("a.jpg", JPEG), ("b.png", PNG)
        )
        monkeypatch.setattr(store, "resolve_for",
                            lambda subject, paths, **kw: [pathlib.Path(staged[0].path)])
        svc = AgentBrowserService(_FakeInstances(_FakeInst()), store)

        with pytest.raises(AgentBrowserError, match="different set of files"):
            await svc.drive(
                "i1", f"upload @e3 {staged[0].path} {staged[1].path}", subject=OWNER
            )
        assert spy == [], "a partial set of files was attached anyway"

    async def test_a_service_with_no_staging_store_says_so(self, spy):
        svc = AgentBrowserService(_FakeInstances(_FakeInst()))
        with pytest.raises(AgentBrowserError, match="cannot stage uploads"):
            await svc.drive("i1", "upload @e3 /data/uploads/upl_x/a.jpg", subject=OWNER)
        assert spy == []

    async def test_an_undrivable_instance_is_refused_before_the_paths_are_read(
        self, tmp_path, spy
    ):
        """Order matters: a sweep's browser is refused on its own grounds, and a
        caller learns nothing about which files exist from trying."""
        store, ticket, staged = await _staging(tmp_path / "uploads", ("photo.jpg", JPEG))
        svc = AgentBrowserService(_FakeInstances(_FakeInst(origin="task")), store)

        with pytest.raises(InstanceNotDrivable):
            await svc.drive("i1", f"upload @e3 {staged[0].path}", subject=OWNER)
        assert spy == []


class TestUploadOverRest:
    """The REST twin takes `command` as an opaque string, so it needed no change
    — proven by running a real upload through it rather than by reading it."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        with TestClient(app, base_url="https://testserver") as c:
            isolate_auth(app, tmp_path)
            yield c

    def test_an_upload_command_goes_through_untouched(self, client, monkeypatch, tmp_path):
        import asyncio

        store, ticket, staged = _staging_sync(tmp_path / "uploads", ("photo.jpg", JPEG))
        spy, calls = _fake_exec_sequence([(0, b"attached 1 file", b"")])
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        monkeypatch.setattr(
            app.state, "agent_browser",
            AgentBrowserService(_FakeInstances(_FakeInst(cdp_port=5150)), store),
        )

        noisy = f"{pathlib.Path(staged[0].path).parent}/./photo.jpg"
        r = client.post(
            "/api/instances/i1/agent-browser",
            json={"command": f"upload @e3 {noisy}"},
            headers={"Authorization": f"Bearer {mint_access(app)}"},
        )

        assert r.status_code == 200, r.text
        assert r.json()["output"] == "attached 1 file"
        assert calls[0][1][:4] == ("--cdp", "5150", "upload", "@e3")
        assert calls[0][1][4] == str(pathlib.Path(staged[0].path).resolve())
        assert "/./" not in calls[0][1][4], "the caller's own string reached argv"

    def test_a_path_the_store_never_wrote_is_a_400_that_says_what_to_do(
        self, client, monkeypatch, tmp_path
    ):
        store, _t, _s = _staging_sync(tmp_path / "uploads", ("photo.jpg", JPEG))
        monkeypatch.setattr(
            app.state, "agent_browser",
            AgentBrowserService(_FakeInstances(_FakeInst()), store),
        )

        r = client.post(
            "/api/instances/i1/agent-browser",
            json={"command": "upload @e3 /data/.dek"},
            headers={"Authorization": f"Bearer {mint_access(app)}"},
        )

        assert r.status_code == 400
        assert "not an uploaded file" in r.json()["detail"]
        assert "create_upload_url" in r.json()["detail"]


# Verbs the allow-list accepts but the description deliberately does not list:
# aliases and rarely-useful variants that would crowd the block a model reads
# without teaching it anything. Written down as a set rather than left implicit,
# so a verb ADDED to the allow-list without a line in the description trips this
# file — that is the dangerous direction, a new capability nobody wrote down.
UNDOCUMENTED_VERBS = frozenset({
    "open",      # alias of navigate
    "dblclick", "hover", "scroll", "select", "type", "wait",
})


def _published_description() -> str:
    """What a model is actually SHOWN, not what the source says.

    Read off the built server rather than sliced out of the file: the thing
    under test is the published surface, and a docstring is only a means to it.
    """
    import asyncio

    from app import mcp_server

    tools = asyncio.run(mcp_server.build(app).list_tools())
    return next(t for t in tools if t.name == "agent_browser").description


def _described_verbs(description: str) -> set[str]:
    """The verbs the description's block advertises, parsed out of the prose.

    Parsed rather than listed, and that is the whole point of the change that
    introduced this. The version before it compared a HARDCODED set of twelve
    known-good verbs against the allow-list, so it could only ever re-confirm
    what it already named: a bogus `download @e3 <path>` line added to the
    description left the entire suite green. A test that cannot see tomorrow's
    drift is not guarding against it.

    The block is the run of deeply-indented lines; each begins with the verb
    form, separated from its gloss by two or more spaces. `back / forward /
    reload` is three verbs on one line and is read as three.
    """
    import re

    verbs: set[str] = set()
    for line in description.splitlines():
        if not line.strip() or (len(line) - len(line.lstrip())) < 12:
            continue
        head = re.split(r"\s{2,}", line.strip())[0]
        for part in head.split("/"):
            token = part.strip().split(" ")[0]
            if token:
                verbs.add(token)
    return verbs


class TestTheToolDescriptionMatchesWhatIsEnforced:
    """A tool description that overstates what the server accepts is a bug with
    a very long feedback loop — the model believes it for the whole session."""

    @pytest.fixture(scope="class")
    def doc(self) -> str:
        return _published_description()

    def test_the_verb_block_lists_upload_with_a_path(self, doc):
        assert "upload @e3 <path>     attach an uploaded file to a file input" in doc

    def test_it_tells_the_model_where_the_path_must_come_from(self, doc):
        assert "create_upload_url" in doc
        assert "A path you wrote yourself is" in doc and "refused" in doc

    def test_it_admits_the_case_it_cannot_serve(self, doc):
        """setInputFiles binds to an <input type=file>; a native chooser needs
        Playwright's filechooser event, which the CLI has no fallback for. A
        model that does not know will loop on a page it cannot serve."""
        assert "file picker" in doc

    def test_the_verb_block_is_actually_being_parsed(self, doc):
        """The control. Every assertion below is `parsed <= allowed`, which a
        parser that found NOTHING would satisfy perfectly."""
        described = _described_verbs(doc)
        assert {"navigate", "snapshot", "read", "click", "fill", "press", "upload",
                "get", "back", "forward", "reload", "screenshot"} <= described
        assert len(described) >= 12

    def test_every_verb_the_description_advertises_is_actually_allowed(self, doc):
        from app.services.agent_browser import ALLOWED_VERBS

        described = _described_verbs(doc)
        overstated = described - set(ALLOWED_VERBS)
        assert not overstated, (
            f"the description advertises {sorted(overstated)}, which parse_command "
            "refuses. A model will try them for the whole session and be told no."
        )

    def test_no_allowed_verb_is_advertised_by_accident_or_hidden_by_accident(self, doc):
        """The other direction: a verb added to the allow-list without a line in
        the description. That is a capability nobody wrote down, and it is the
        direction that matters more."""
        from app.services.agent_browser import ALLOWED_VERBS

        undescribed = set(ALLOWED_VERBS) - _described_verbs(doc)
        assert undescribed == set(UNDOCUMENTED_VERBS), (
            "the set of allowed-but-undescribed verbs changed.\n"
            f"  newly undescribed: {sorted(undescribed - set(UNDOCUMENTED_VERBS))}\n"
            f"  no longer undescribed: {sorted(set(UNDOCUMENTED_VERBS) - undescribed)}"
        )
