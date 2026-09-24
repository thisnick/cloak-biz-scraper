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

**One verb now READS the disk, and it is worth saying why that changed.**
`upload` was excluded by exactly the sentence above: it takes caller-named paths
and `setInputFiles` reads whatever it is pointed at, so `upload @e3 /data/.dek`
would post the key that decrypts the licence, proxy and Notion credentials to
whatever site the browser is sitting on. What makes it admissible is that the
caller no longer names the file. Every path is run through
`services/uploads.resolve_for`, which returns only files this server itself
wrote into a live, subject-owned staging ticket — and the argv handed to
`create_subprocess_exec` is built from what THAT returned, never from the
caller's string re-used after a boolean check. The verb still writes nothing;
it reads one directory the server filled itself.

**And one verb now WRITES, under the same discipline as `screenshot`.**
`agent-browser download <selector> <path>` saves wherever it is told, so the
caller supplies the selector and nothing else. The path is a landing directory
services/downloads minted, the bytes are watched while they arrive and the
download is cancelled the moment it outgrows its reservation, and what the
caller gets back is a ticket for the finished file — never a path on this disk.

Driving is the same privilege as the CDP endpoint (`create_instance` already
hands out a `cdp_url`), so it carries the same guards: a sweep's browser is
refused (it is mid-navigation on its own schedule), and the instance must belong
to the calling subject.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import shlex
import tempfile
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
import websockets

from . import downloads as downloads_service
from .tokens import OWNER
from .uploads import Expired, NotStaged

logger = logging.getLogger("cloakbiz.agent_browser")

# Read and interact only. `screenshot` is here but SERVICE-HANDLED (see drive):
# agent-browser's raw `screenshot <path>` takes a caller path — the file-write
# surface — so it never reaches the passthrough. The verb captures to a path this
# service picks; the caller only chooses viewport vs full page.
# `upload` is here and SERVICE-HANDLED too (see drive): its paths are replaced
# with the ones services/uploads.resolve_for vouched for before any argv is
# built, so a caller-named path never reaches agent-browser.
# `download` is SERVICE-HANDLED as well (see drive): the caller names the element
# to click and the service names the file — the same split `screenshot` makes.
ALLOWED_VERBS = frozenset({
    "navigate", "open", "back", "forward", "reload",
    "snapshot", "read", "get",
    "click", "dblclick", "hover", "fill", "type", "press", "select", "scroll", "wait",
    "screenshot", "upload", "download",
})

# Verbs that take EXACTLY this many positionals. `download` takes its selector
# and nothing else: agent-browser's second positional is the output path, the
# file-write surface, and it is always ours.
_EXACT_POSITIONALS = {"download": 1}

# Verbs that take NO positional arguments — only their whitelisted flags.
# `screenshot` is service-handled: the caller may pick full/annotate, never the
# output path, so any positional is refused. agent-browser's screenshot DOES take
# positionals (`screenshot [selector] [path]` — an element form exists); we omit
# both deliberately, to keep this verb flags-only. The path must be ours (it is
# the file-write surface), and a selector positional would widen the argument
# surface for no real gain, so it stays out.
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
# A download is a transfer, not a page action: a 100 MB file over a residential
# proxy takes minutes, and cutting it at 45s would refuse exactly the files the
# cap was sized for.
_DOWNLOAD_TIMEOUT = 180.0
# How often the landing directory is measured while a download runs. At a
# generous 50 MB/s this lets at most ~25 MB past the cap before the cancel.
_WATCH_INTERVAL = 0.5
# Chromium answers /json locally and instantly; a slow answer means it is wedged.
_CDP_TIMEOUT = 5.0
# How long a killed CLI gets to close its pipes before we stop waiting for it.
_REAP_TIMEOUT = 2.0
_WARM_TIMEOUT = 8.0
_CLOSE_TIMEOUT = 5.0


def _session(port: int) -> str:
    """The agent-browser daemon/session dedicated to one bound CDP port.

    agent-browser's implicit session is literally named ``default``. Passing a
    different ``--cdp`` on two concurrent CLI invocations does not create two
    daemons: each invocation sends a reattach followed by its action to that
    shared daemon, so ``attach A, attach B, click A`` can click browser B.
    A port is unique for the lifetime of a pool instance, making it the stable,
    non-secret isolation key we need. agent-browser 0.34+ persists this binding
    per named session and 0.36 keeps the same contract.
    """
    return f"cloakbiz-cdp-{port}"

