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

# Read and interact only. `screenshot` is here but SERVICE-HANDLED (see drive):
# agent-browser's raw `screenshot <path>` takes a caller path — the file-write
# surface — so it never reaches the passthrough. The verb captures to a path this
# service picks; the caller only chooses viewport vs full page.
ALLOWED_VERBS = frozenset({
    "navigate", "open", "back", "forward", "reload",
    "snapshot", "read", "get",
    "click", "dblclick", "hover", "fill", "type", "press", "select", "scroll", "wait",
    "screenshot",
})

# Verbs that take NO positional arguments — only their whitelisted flags.
# `screenshot` is service-handled: the caller may pick full/annotate, never the
# output path or an element, so any positional (a path, an @ref) is refused.
_FLAGS_ONLY = frozenset({"screenshot"})

# Options allowed *per verb*, as an explicit whitelist. This is the second half of
# the security boundary, and it is a whitelist for a reason: agent-browser parses
# its global options (`--cdp`, `--proxy`, `--executable-path`, `--init-script`,
# `--extension`, …) from ANYWHERE in the argv, any position, `--` does not stop
# them. So a verb-only allow-list is not enough — `navigate --cdp <otherport>`
# would redirect the command to a DIFFERENT instance's browser, going around the
# subject-bound port we resolved. Whitelisting the flags neutralises `--cdp` and
# every other global option by construction, and a future agent-browser flag
# cannot silently widen the hole. Only `snapshot` takes flags; every other verb
# takes positional arguments only, so any option-looking token is refused for it.
_VERB_FLAGS = {
    "snapshot": frozenset({"-i", "-u", "-c", "-d", "-s", "--json"}),
    # Display geometry only; the output path is never a flag here (the service
    # supplies it), so no path-taking flag is whitelisted.
    "screenshot": frozenset({"--full", "--annotate"}),
}

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

    Two gates, both required. First the verb (`argv[0]`) must be an allowed
    read/interact action. Then every remaining option-looking token — anything
    starting with `-` — must be in that verb's flag whitelist; only `snapshot`
    has one. This second gate is what stops option-injection: a smuggled
    `--cdp <port>` (or `--proxy`, `--executable-path`, …) anywhere in the argv
    would otherwise redirect the whole command to another instance's browser.

    `shlex.split` tokenises with quote handling and invokes no shell, so shell
    metacharacters survive as ordinary tokens rather than becoming operators.
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
    allowed_flags = _VERB_FLAGS.get(verb, frozenset())
    flags_only = verb in _FLAGS_ONLY
    for token in argv[1:]:
        is_option = token.startswith("-") and token != "-"
        # An option to agent-browser's global parser — the smuggling vector —
        # must be explicitly whitelisted. Matched exactly, so the "=" form
        # (--cdp=59999) and combined shorts (-ic) do not sneak through.
        if is_option and token not in allowed_flags:
            allowed = ", ".join(sorted(allowed_flags)) if allowed_flags else "none"
            raise AgentBrowserError(
                f"option {token!r} is not allowed for {verb!r} (allowed flags: {allowed}). "
                f"This blocks redirecting the command to another browser."
            )
        # A flags-only verb (screenshot) refuses positionals, so the caller can
        # never supply the output path or an element — the service picks the path.
        if not is_option and flags_only:
            raise AgentBrowserError(
                f"{verb!r} takes no positional arguments — the service chooses the "
                f"output path. Allowed flags: {', '.join(sorted(allowed_flags))}."
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

    async def _screenshot(self, port: int, flags: list[str]) -> bytes | None:
        """A PNG of the current page, written to a path THIS service picks (never
        the caller's) and read back. `flags` is the already-whitelisted display
        geometry (--full/--annotate); the path is always appended by us, last."""
        with tempfile.TemporaryDirectory(prefix="ab-shot-") as d:
            path = pathlib.Path(d) / "shot.png"
            try:
                rc, _out, _err = await self._run(port, ["screenshot", *flags, str(path)],
                                                 timeout=_SHOT_TIMEOUT)
            except TimeoutError:
                return None
            if rc == 0 and path.exists():
                return path.read_bytes()
        return None

    async def drive(self, instance_id: str, command: str, *,
                    subject: str | None = OWNER) -> DriveOutcome:
        """Run one allow-listed action against the instance.

        Text by default: a screenshot is returned ONLY for the explicit
        `screenshot` verb (opt-in), so a `read`/`get`/`snapshot` doesn't spend the
        user tokens on a PNG they didn't ask for. Parsing/allow-listing happens
        before the instance is even resolved, so a disallowed command is refused
        without regard to who asked."""
        argv = parse_command(command)
        port = self._cdp_port(instance_id, subject)

        # `screenshot` is intercepted, never passed through: the service captures
        # to its own path with only the whitelisted display flags, so agent-browser
        # never receives a caller-chosen path.
        if argv[0] == "screenshot":
            flags = argv[1:]  # already validated to whitelisted flags only
            shot = await self._screenshot(port, flags)
            ok = shot is not None
            return DriveOutcome(instance_id, command, ok,
                                "screenshot captured" if ok else "could not capture a screenshot",
                                shot)

        # A browser that hangs is a failed *action* the caller can read and retry,
        # not an error about the request — so it comes back ok=False, not raised.
        try:
            rc, out, err = await self._run(port, argv, timeout=_RUN_TIMEOUT)
        except TimeoutError as exc:
            logger.warning("agent_browser %s %r timed out", instance_id, argv[0])
            return DriveOutcome(instance_id, command, False, str(exc), None)
        ok = rc == 0
        output = (out.strip() or err.strip() or ("done" if ok else "failed"))
        logger.info("agent_browser %s %r rc=%s", instance_id, argv[0], rc)
        return DriveOutcome(instance_id, command, ok, output, None)
