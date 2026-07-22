"""The agent_browser passthrough — and the one thing that must not break.

This shells out on a command an LLM wrote, so the security surface is the
shell-out. The tests below prove the command is tokenised (not shell-parsed),
allow-listed by verb, argv-passed, and bound to the caller's own non-sweep
browser. The injection test uses a harmless stand-in binary and a canary file:
if the metacharacters were ever handed to a shell, the canary would be created.
"""
from __future__ import annotations

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
