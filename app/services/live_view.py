"""The in-chat live view: what a running browser looks like right now.

A chat client that renders MCP Apps (Claude, ChatGPT) shows a small panel next
to `create_instance` / `show_browser`. The panel polls the app-only `live_view`
tool about once a second; this service is what answers it.

**Where the pixels come from.** agent-browser runs a WebSocket frame server for
every session (`stream status` reports its port): CDP screencast frames as JPEG,
plus URL and tab messages. It only screencasts while a client is connected, so
this service connects only while somebody is watching and drops the connection
once the panel stops asking (`IDLE_SEC`). The stream is a localhost port inside
the container and never leaves it — the panel gets frames through MCP, which is
the one channel every host allows by default.

**Watching only.** The same stream accepts input events; nothing here ever
sends one. Taking control is a deliberate step on the dashboard (`control_url`),
behind the login and the existing "Take control" switch, never from the chat.

**Watching is not using.** Polling never touches the instance, so a panel left
open does not keep an idle browser alive past its idle reap.

The activity list and the files come from AgentBrowserService's listener, not
from the stream: every agent action already passes through `drive`, which is
the one place that knows the verb, whether it worked, and what a download was
kept as. The labels are written for the person watching and deliberately never
include what was typed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import posixpath
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import websockets

from .agent_browser import InstanceNotDrivable
from .tokens import OWNER

logger = logging.getLogger("cloakbiz.live_view")

# The panel polls about once a second, so two frames a second is all it can use.
MAX_FPS = 2
# Nobody has asked for this long: stop screencasting.
IDLE_SEC = 30.0
# How long one read waits before re-checking whether anyone is still watching.
_RECV_TIMEOUT = 5.0
# Between reconnect attempts when the stream is not up (yet) or dropped:
# doubling from the first to the last, and back to the first once a frame
# arrives. Each attempt may start an `agent-browser` process, so a stream that
# never comes up must not cost one a second for the life of the browser.
_RETRY_SEC = 1.0
_RETRY_MAX_SEC = 30.0
# A single stream message: a JPEG frame, base64. Generous; frames are ~50-100 KB.
_MAX_MESSAGE = 8 * 1024 * 1024
ACTIVITY_MAX = 30
FILES_MAX = 20
# Mirrors instances._IDLE_TTL_MIN: an interactive browser nobody drives closes then.
_IDLE_REAP_SEC = 15 * 60

Connect = Callable[..., Any]


@dataclass
class _Frame:
    jpeg: bytes
    frame_id: str
    width: int | None
    height: int | None
    captured_at: float | None


@dataclass
class _Watch:
    instance_id: str
    subject: str
    last_asked: float
    task: asyncio.Task | None = None
    frame: _Frame | None = None
    url: str = ""
    title: str = ""
    connected: bool = False


@dataclass
class _Activity:
    at: float
    text: str
    ok: bool


@dataclass
class _Trail:
    """What happened in one browser, kept whether or not anyone is watching."""
    activity: deque = field(default_factory=lambda: deque(maxlen=ACTIVITY_MAX))
    files: deque = field(default_factory=lambda: deque(maxlen=FILES_MAX))
    # The page title a navigation reported, and the address it was for. The
    # stream's tab messages carry an address-like label rather than the title.
    page_url: str = ""
    page_title: str = ""


# Verbs whose output starts with "✓ <page title>" then the address it landed on.
_LANDS_ON_A_PAGE = frozenset({"navigate", "open", "back", "forward", "reload"})


class LiveViewService:
    def __init__(self, instances, agent_browser, *,
                 connect: Connect = websockets.connect,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self._instances = instances
        self._agent_browser = agent_browser
        self._connect = connect
        self._clock = clock
        self._wall = wall
        self._watches: dict[str, _Watch] = {}
        self._trails: dict[str, _Trail] = {}
        agent_browser.add_listener(self._heard)

    # ── the listener ───────────────────────────────────────────────────────
    def _heard(self, instance_id: str, argv: list[str], outcome) -> None:
        # Browsers that closed without anybody watching never reach _forget;
        # their trails go here instead, so this cannot grow without bound.
        self._prune(keep=instance_id)
        trail = self._trails.setdefault(instance_id, _Trail())
        trail.activity.append(_Activity(self._wall(), describe(argv, outcome), outcome.ok))
        kept = getattr(outcome, "download", None)
        if kept is not None:
            trail.files.append(kept)
        if outcome.ok and argv and argv[0] in _LANDS_ON_A_PAGE:
            title, url = _landed(outcome.output)
            if url:
                trail.page_url, trail.page_title = url, title

    # ── the tool's answer ──────────────────────────────────────────────────
    async def state(self, instance_id: str, subject: str | None, *,
                    since: str = "") -> tuple[dict[str, Any], bytes | None]:
        """The panel's view of one browser, and the frame if it changed.

        Returns (state, jpeg). `jpeg` is None when the frame is the one the
        panel already has (`since`), or when there is none yet.
        """
        subject = subject or OWNER
        self._prune(keep=instance_id)
        inst = self._instances.get(instance_id)
        if inst is None:
            self._forget(instance_id)
            return {"instance_id": instance_id, "status": "closed"}, None
        self._check(inst, subject)

        watch = self._watches.get(instance_id)
        if watch is None or watch.subject != subject:
            watch = _Watch(instance_id, subject, self._clock())
            self._watches[instance_id] = watch
        watch.last_asked = self._clock()
        if watch.task is None or watch.task.done():
            watch.task = asyncio.create_task(self._pump(watch))

        frame = watch.frame
        state: dict[str, Any] = {
            "instance_id": instance_id,
            # Live means the stream is connected. A blank page draws nothing, so
            # "live" with no frame yet is normal and the panel says so.
            "status": "live" if watch.connected else "connecting",
            "url": display_url(watch.url),
            "title": self._title(instance_id, watch),
            "closes_at": self._closes_at(inst),
            "activity": self._activity(instance_id),
            "files": self._files(instance_id),
        }
        jpeg = None
        if frame is not None:
            state.update(frame_id=frame.frame_id, frame_width=frame.width,
                         frame_height=frame.height, captured_at=frame.captured_at)
            if frame.frame_id != since:
                jpeg = frame.jpeg
        return state, jpeg

    def _title(self, instance_id: str, watch: _Watch) -> str:
        trail = self._trails.get(instance_id)
        if trail and trail.page_title and _same_page(trail.page_url, watch.url):
            return trail.page_title
        title = watch.title
        # agent-browser labels a tab with its address until something names it;
        # repeating the address as a title says nothing, so the panel falls back.
        if not title or _strip_scheme(title) in _strip_scheme(watch.url):
            return ""
        return title

    def _check(self, inst, subject: str) -> None:
        # The same two refusals driving makes, so the view cannot reach further
        # than agent_browser already can: a sweep's browser is its own, and
        # another subject's browser is not ours to look at.
        if getattr(inst, "origin", None) == "task":
            raise InstanceNotDrivable(
                f"instance {inst.id!r} belongs to a running sweep; watch it from the dashboard"
            )
        owner = getattr(inst, "subject", None) or OWNER
        if owner != subject:
            raise InstanceNotDrivable(f"instance {inst.id!r} belongs to another subject")

    def _closes_at(self, inst) -> float | None:
        try:
            hard = inst.created_wall + inst.ttl_min * 60
        except (AttributeError, TypeError):
            return None
        if getattr(inst, "origin", None) != "interactive":
            return hard
        try:
            idle = self._wall() + max(0.0, _IDLE_REAP_SEC - inst.idle_sec())
        except (AttributeError, TypeError):
            return hard
        return min(hard, idle)

    def _activity(self, instance_id: str) -> list[dict[str, Any]]:
        trail = self._trails.get(instance_id)
        if trail is None:
            return []
        return [{"at": a.at, "text": a.text, "ok": a.ok} for a in trail.activity]

    def _files(self, instance_id: str) -> list[Any]:
        """The downloads this browser made that can still be fetched, newest first.

        Returned as the store's KeptDownload records; the façade turns them into
        links, because only it knows the address this server is reachable at.
        """
        trail = self._trails.get(instance_id)
        if trail is None:
            return []
        now = self._wall()
        return [k for k in reversed(trail.files) if getattr(k, "expires_at", 0) > now]

    # ── the stream ─────────────────────────────────────────────────────────
    def _idle(self, watch: _Watch) -> bool:
        return self._clock() - watch.last_asked > IDLE_SEC

    async def _pump(self, watch: _Watch) -> None:
        """Hold the stream open while somebody is watching, and no longer."""
        delay = _RETRY_SEC
        port: int | None = None
        try:
            while not self._idle(watch) and self._instances.get(watch.instance_id):
                if port is None:
                    try:
                        port = await self._agent_browser.stream_port(
                            watch.instance_id, watch.subject)
                    except InstanceNotDrivable:
                        return
                    except OSError as exc:
                        logger.debug("live view could not ask for %s's stream: %r",
                                     watch.instance_id, exc)
                if port is not None:
                    got_frame = await self._stream(watch, port)
                    if got_frame:
                        delay = _RETRY_SEC
                    else:
                        # Connected to nothing useful: ask agent-browser afresh next
                        # time, in case the daemon restarted on another port.
                        port = None
                if self._idle(watch) or not self._instances.get(watch.instance_id):
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken view must not take anything else down
            logger.exception("live view for %s stopped", watch.instance_id)
        # The watch itself stays: its last frame, address and title are what the
        # panel shows while the next connection comes up, instead of a blank.
        # It goes with the browser (_forget, _prune).

    async def _stream(self, watch: _Watch, port: int) -> bool:
        """One connection. True if at least one frame came through it."""
        before = watch.frame
        try:
            async with self._connect(
                f"ws://127.0.0.1:{port}/?maxFps={MAX_FPS}",
                max_size=_MAX_MESSAGE, open_timeout=5,
            ) as ws:
                watch.connected = True
                await self._read(ws, watch)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            logger.debug("live view stream for %s dropped: %r", watch.instance_id, exc)
        finally:
            watch.connected = False
        return watch.frame is not before

    async def _read(self, ws, watch: _Watch) -> None:
        while not self._idle(watch):
            try:
                raw = await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT)
            except asyncio.TimeoutError:
                if not self._instances.get(watch.instance_id):
                    return
                continue
            self._apply(watch, raw)

    def _apply(self, watch: _Watch, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        if kind == "frame":
            try:
                jpeg = base64.b64decode(msg.get("data") or "", validate=True)
            except (ValueError, TypeError):
                return
            if not jpeg:
                return
            meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            stamp = meta.get("timestamp")
            watch.frame = _Frame(
                jpeg=jpeg,
                frame_id=hashlib.sha256(jpeg).hexdigest()[:16],
                width=_int(meta.get("deviceWidth")),
                height=_int(meta.get("deviceHeight")),
                captured_at=stamp / 1000 if isinstance(stamp, (int, float)) else None,
            )
        elif kind == "url":
            url = msg.get("url")
            if isinstance(url, str):
                watch.url = url
        elif kind == "tabs":
            tabs = msg.get("tabs")
            if isinstance(tabs, list):
                active = next((t for t in tabs if isinstance(t, dict) and t.get("active")), None)
                if active:
                    if isinstance(active.get("url"), str):
                        watch.url = active["url"]
                    if isinstance(active.get("title"), str):
                        watch.title = active["title"]

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _prune(self, keep: str | None = None) -> None:
        """Drop what is kept for browsers that have closed."""
        for gone in [i for i in {*self._trails, *self._watches}
                     if i != keep and not self._instances.get(i)]:
            self._forget(gone)

    def _forget(self, instance_id: str) -> None:
        watch = self._watches.pop(instance_id, None)
        if watch and watch.task and not watch.task.done():
            watch.task.cancel()
        self._trails.pop(instance_id, None)

    async def close(self) -> None:
        tasks = [w.task for w in self._watches.values() if w.task and not w.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        self._watches.clear()


def _landed(output: str) -> tuple[str, str]:
    """(title, url) from a navigation's output: "✓ <title>" then "<url>"."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].startswith("✓"):
        return "", ""
    return lines[0].lstrip("✓").strip()[:200], lines[1]