# First-call readiness race. The `agent-browser` CLI cold-starts a detached
# per-session daemon (a node process) on its first invocation, and that daemon
# makes its OWN fresh CDP attach (`connectOverCDP`) to the browser. Right after
# create_instance the browser's CDP port is already listening — but the daemon
# may still be coming up, or its first attach can be refused for a beat. The CLI
# then exits non-zero with one of a small, specific set of "can't reach the
# daemon / can't attach CDP" messages; a second invocation a moment later meets a
# warm daemon and succeeds. We retry ONLY that class, a bounded number of times.
#
# Every marker below is about REACHING the daemon or ATTACHING CDP — never about
# the page. A genuine command failure (a real navigation error, a missing
# element) carries a different message, is not matched here, and is surfaced on
# the first attempt. A truly dead instance keeps matching, so it fails fast after
# the bound with its own clear message rather than hanging.
#
# Every marker is a fixed prefix the agent-browser CLI itself emits, NOT a bare
# phrase that could ride in on echoed caller input. That distinction matters
# because the match is against combined stdout+stderr, which includes the CLI's
# echo of the selector/URL: a bare "connection refused" / "econnrefused" would
# match a REAL, permanent command error whose text merely contains those words —
# e.g. `click "text=Connection refused"` -> "Element not found: text=Connection
# refused" (a mutating verb, re-executed on every retry), or a navigate to a URL
# containing "econnrefused" that fails DNS. The real Unix/Docker daemon-socket
# refusal is "Failed to connect: Connection refused (os error 111)", already
# caught by the "failed to connect:" prefix — so no transient coverage is lost.
_TRANSIENT_MARKERS = (
    "failed to connect via cdp",   # the daemon's connectOverCDP raced the browser
    "cdp connection failed",       # the CLI's fallback text for the same
    "make sure the app is running with --remote-debugging-port",
    "daemon failed to start",      # daemon socket not ready within the CLI's own poll
    "failed to start daemon",
    "failed to connect:",          # client -> daemon socket, e.g. "Connection refused
                                   #   (os error 111)" or "No such file or directory"
    "failed to send:",             # daemon died mid-handshake
    "failed to read:",
)
_RETRY_ATTEMPTS = 3            # 1 initial try + 2 retries
_RETRY_BACKOFF = (0.3, 0.7)   # seconds to wait before retry 2 and retry 3


def _is_transient_failure(rc: int, out: str, err: str) -> bool:
    """A non-zero result whose output is the daemon/CDP readiness race, not a
    genuine command failure. Scoped by an explicit marker list so only the
    first-call race retries; everything else surfaces immediately."""
    if rc == 0:
        return False
    blob = f"{out}\n{err}".lower()
    return any(marker in blob for marker in _TRANSIENT_MARKERS)


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
    exact = _EXACT_POSITIONALS.get(verb)
    if exact is not None and len(argv) - 1 != exact:
        raise AgentBrowserError(
            f"{verb!r} takes exactly one argument — the element to click, e.g. "
            "`download @e5` or `download \"a[href$='.pdf']\"`. The service chooses "
            "where the file goes and hands back a link to fetch it."
        )
    return argv


# What CDP answers when `setInputFiles` is pointed at something that is not an
# `<input type=file>`. Verified against agent-browser 0.32.3 driving real
# CloakBrowser Pro 148, where a styled picker button produced exactly this and
# nothing reached the page.
_NOT_A_FILE_INPUT = "not a file input element"

# The hint, and why it is worth coupling to somebody else's error string: on a
# real listing site the file input is usually hidden behind a styled button, so
# `snapshot -i` lists the BUTTON and not the input — the accessibility tree does
# not carry it. A model following the documented snapshot-then-act workflow
# therefore meets this error on a page that would have worked by CSS selector,
# reads "a file picker is not supported", and gives up. The coupling is cheap
# (a substring of stderr) and fails safe: if the CLI rewords its error we lose a
# hint, never correctness.
_UPLOAD_HINT = (
    " — that element is not a file input. Most pages hide the real one behind a "
    "styled button and a snapshot only shows the button, so try a CSS selector "
    'instead: upload "input[type=file]" <path>. If the page has no file input at '
    "all and the button opens the operating system's own picker, that cannot be "
    "driven from here."
)


def _with_upload_hint(output: str) -> str:
    """Turn a legible CDP error into a next step.

    `Node is not a file input element` tells a reader what went wrong and
    nothing about what to do, which on this particular failure is the difference
    between abandoning a page and uploading to it.
    """
    if _NOT_A_FILE_INPUT in output.lower():
        return output + _UPLOAD_HINT
    return output


