"""Drive a running browser conversationally, by shelling out to the `agent-browser`
CLI over the instance's own local CDP endpoint.

**The one rule that matters lives here, because this shells out on a string an LLM
wrote.** The command is tokenised with `shlex` — which is a parser, not a shell —
its first token is checked against an allow-list of read/interact verbs, and the
tokens are handed to `agent-browser` as a bare argv via
`create_subprocess_exec`. No string is ever passed to a shell, so `;`, `|`,
`$(...)`, backticks, `&&` and friends are inert literal arguments, never
operators. There is deliberately no `shell=True` / `create_subprocess_shell`
path; the injection test reintroduces one and watches the canary fire.

The allow-list is verbs only. It excludes anything that writes the container's
disk or launches/escapes a browser — `screenshot` (to a caller path), `state`,
`mcp`, `install`, `command.run`, raw `--user-data-dir`, etc. A screenshot of the
resulting page is taken by *this* service to a path *it* controls and returned,
so the caller never needs a file-writing verb.

Driving is the same privilege as the CDP endpoint (`create_instance` already
hands out a `cdp_url`), so it carries the same guards: a sweep's browser is
refused (it is mid-navigation on its own schedule), and the instance must belong
to the calling subject.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shlex
import tempfile
from dataclasses import dataclass

from .tokens import OWNER

logger = logging.getLogger("cloakbiz.agent_browser")

# Read and interact only. No file-writing or browser-launching subcommands, so a
# command can neither touch the container's disk nor escape the target browser.
ALLOWED_VERBS = frozenset({
    "navigate", "open", "back", "forward", "reload",
    "snapshot", "read", "get",
    "click", "dblclick", "hover", "fill", "type", "press", "select", "scroll", "wait",
})

_RUN_TIMEOUT = 45.0
_SHOT_TIMEOUT = 20.0


def _binary() -> str:
    # Read at call time so a test can point it at a harmless stand-in.
    return os.environ.get("AGENT_BROWSER_BIN") or "agent-browser"


class AgentBrowserError(ValueError):
    """A command refused before anything runs — empty, unparseable, or not allowed."""


class InstanceNotDrivable(RuntimeError):
    """No such instance, one that belongs elsewhere, or one that must not be driven."""


def parse_command(command: str) -> list[str]:
    """The allow-listed argv for one `agent-browser` action, or raise.

    `shlex.split` tokenises with quote handling and invokes no shell, so shell
    metacharacters survive as ordinary tokens. Only the first token — the verb —
    gates the call; the rest are passed through as literal arguments.
    """
    try:
        argv = shlex.split(command or "")
    except ValueError as exc:  # e.g. an unbalanced quote
        raise AgentBrowserError(f"could not parse the command: {exc}") from exc
    if not argv:
        raise AgentBrowserError("empty command")
    verb = argv[0]
    if verb not in ALLOWED_VERBS:
        raise AgentBrowserError(
            f"{verb!r} is not an allowed action. Allowed: "
            f"{', '.join(sorted(ALLOWED_VERBS))}."
        )
    return argv


@dataclass
class DriveOutcome:
    instance_id: str
    command: str
    ok: bool
    output: str
    screenshot: bytes | None


class AgentBrowserService:
    """Runs allow-listed `agent-browser` actions against a pool instance's CDP."""

    def __init__(self, instances) -> None:
        self._instances = instances

    def _cdp_port(self, instance_id: str, subject: str | None) -> int:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise InstanceNotDrivable(
                f"No running browser with instance_id={instance_id!r}. It may have "
                f"been closed, or reaped after going idle."
            )
        # Same refusal CDP makes: a sweep's browser is mid-navigation on its own
        # schedule and driving it would corrupt the run.
        if getattr(inst, "origin", None) == "task":
            raise InstanceNotDrivable(
                f"instance {instance_id!r} belongs to a running sweep and cannot be driven"
            )
        # Same subject binding CDP/VNC make: a browser with no recorded subject
        # (a sweep's own) falls back to the one owner, never to "anyone".
        owner = getattr(inst, "subject", None) or OWNER
        if subject is not None and owner != subject:
            raise InstanceNotDrivable(f"instance {instance_id!r} belongs to another subject")
        return inst.cdp_port

    async def _run(self, port: int, argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        """One `agent-browser --cdp <port> <argv>` invocation. exec, never a shell."""
        proc = await asyncio.create_subprocess_exec(
            _binary(), "--cdp", str(port), *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"the browser did not respond within {int(timeout)}s") from None
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _screenshot(self, port: int) -> bytes | None:
        """A PNG of the current page, to a path this service controls (never the
        caller's). Best effort: a failure here must not sink the command's own
        result."""
        with tempfile.TemporaryDirectory(prefix="ab-shot-") as d:
            path = pathlib.Path(d) / "shot.png"
            try:
                rc, _out, _err = await self._run(port, ["screenshot", str(path)],
                                                 timeout=_SHOT_TIMEOUT)
            except TimeoutError:
                return None
            if rc == 0 and path.exists():
                return path.read_bytes()
        return None

    async def drive(self, instance_id: str, command: str, *,
                    subject: str | None = OWNER) -> DriveOutcome:
        """Run one allow-listed action against the instance, then snapshot the page.

        Parsing/allow-listing happens before the instance is even resolved, so a
        disallowed command is refused without regard to who asked."""
        argv = parse_command(command)
        port = self._cdp_port(instance_id, subject)
        # A browser that hangs is a failed *action* the caller can read and retry,
        # not an error about the request — so it comes back ok=False, not raised.
        try:
            rc, out, err = await self._run(port, argv, timeout=_RUN_TIMEOUT)
        except TimeoutError as exc:
            logger.warning("agent_browser %s %r timed out", instance_id, argv[0])
            return DriveOutcome(instance_id, command, False, str(exc), None)
        ok = rc == 0
        output = (out.strip() or err.strip() or ("done" if ok else "failed"))
        shot = await self._screenshot(port)
        logger.info("agent_browser %s %r rc=%s", instance_id, argv[0], rc)
        return DriveOutcome(instance_id, command, ok, output, shot)