def _strip_scheme(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/")


def _same_page(a: str, b: str) -> bool:
    return bool(a) and _strip_scheme(a) == _strip_scheme(b)


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ── activity labels ─────────────────────────────────────────────────────────
# Written for the person watching. Never the text that was typed, the option
# that was chosen, or a single character pressed: a password is typed through
# exactly these verbs, and the panel is a screen someone might share.
_NAMED_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "Insert",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Home", "End", "PageUp", "PageDown",
    *(f"F{n}" for n in range(1, 13)),
})
_MODIFIERS = frozenset({"Control", "Shift", "Alt", "Meta"})


def _is_named_key(key: str) -> bool:
    """Enter, Tab, Control+a… — a key worth naming. Anything else is typing,
    and `press` accepts arbitrary strings, so it is not repeated."""
    *mods, last = key.split("+") if key else [""]
    if mods and all(m in _MODIFIERS for m in mods):
        # Control+a is a shortcut; Shift+a is a capital A.
        return last in _NAMED_KEYS or (len(last) == 1 and set(mods) - {"Shift"} != set())
    return not mods and last in _NAMED_KEYS


def describe(argv: list[str], outcome) -> str:
    verb = argv[0] if argv else ""
    args = argv[1:]
    text = _describe(verb, args, outcome)
    return text if outcome.ok else f"{text} — failed"