@dataclass
class DriveOutcome:
    instance_id: str
    command: str
    ok: bool
    output: str
    screenshot: bytes | None
    # Set only by a successful `download`. The façades turn it into a fetch URL,
    # because only they know the address this server is reachable at.
    download: "downloads_service.KeptDownload | None" = None


class _Stopped(Exception):
    """A watched run the watcher cut short."""


class _SuggestedName:
    """The filename the site suggested, heard from the pool's own Playwright
    context while a download runs. Best effort by design.

    agent-browser saves to the path it was given and does not say what the site
    called the file. Chromium does, in `downloadWillBegin` — and the Playwright
    context this server launched the browser with receives that event for a
    download agent-browser starts. Nothing depends on it: a missed event (a test
    double with no context, a future CLI that disables download events) costs
    the nice name, and the file is called `download.<ext>` from its bytes.
    """

    def __init__(self, context) -> None:
        self.name: str | None = None
        self.url = ""
        self._context = context
        self._pages: list = []
        if context is None:
            return
        try:
            for page in list(context.pages):
                self._watch(page)
            context.on("page", self._watch)
        except Exception as exc:  # noqa: BLE001 - a name is not worth a failure
            logger.debug("could not listen for download names: %r", exc)

    def _watch(self, page) -> None:
        page.on("download", self._seen)
        self._pages.append(page)

    def _seen(self, download) -> None:
        # The first one: the click this verb made. Anything later in the same
        # window is somebody else's, and the name is cosmetic either way.
        if self.name is None:
            self.name = getattr(download, "suggested_filename", None) or None
            self.url = getattr(download, "url", "") or ""

    def stop(self) -> None:
        for page in self._pages:
            with contextlib.suppress(Exception):
                page.remove_listener("download", self._seen)
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.remove_listener("page", self._watch)


