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
        # The verb is allowed; the rest are inert tokens, never operators.
        assert parse_command("navigate a; rm -rf /") == ["navigate", "a;", "rm", "-rf", "/"]
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
        await svc.drive("i1", "navigate a; rm -rf /", subject=OWNER)
        program, args = calls[0]  # the command itself (calls[1] is the screenshot)
        assert program == "agent-browser"
        # --cdp <port> then the tokens, each its own argv item (";" and "rm" separate)
        assert args[:3] == ("--cdp", "4242", "navigate")
        # The metacharacters are literal argv items ("a;" glued, "rm"/"-rf" plain),
        # passed to agent-browser as arguments — never a shell operator.
        assert "a;" in args and "rm" in args and "-rf" in args


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


# ── the REST mirror + its auth ────────────────────────────────────────────────
class TestRestEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        monkeypatch.delenv("APP_SECRET_RESET", raising=False)
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