def _describe(verb: str, args: list[str], outcome) -> str:
    if verb in ("navigate", "open"):
        return f"Opened {_short_url(args[0])}" if args else "Opened a page"
    if verb == "back":
        return "Went back"
    if verb == "forward":
        return "Went forward"
    if verb == "reload":
        return "Reloaded the page"
    if verb == "snapshot":
        return "Looked at the page's controls"
    if verb == "read":
        return "Read the page"
    if verb == "get":
        what = args[0] if args else ""
        return {"url": "Checked the address", "title": "Checked the title"}.get(
            what, "Read part of the page")
    if verb in ("click", "dblclick"):
        return f"Clicked {_target(args)}"
    if verb == "hover":
        return f"Hovered over {_target(args)}"
    if verb in ("fill", "type"):
        # With one argument there is no selector: that argument IS the text.
        return f"Typed into {_target(args) if len(args) >= 2 else 'the page'}"
    if verb == "select":
        return f"Chose an option in {_target(args) if len(args) >= 2 else 'a list'}"
    if verb == "press":
        key = args[0] if args else ""
        return f"Pressed {key}" if _is_named_key(key) else "Pressed a key"
    if verb == "scroll":
        direction = args[0] if args and args[0] in ("up", "down", "left", "right") else ""
        return f"Scrolled {direction}".strip()
    if verb == "wait":
        return "Waited for the page"
    if verb == "screenshot":
        return "Took a screenshot"
    if verb == "upload":
        names = [posixpath.basename(a) for a in args[1:] if a]
        return f"Attached {', '.join(names)}" if names else "Attached a file"
    if verb == "download":
        kept = getattr(outcome, "download", None)
        return f"Downloaded {kept.name}" if kept is not None else "Tried a download"
    return verb.capitalize() or "Acted on the page"