class AgentBrowserService:
    """Runs allow-listed `agent-browser` actions against a pool instance's CDP."""

    def __init__(self, instances, uploads=None, downloads=None,
                 secret: Callable[[], str | None] | None = None) -> None:
        self._instances = instances
        # The store behind the `download` verb, and the signing secret its fetch
        # tickets are minted with — read per call through a callable, like
        # everywhere else, so a rotated APP_SECRET is never captured stale.
        self._downloads = downloads
        self._secret = secret or (lambda: None)
        # The staging store behind the `upload` verb. Optional so a test double
        # or an embedder that never stages files can build the service without
        # one — `upload` then refuses with a message that says so, rather than
        # the service failing to construct for a verb nobody is using.
        self._uploads = uploads
        # Warm the per-port agent-browser daemon the moment a browser launches,
        # so the caller's FIRST command meets a warm daemon instead of racing its
        # cold start. Registered as an optional hook the instance pool fires
        # post-registration for interactive launches; the pool owns the
        # fire-and-forget scheduling and shutdown draining, so instances.py keeps
        # no dependency on this service. getattr keeps test doubles that lack the
        # setter working unchanged.
        register = getattr(instances, "set_launch_warm_hook", None)
        if callable(register):
            register(self.warm)
        register_close = getattr(instances, "set_launch_close_hook", None)
        if callable(register_close):
            register_close(self.close)

    def _cdp_port(self, instance_id: str, subject: str | None) -> int:
        return self._drivable(instance_id, subject).cdp_port

    def _drivable(self, instance_id: str, subject: str | None):
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
        return inst

    async def _run(self, port: int, argv: list[str], *, timeout: float,
                   watch: Callable[[], Awaitable[bool]] | None = None
                   ) -> tuple[int, str, str]:
        """One `agent-browser --cdp <port> <argv>` invocation. exec, never a shell.

        `watch`, when given, is polled every `_WATCH_INTERVAL` while the CLI
        runs; answering True kills it and raises `_Stopped`. That is how a
        download is cut off while its bytes are still arriving rather than
        measured after they have all landed.
        """
        proc = await asyncio.create_subprocess_exec(
            _binary(), "--session", _session(port), "--cdp", str(port), *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        if watch is not None:
            return await self._watched(proc, timeout, watch)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"the browser did not respond within {int(timeout)}s") from None
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    @staticmethod
    async def _watched(proc, timeout: float,
                       watch: Callable[[], Awaitable[bool]]) -> tuple[int, str, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        talking = asyncio.ensure_future(proc.communicate())
        try:
            while not talking.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"the browser did not respond within {int(timeout)}s")
                await asyncio.wait({talking}, timeout=min(_WATCH_INTERVAL, remaining))
                if not talking.done() and await watch():
                    raise _Stopped()
        except BaseException:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            # Cancelled, not drained. The `agent-browser` on PATH is a Node
            # wrapper around the native CLI, and killing the wrapper leaves the
            # native child holding stdout open until ITS download ends —
            # measured: the watchdog tripped at 2s and the call still took 12s,
            # the whole transfer. The caller cancels the download itself next,
            # which is what makes that child exit.
            talking.cancel()
            with contextlib.suppress(BaseException):
                await talking
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(proc.wait(), _REAP_TIMEOUT)
            raise
        out, err = talking.result()
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _run_resilient(self, port: int, argv: list[str], *,
                             timeout: float,
                             watch: Callable[[], Awaitable[bool]] | None = None
                             ) -> tuple[int, str, str]:
        """`_run`, retrying ONLY the first-call daemon/CDP readiness race.

        The retry is scoped to `_is_transient_failure` — a cold agent-browser
        daemon or its initial CDP attach being refused for a beat right after
        create_instance. A real command error is returned on the first attempt,
        and a timeout is left to propagate to the caller (never retried), so a
        genuinely broken action or a dead instance still fails fast. The bound is
        small, so even a permanently unreachable daemon returns promptly."""
        extra = {"watch": watch} if watch is not None else {}
        rc, out, err = await self._run(port, argv, timeout=timeout, **extra)
        for attempt in range(1, _RETRY_ATTEMPTS):
            if not _is_transient_failure(rc, out, err):
                break
            logger.info("agent_browser %r transient (rc=%s), retry %d/%d",
                        argv[0], rc, attempt, _RETRY_ATTEMPTS - 1)
            await asyncio.sleep(_RETRY_BACKOFF[attempt - 1])
            rc, out, err = await self._run(port, argv, timeout=timeout, **extra)
        return rc, out, err

    async def warm(self, port: int) -> None:
        """Best-effort: spawn/attach the agent-browser daemon for `port` so the
        caller's first real command doesn't pay the cold-start race.

        Runs one cheap, side-effect-free action (`get url`) — enough to bring the
        daemon up and make its initial CDP attach — and swallows everything. A
        warm that fails changes nothing the caller sees; the in-`drive` retry is
        the real guarantee. Short-bounded so it never adds meaningful latency."""
        try:
            await self._run(port, ["get", "url"], timeout=_WARM_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - warming must never surface
            logger.debug("agent_browser warm on cdp=%s did not complete: %r", port, exc)

    async def close(self, port: int) -> None:
        """Best-effort stop of the daemon dedicated to a closed pool instance.

        No ``--cdp`` is supplied: the browser has already closed, and all we
        need is the named daemon's own ``close`` action. This is an internal
        lifecycle hook, not a caller-reachable verb.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                _binary(), "--session", _session(port), "close",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), _CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.debug("agent_browser close on cdp=%s timed out", port)
        except Exception as exc:  # noqa: BLE001 - cleanup must never block pool release
            logger.debug("agent_browser close on cdp=%s did not complete: %r", port, exc)

    async def _screenshot(self, port: int, flags: list[str]) -> bytes | None:
        """A PNG of the current page, written to a path THIS service picks (never
        the caller's) and read back. `flags` is the already-whitelisted display
        geometry (--full/--annotate); the path is always appended by us, last."""
        with tempfile.TemporaryDirectory(prefix="ab-shot-") as d:
            path = pathlib.Path(d) / "shot.png"
            try:
                rc, _out, _err = await self._run_resilient(
                    port, ["screenshot", *flags, str(path)], timeout=_SHOT_TIMEOUT)
            except TimeoutError:
                return None
            if rc == 0 and path.exists():
                return path.read_bytes()
        return None

    def _staged_upload(self, argv: list[str], subject: str | None) -> list[str]:
        """`["upload", <selector>, *paths]` with the paths replaced by ours.

        The selector is passed through like every other verb's argument — it is
        a page ref, not a filesystem name. The paths are not passed through at
        all: they are looked up, and what comes back is what runs.

        `subject or OWNER` mirrors ws_guard's fallback and for the same reason —
        a caller with no recorded subject falls back to the one resource owner,
        which is the strictest reading, never to "anybody's tickets".

        Note this is the OPPOSITE convention from `tokens.verify`, where
        `subject=None` means "any subject". That is deliberate, not an
        inconsistency to be tidied away: there, None is a documented hole for an
        instance whose owner was never recorded, and the worst case is a token
        check that does not narrow. Here the same default would hand one
        caller's staged files to another — so unknown resolves to the strictest
        answer, not the loosest.
        """
        if self._uploads is None:
            raise AgentBrowserError(
                "this server cannot stage uploads, so there is nothing for `upload` to attach"
            )
        if len(argv) < 3:
            raise AgentBrowserError(
                "upload needs a selector and at least one staged file path, e.g. "
                "`upload @e3 /data/uploads/upl_.../photo.jpg`"
            )
        try:
            resolved = self._uploads.resolve_for(subject or OWNER, argv[2:])
        except (NotStaged, Expired) as exc:
            # One clause, two messages, and the difference is the point: "not an
            # uploaded file" and "that file has expired" call for different
            # readings by the model, and the expired one is the only status
            # check this feature has. The wording belongs to the store, so the
            # refusal a model reads cannot drift from the rule that produced it.
            raise AgentBrowserError(str(exc)) from exc
        # Pinning a cross-unit invariant, not defending against a known bug.
        # There is no silent-drop defect today: resolve_for is a 1:1 list
        # comprehension, so a path either comes back or raises. But that is a
        # property of ANOTHER module, undocumented as a contract, and the failure
        # it would produce here is the quiet kind — a browser told to attach two
        # files when the caller named three, and no error anywhere. One line
        # turns that into a loud one.
        if len(resolved) != len(argv[2:]):
            raise AgentBrowserError(
                f"resolved {len(resolved)} of {len(argv) - 2} file paths; refusing to "
                "attach a different set of files than you asked for"
            )
        return ["upload", argv[1], *(str(path) for path in resolved)]

    async def _browser_command(self, port: int, method: str, params: dict) -> bool:
        """One Browser-domain CDP command on the instance's own local endpoint.

        Best effort and short-bounded: every caller is cleaning up after a
        download, and cleanup must never be what fails the call. Loopback
        only, and `trust_env=False` so an ambient HTTP proxy cannot swallow a
        request that was never meant to leave the container.
        """
        try:
            async with httpx.AsyncClient(timeout=_CDP_TIMEOUT, trust_env=False) as client:
                version = await client.get(f"http://127.0.0.1:{port}/json/version")
                version.raise_for_status()
                target = version.json()["webSocketDebuggerUrl"]
            async with websockets.connect(
                target, max_size=None, open_timeout=_CDP_TIMEOUT,
                ping_interval=None, ping_timeout=None,
            ) as ws:
                await ws.send(json.dumps({"id": 1, "method": method, "params": params}))

                async def reply() -> dict:
                    while True:
                        message = json.loads(await ws.recv())
                        if message.get("id") == 1:
                            return message

                answer = await asyncio.wait_for(reply(), _CDP_TIMEOUT)
            if "error" in answer:
                logger.debug("cdp %s on %s answered %s", method, port, answer["error"])
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("cdp %s on cdp=%s did not complete: %r", method, port, exc)
            return False

    async def _stop_downloading(self, port: int, guids: list[str]) -> None:
        """Cancel what is still arriving, then point the browser's downloads at
        nothing.

        The second half matters as much as the first. agent-browser sets the
        browser-wide download folder to the directory of the path it was given
        and never sets it back, and Chromium re-creates that folder if it is
        missing — measured: a plain `click` on a download link after one
        `download` wrote a GUID-named file into the deleted landing directory.
        So after every `download`, the browser is told to refuse downloads until
        the next `download` sets its own folder again. That also means no plain
        `click` can ever write to this volume.
        """
        for guid in guids:
            await self._browser_command(port, "Browser.cancelDownload", {"guid": guid})
        await self._browser_command(port, "Browser.setDownloadBehavior", {"behavior": "deny"})

    async def _download(self, inst, command: str, selector: str,
                        subject: str | None) -> DriveOutcome:
        """Click `selector`, save what it downloads, and keep it for the caller.

        The order in the `finally` is deliberate: cancel and deny BEFORE the
        landing directory is removed, so nothing the browser is still writing
        re-creates it after the store has let it go.
        """
        if self._downloads is None:
            raise AgentBrowserError("this server cannot keep downloads")
        port = inst.cdp_port
        try:
            landing = await self._downloads.begin()
        except downloads_service.DownloadsError as exc:
            return DriveOutcome(inst.id, command, False, str(exc), None)

        async def outgrown() -> bool:
            size, partial = await asyncio.to_thread(self._downloads.landed_bytes, landing)
            if size <= landing.allowance:
                return False
            # Cancel in the browser BEFORE the CLI is killed: the native CLI
            # exits once its download is cancelled, and until it does it holds
            # the pipes the kill is waiting on.
            for guid in partial:
                await self._browser_command(port, "Browser.cancelDownload", {"guid": guid})
            return True

        names = _SuggestedName(getattr(inst, "context", None))
        failure: str | None = None
        rc, out, err = 1, "", ""
        try:
            try:
                rc, out, err = await self._run_resilient(
                    port, ["download", selector, str(landing.target)],
                    timeout=_DOWNLOAD_TIMEOUT, watch=outgrown,
                )
            except _Stopped:
                failure = str(self._downloads.too_large(landing))
            except TimeoutError:
                failure = (
                    f"the download did not finish within {int(_DOWNLOAD_TIMEOUT)}s and was "
                    "cancelled. If the element only opens a page, navigate to that page "
                    "first."
                )
            finally:
                names.stop()
                # Whatever is still partial: a timeout's download, or one the
                # watchdog's cancel did not reach. Cancelling twice is harmless.
                partial = (await asyncio.to_thread(self._downloads.landed_bytes, landing))[1]
                await self._stop_downloading(port, partial)
        except BaseException:
            # Anything unplanned — the CLI could not even be spawned, the call
            # was cancelled — still gives the landing directory and its
            # reservation back.
            self._downloads.abort(landing)
            raise
        if failure is None and rc != 0:
            failure = out.strip() or err.strip() or "the download failed"
        if failure is not None:
            self._downloads.abort(landing)
            logger.info("agent_browser %s 'download' failed", inst.id)
            return DriveOutcome(inst.id, command, False, failure, None)

        try:
            kept = await self._downloads.commit(
                landing, subject=subject or OWNER, secret=self._secret(),
                suggested_name=names.name, source_url=names.url,
            )
        except downloads_service.DownloadsError as exc:
            return DriveOutcome(inst.id, command, False, str(exc), None)
        logger.info("agent_browser %s 'download' kept %s", inst.id, kept.handle)
        return DriveOutcome(
            inst.id, command, True,
            f"downloaded {kept.name} ({kept.bytes} bytes, {kept.content_type})",
            None, download=kept,
        )

    async def drive(self, instance_id: str, command: str, *,
                    subject: str | None = OWNER) -> DriveOutcome:
        """Run one allow-listed action against the instance.

        Text by default: a screenshot is returned ONLY for the explicit
        `screenshot` verb (opt-in), so a `read`/`get`/`snapshot` doesn't spend the
        user tokens on a PNG they didn't ask for. Parsing/allow-listing happens
        before the instance is even resolved, so a disallowed command is refused
        without regard to who asked."""
        argv = parse_command(command)
        inst = self._drivable(instance_id, subject)
        port = inst.cdp_port

        # `screenshot` is intercepted, never passed through: the service captures
        # to its own path with only the whitelisted display flags, so agent-browser
        # never receives a caller-chosen path.
        # `upload` is intercepted for the same reason: agent-browser would take the
        # caller's paths verbatim into setInputFiles. What goes into argv is what
        # resolve_for returned from a ticket manifest — reclaim.removable_child's
        # rule, applied to reads: "callers use the Path returned here, never the
        # one they passed in."
        if argv[0] == "upload":
            argv = self._staged_upload(argv, subject)

        # `download` is intercepted like `screenshot`: the caller named an
        # element, the service names the file, and what comes back is a ticket
        # for the finished file rather than a path on this disk.
        if argv[0] == "download":
            return await self._download(inst, command, argv[1], subject)

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
            rc, out, err = await self._run_resilient(port, argv, timeout=_RUN_TIMEOUT)
        except TimeoutError as exc:
            logger.warning("agent_browser %s %r timed out", instance_id, argv[0])
            return DriveOutcome(instance_id, command, False, str(exc), None)
        ok = rc == 0
        output = (out.strip() or err.strip() or ("done" if ok else "failed"))
        if argv[0] == "upload" and not ok:
            output = _with_upload_hint(output)
        logger.info("agent_browser %s %r rc=%s", instance_id, argv[0], rc)
        return DriveOutcome(instance_id, command, ok, output, None)