def _target(args: list[str]) -> str:
    """A ref like @e3 means nothing to a person; a selector sometimes does."""
    if not args or args[0].startswith("@"):
        return "an element"
    selector = args[0]
    if selector.startswith("text="):
        selector = selector[5:]
    selector = selector.strip("\"'")
    return f"“{selector[:40]}”" if selector else "an element"


# A path segment this long is more likely a token (reset links, magic links,
# signed paths) than a word, and the panel is a screen someone might share.
_LONG_SEGMENT = 24


def display_url(url: str) -> str:
    """An address for the panel's header: scheme, host and path, with no
    credentials, query or fragment, and long path segments elided."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.hostname:
        return url if url in ("", "about:blank") else (f"{parts.scheme}:" if parts.scheme else "")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{_readable_path(parts.path)}"


def _readable_path(path: str) -> str:
    if path in ("", "/"):
        return "" if not path else "/"
    segments = [s if len(unquote(s)) <= _LONG_SEGMENT else "…" for s in path.split("/")]
    return unquote("/".join(segments))


def _short_url(url: str) -> str:
    """Host and path, decoded for reading; never the query, which is where
    tokens live. A page with no host (data:, about:, file:) is named by its
    scheme rather than echoed — a data: URL is the whole document."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "a page"
    host = parts.hostname or ""
    if not host:
        return f"a {parts.scheme}: page" if parts.scheme else "a page"
    path = _readable_path(parts.path) if parts.path not in ("", "/") else ""
    shown = host + path
    return shown if len(shown) <= 60 else shown[:59] + "…"
