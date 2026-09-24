"""The in-chat live view: the service behind `live_view`, its MCP surface, and
the dashboard link it hands out.

The stream is faked at the `connect` seam, so these run without a browser; the
live path (a real CloakBrowser, agent-browser's stream) was checked by hand
against a container and is described in the PR.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import live_view as lv
from app.services.agent_browser import DriveOutcome, InstanceNotDrivable
from app.services.live_view import LiveViewService, describe
from conftest import mint_access

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
JPEG2 = b"\xff\xd8\xff\xe0another-frame"


# ── fakes ───────────────────────────────────────────────────────────────────
@dataclass
class Inst:
    id: str = "i1"
    origin: str = "interactive"
    subject: str | None = "owner"
    created_wall: float = 1_000.0
    ttl_min: int = 60
    idle: float = 0.0

    def idle_sec(self) -> float:
        return self.idle


class Instances:
    def __init__(self, *insts: Inst) -> None:
        self.running = {i.id: i for i in insts}

    def get(self, iid):
        return self.running.get(iid)


class Driver:
    """The two things LiveViewService asks of AgentBrowserService."""

    def __init__(self, port: int | None = 4000) -> None:
        self.port = port
        self.listeners: list = []
        self.asked = 0

    def add_listener(self, fn) -> None:
        self.listeners.append(fn)

    async def stream_port(self, instance_id, subject):
        self.asked += 1
        return self.port


class FakeSocket:
    def __init__(self, messages: list, *, hold: bool = True) -> None:
        self.messages = [m if isinstance(m, str) else json.dumps(m) for m in messages]
        self.sent: list = []
        self.hold = hold
        self.closed = False

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if not self.hold:
            raise OSError("stream closed")
        await asyncio.sleep(3600)

    async def send(self, data):  # pragma: no cover - asserted never to happen
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


@dataclass
class Connector:
    sockets: list = field(default_factory=list)
    urls: list = field(default_factory=list)

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        return self.sockets.pop(0) if self.sockets else FakeSocket([], hold=False)


def frame(data: bytes = JPEG, w: int = 1024, h: int = 576) -> dict:
    return {"type": "frame", "seq": 1, "data": base64.b64encode(data).decode(),
            "metadata": {"deviceWidth": w, "deviceHeight": h, "timestamp": 1_700_000_000_000}}


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def settle(rounds: int = 5) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def outcome(ok=True, download=None) -> DriveOutcome:
    return DriveOutcome("i1", "x", ok, "", None, download=download)


# ── activity labels ─────────────────────────────────────────────────────────
class TestTheLabelsNeverRepeatWhatWasTyped:
    """The panel is a screen someone might share, and passwords are typed
    through exactly these verbs."""

    def test_fill_and_type_name_the_field_not_the_text(self):
        for verb in ("fill", "type"):
            text = describe([verb, "@e3", "hunter2"], outcome())
            assert text == "Typed into an element"
            assert "hunter2" not in text

    def test_a_single_key_is_typing(self):
        assert describe(["press", "a"], outcome()) == "Pressed a key"
        assert describe(["press", "Enter"], outcome()) == "Pressed Enter"

    def test_a_chosen_option_is_not_shown(self):
        assert "gold" not in describe(["select", "@e2", "gold"], outcome())

    def test_navigation_shows_where_but_not_the_query(self):
        text = describe(["navigate", "https://example.com/login?token=abc"], outcome())
        assert text == "Opened example.com/login"

    def test_upload_shows_file_names_only(self):
        text = describe(["upload", "@e1", "/data/uploads/up_x/resume.pdf"], outcome())
        assert text == "Attached resume.pdf"

    def test_a_failure_says_so(self):
        assert describe(["click", "@e9"], outcome(ok=False)) == "Clicked an element — failed"

    def test_a_readable_selector_is_kept(self):
        assert describe(["click", "text=Search"], outcome()) == "Clicked “Search”"


# ── the service ─────────────────────────────────────────────────────────────
@pytest.fixture
def clock():
    return Clock()


def service(instances, driver, connector, clock, wall=lambda: 5_000.0):
    return LiveViewService(instances, driver, connect=connector, clock=clock, wall=wall)


@pytest.mark.asyncio
class TestFrames:
    async def test_first_call_starts_the_stream_and_a_frame_arrives(self, clock):
        sock = FakeSocket([{"type": "tabs", "tabs": [{"active": True, "url": "https://a.test/",
                                                      "title": "A"}]}, frame()])
        conn = Connector([sock])
        svc = service(Instances(Inst()), Driver(), conn, clock)

        state, jpeg = await svc.state("i1", "owner")
        assert state["status"] == "connecting" and jpeg is None
        await settle()

        state, jpeg = await svc.state("i1", "owner")
        assert state["status"] == "live"
        assert (state["url"], state["title"]) == ("https://a.test/", "A")
        assert jpeg == JPEG and (state["frame_width"], state["frame_height"]) == (1024, 576)
        assert conn.urls == ["ws://127.0.0.1:4000/?maxFps=2"]
        await svc.close()

    async def test_the_same_frame_is_not_sent_twice(self, clock):
        svc = service(Instances(Inst()), Driver(), Connector([FakeSocket([frame()])]), clock)
        await svc.state("i1", "owner")
        await settle()
        state, jpeg = await svc.state("i1", "owner")
        assert jpeg == JPEG
        _, again = await svc.state("i1", "owner", since=state["frame_id"])
        assert again is None
        await svc.close()

    async def test_nothing_is_ever_sent_into_the_stream(self, clock):
        """The stream accepts input events. Watching must never produce one."""
        sock = FakeSocket([frame(), {"type": "url", "url": "https://b.test/"}, frame(JPEG2)])
        svc = service(Instances(Inst()), Driver(), Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        assert sock.sent == []
        await svc.close()

    async def test_junk_messages_are_ignored(self, clock):
        sock = FakeSocket(["not json", "[1,2]", {"type": "frame", "data": "%%%"},
                           {"type": "tabs", "tabs": "nope"}, frame()])
        svc = service(Instances(Inst()), Driver(), Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        _, jpeg = await svc.state("i1", "owner")
        assert jpeg == JPEG
        await svc.close()

    async def test_the_last_picture_survives_a_dropped_stream(self, clock):
        first = FakeSocket([{"type": "url", "url": "https://kept.test/"}, frame()], hold=False)
        svc = service(Instances(Inst()), Driver(), Connector([first]), clock)
        await svc.state("i1", "owner")
        await settle()
        state, jpeg = await svc.state("i1", "owner")
        assert state["url"] == "https://kept.test/" and jpeg == JPEG


@pytest.mark.asyncio
class TestWhoMayWatch:
    async def test_a_sweeps_browser_is_refused(self, clock):
        svc = service(Instances(Inst(origin="task", subject=None)), Driver(), Connector(), clock)
        with pytest.raises(InstanceNotDrivable):
            await svc.state("i1", "owner")

    async def test_another_subjects_browser_is_refused(self, clock):
        svc = service(Instances(Inst(subject="someone-else")), Driver(), Connector(), clock)
        with pytest.raises(InstanceNotDrivable):
            await svc.state("i1", "owner")

    async def test_a_closed_browser_reads_closed_not_error(self, clock):
        svc = service(Instances(), Driver(), Connector(), clock)
        state, jpeg = await svc.state("gone", "owner")
        assert state == {"instance_id": "gone", "status": "closed"} and jpeg is None


@pytest.mark.asyncio
class TestWatchingEnds:
    async def test_the_stream_closes_once_nobody_asks(self, clock, monkeypatch):
        monkeypatch.setattr(lv, "_RECV_TIMEOUT", 0.01)
        sock = FakeSocket([frame()])
        svc = service(Instances(Inst()), Driver(), Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        clock.t += lv.IDLE_SEC + 1
        await asyncio.sleep(0.05)
        assert sock.closed

    async def test_watching_does_not_keep_the_browser_alive(self, clock):
        inst = Inst()
        inst.touch = lambda: (_ for _ in ()).throw(AssertionError("touched"))
        svc = service(Instances(inst), Driver(), Connector([FakeSocket([frame()])]), clock)
        await svc.state("i1", "owner")
        await settle()
        await svc.state("i1", "owner")
        await svc.close()

    async def test_closes_at_is_the_idle_reap_when_that_comes_first(self, clock):
        inst = Inst(created_wall=4_900.0, ttl_min=60, idle=14 * 60)
        svc = service(Instances(inst), Driver(port=None), Connector(), clock)
        state, _ = await svc.state("i1", "owner")
        assert state["closes_at"] == 5_000.0 + 60
        await svc.close()


@pytest.mark.asyncio
class TestActivityAndFiles:
    def _kept(self, name, expires_at):
        return SimpleNamespace(name=name, expires_at=expires_at)

    async def test_actions_and_downloads_are_kept_per_browser(self, clock):
        driver = Driver(port=None)
        svc = service(Instances(Inst()), driver, Connector(), clock)
        heard = driver.listeners[0]
        heard("i1", ["navigate", "https://a.test/x"], outcome())
        heard("i1", ["download", "@e4"], outcome(download=self._kept("report.pdf", 9_999.0)))
        heard("i1", ["download", "@e5"], outcome(download=self._kept("old.pdf", 10.0)))
        state, _ = await svc.state("i1", "owner")
        assert [a["text"] for a in state["activity"]] == [
            "Opened a.test/x", "Downloaded report.pdf", "Downloaded old.pdf"]
        assert [k.name for k in state["files"]] == ["report.pdf"]  # the expired one is gone
        await svc.close()

    async def test_a_closed_browsers_history_is_dropped(self, clock):
        driver = Driver(port=None)
        instances = Instances(Inst("i1"), Inst("i2"))
        svc = service(instances, driver, Connector(), clock)
        driver.listeners[0]("i1", ["reload"], outcome())
        del instances.running["i1"]
        driver.listeners[0]("i2", ["reload"], outcome())
        assert "i1" not in svc._trails
        await svc.close()


# ── the MCP surface ─────────────────────────────────────────────────────────
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "test-secret-value-long-enough")
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        c.headers["Authorization"] = f"Bearer {mint_access(app)}"
        yield c


def rpc(client, method, params=None):
    return client.post("/mcp", headers=HEADERS,
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()


class TestTheAppSurface:
    def test_the_view_is_bound_to_the_right_tools(self, mcp):
        tools = {t["name"]: t for t in rpc(mcp, "tools/list")["result"]["tools"]}
        uri = "ui://cloak-biz-scraper/live-view.html"
        for name in ("create_instance", "show_browser"):
            assert tools[name]["_meta"]["ui"] == {"resourceUri": uri}
        assert tools["live_view"]["_meta"]["ui"] == {"resourceUri": uri, "visibility": ["app"]}
        # Nothing else grew a view.
        assert {n for n, t in tools.items() if (t.get("_meta") or {}).get("ui")} == {
            "create_instance", "show_browser", "live_view"}

    def test_the_page_is_self_contained(self, mcp):
        """A host's default sandbox allows no network at all: every script and
        style must be inline, and the bridge must be in the page."""
        result = rpc(mcp, "resources/read", {"uri": "ui://cloak-biz-scraper/live-view.html"})
        page = result["result"]["contents"][0]
        assert page["mimeType"] == "text/html;profile=mcp-app"
        html = page["text"]
        assert "McpApps" in html and "<!--EXT_APPS-->" not in html
        assert "<script src" not in html and "<link" not in html
        assert "wss://" not in html

    def test_polling_a_closed_browser_is_not_an_error(self, mcp):
        result = rpc(mcp, "tools/call", {"name": "live_view",
                                         "arguments": {"instance_id": "nope"}})["result"]
        assert result.get("isError") is not True
        assert result["structuredContent"] == {"instance_id": "nope", "status": "closed"}

    def test_show_browser_refuses_an_unknown_browser_in_words(self, mcp):
        result = rpc(mcp, "tools/call", {"name": "show_browser",
                                         "arguments": {"instance_id": "nope"}})["result"]
        assert result["isError"] is True
        assert "No running browser" in result["content"][0]["text"]

    def test_a_frame_goes_out_as_an_image_and_the_link_needs_no_token(self, mcp, monkeypatch):
        async def fake_state(instance_id, subject, *, since=""):
            return {"instance_id": instance_id, "status": "live", "files": [],
                    "frame_id": "f1"}, JPEG

        monkeypatch.setattr(app.state.live_view, "state", fake_state)
        result = rpc(mcp, "tools/call", {"name": "live_view",
                                         "arguments": {"instance_id": "i1"}})["result"]
        image = next(c for c in result["content"] if c["type"] == "image")
        assert base64.b64decode(image["data"]) == JPEG and image["mimeType"] == "image/jpeg"
        control = result["structuredContent"]["control_url"]
        assert control == "https://testserver/?view=browsers&instance=i1"


# ── the dashboard link and the login in front of it ─────────────────────────
class TestReturnTo:
    @pytest.mark.parametrize("value,expected", [
        ("/?view=browsers&instance=abc123", "/?view=browsers&instance=abc123"),
        ("/?instance=abc123", "/?instance=abc123"),
        ("/?view=browsers&instance=abc123&extra=1", "/?view=browsers&instance=abc123"),
        ("/?view=nowhere", "/"),
        ("/", "/"),
        ("/?instance=../../x", "/"),  # a malformed id is dropped, the path is fine
    ])
    def test_a_dashboard_link_is_rebuilt(self, value, expected):
        from app.routes.ui import return_to
        assert return_to(value) == expected

    @pytest.mark.parametrize("value", [
        "https://evil.example/", "//evil.example/", "/\\evil.example", "/settings",
        "javascript:alert(1)", "", None,
    ])
    def test_anything_else_goes_nowhere(self, value):
        from app.routes.ui import return_to
        assert return_to(value) is None


SECRET = "test-secret-value-long-enough"


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


class TestTheTakeControlLink:
    def test_signed_out_it_goes_through_login_and_comes_back(self, web):
        r = web.get("/?view=browsers&instance=abc123")
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2F%3Fview%3Dbrowsers%26instance%3Dabc123"
        page = web.get(r.headers["location"])
        assert 'name="next" value="/?view=browsers&amp;instance=abc123"' in page.text
        signed_in = web.post("/login", data={"secret": SECRET,
                                             "next": "/?view=browsers&instance=abc123"})
        assert signed_in.headers["location"] == "/?view=browsers&instance=abc123"

    def test_a_foreign_next_lands_on_the_dashboard(self, web):
        r = web.post("/login", data={"secret": SECRET, "next": "https://evil.example/"})
        assert r.headers["location"] == "/"

    def test_a_plain_signed_out_visit_is_unchanged(self, web):
        assert web.get("/").headers["location"] == "/login"

    def test_the_named_browser_is_opened_and_marked(self, web, monkeypatch):
        web.post("/login", data={"secret": SECRET})
        from app.services.views import instance_view  # noqa: F401 - the page renders it

        fake = SimpleNamespace(
            id="abc123", origin="interactive", subject="owner", profile="Default",
            proxy_ip=None, timezone=None, locale=None, geoip=True, humanize=True,
            ttl_min=60, created_wall=1_700_000_000.0, vnc_port=None,
            age_sec=lambda: 1.0, idle_sec=lambda: 1.0,
        )
        app.state.instances.running["abc123"] = fake
        try:
            page = web.get("/?view=browsers&instance=abc123").text
        finally:
            del app.state.instances.running["abc123"]
        assert 'class="acc-item open focused" id="browser-abc123"' in page


class TestPageNames:
    def test_a_page_with_no_host_is_named_by_its_scheme(self):
        page = "data:text/html,<body>secret document</body>"
        assert describe(["navigate", page], outcome()) == "Opened a data: page"

    def test_the_path_is_decoded_for_reading(self):
        assert describe(["navigate", "https://w.test/wiki/Special%3ASearch"], outcome()) == \
            "Opened w.test/wiki/Special:Search"

    @pytest.mark.asyncio
    async def test_the_title_comes_from_the_navigation(self, clock):
        sock = FakeSocket([{"type": "tabs", "tabs": [{"active": True, "url": "https://a.test/x",
                                                      "title": "a.test/x"}]}, frame()])
        driver = Driver()
        svc = service(Instances(Inst()), driver, Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        state, _ = await svc.state("i1", "owner")
        assert state["title"] == ""  # an address-like label is not a title
        driver.listeners[0]("i1", ["navigate", "https://a.test/x"],
                            DriveOutcome("i1", "x", True, "✓ The A page\n  https://a.test/x", None))
        state, _ = await svc.state("i1", "owner")
        assert state["title"] == "The A page"
        await svc.close()


# ── review round: what the panel must never show ────────────────────────────
class TestNothingTypedEverShows:
    @pytest.mark.parametrize("argv", [
        ["type", "hunter2pass"], ["fill", "hunter2pass"], ["select", "hunter2pass"],
        ["press", "hunter2"], ["press", "Shift+h"], ["press", "Enterx"],
    ])
    def test_a_lone_argument_is_never_repeated(self, argv):
        for ok in (True, False):
            assert "hunter2" not in describe(argv, outcome(ok=ok))
            assert "Enterx" not in describe(argv, outcome(ok=ok))

    @pytest.mark.parametrize("key", ["Enter", "Tab", "ArrowDown", "F5", "Control+a", "Meta+Shift+k"])
    def test_named_keys_and_shortcuts_are_named(self, key):
        assert describe(["press", key], outcome()) == f"Pressed {key}"


class TestTheAddressIsScrubbed:
    @pytest.mark.parametrize("url,shown", [
        ("https://user:pw@a.test/login?code=SECRET#frag", "https://a.test/login"),
        ("https://a.test/reset/0123456789abcdef0123456789abcdef", "https://a.test/reset/…"),
        ("http://a.test:8080/x", "http://a.test:8080/x"),
        ("about:blank", "about:blank"),
        ("data:text/html,<p>doc</p>", "data:"),
    ])
    def test_display_url(self, url, shown):
        assert lv.display_url(url) == shown

    def test_a_token_in_the_path_is_not_in_the_activity(self):
        text = describe(["navigate", "https://a.test/reset/0123456789abcdef0123456789abcdef"],
                        outcome())
        assert "0123456789" not in text

    @pytest.mark.asyncio
    async def test_the_state_carries_the_scrubbed_address(self, clock):
        sock = FakeSocket([{"type": "url", "url": "https://a.test/cb?code=SECRET"}, frame()])
        svc = service(Instances(Inst()), Driver(), Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        state, _ = await svc.state("i1", "owner")
        assert state["url"] == "https://a.test/cb" and "SECRET" not in json.dumps(state)
        await svc.close()


@pytest.mark.asyncio
class TestAStreamThatNeverComesUpBacksOff:
    async def test_each_retry_waits_twice_as_long_up_to_the_cap(self, clock, monkeypatch):
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            waits.append(delay)
            if len(waits) >= 8:
                clock.t += lv.IDLE_SEC + 1  # the panel went away
            await real_sleep(0)

        monkeypatch.setattr(lv.asyncio, "sleep", fake_sleep)
        driver = Driver(port=None)  # `stream status` never reports a port
        svc = service(Instances(Inst()), driver, Connector(), clock)
        await svc.state("i1", "owner")
        for _ in range(40):
            await real_sleep(0)
        assert waits[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        assert driver.asked == len(waits)
        await svc.close()

    async def test_the_pump_stops_when_the_browser_closes(self, clock, monkeypatch):
        monkeypatch.setattr(lv, "_RECV_TIMEOUT", 0.01)
        instances = Instances(Inst())
        sock = FakeSocket([frame()])
        svc = service(instances, Driver(), Connector([sock]), clock)
        await svc.state("i1", "owner")
        await settle()
        del instances.running["i1"]
        await asyncio.sleep(0.05)
        assert sock.closed and svc._watches["i1"].task.done()


# ── the agent-browser side ──────────────────────────────────────────────────
from app.services.agent_browser import AgentBrowserService  # noqa: E402


class _Inst:
    def __init__(self, iid="i1", origin="interactive", subject=None, cdp_port=4242):
        self.id, self.origin, self.subject, self.cdp_port = iid, origin, subject, cdp_port


class _Insts:
    def __init__(self, *insts):
        self.running = {i.id: i for i in insts}

    def get(self, iid):
        return self.running.get(iid)


@pytest.mark.asyncio
class TestStreamPort:
    async def _svc(self, monkeypatch, *, rc=0, out="", raises=None, inst=None):
        svc = AgentBrowserService(_Insts(inst or _Inst()))
        seen = []

        async def fake_run(port, argv, *, timeout, watch=None):
            seen.append((port, argv))
            if raises:
                raise raises
            return rc, out, ""

        monkeypatch.setattr(svc, "_run", fake_run)
        return svc, seen

    async def test_reads_the_port(self, monkeypatch):
        svc, seen = await self._svc(monkeypatch, out=json.dumps(
            {"success": True, "data": {"enabled": True, "port": 38335}}))
        assert await svc.stream_port("i1", "owner") == 38335
        assert seen == [(4242, ["stream", "status", "--json"])]

    @pytest.mark.parametrize("out", [
        json.dumps({"data": {"enabled": False, "port": 1}}),
        json.dumps({"data": {"enabled": True, "port": "1"}}),
        "not json", json.dumps([1]),
    ])
    async def test_anything_else_is_none(self, monkeypatch, out):
        svc, _ = await self._svc(monkeypatch, out=out)
        assert await svc.stream_port("i1", "owner") is None

    async def test_a_timeout_is_none(self, monkeypatch):
        svc, _ = await self._svc(monkeypatch, raises=TimeoutError("slow"))
        assert await svc.stream_port("i1", "owner") is None

    @pytest.mark.parametrize("inst", [_Inst(origin="task"), _Inst(subject="someone-else")])
    async def test_the_driving_rules_apply(self, monkeypatch, inst):
        svc, seen = await self._svc(monkeypatch, inst=inst)
        with pytest.raises(InstanceNotDrivable):
            await svc.stream_port("i1", "owner")
        assert seen == []

    async def test_it_waits_for_a_warm_up_in_flight(self, monkeypatch):
        svc, seen = await self._svc(monkeypatch, out=json.dumps(
            {"data": {"enabled": True, "port": 1}}))
        gate = asyncio.Event()
        svc._warming[4242] = gate
        asking = asyncio.create_task(svc.stream_port("i1", "owner"))
        await settle()
        assert seen == []  # not a second cold start alongside the warm-up
        gate.set()
        assert await asking == 1 and len(seen) == 1


@pytest.mark.asyncio
class TestDriveTellsItsListeners:
    async def _svc(self, monkeypatch):
        svc = AgentBrowserService(_Insts(_Inst()))

        async def fake_run(port, argv, *, timeout, watch=None):
            return 0, "done", ""

        monkeypatch.setattr(svc, "_run", fake_run)
        return svc

    async def test_an_action_is_reported_with_its_parsed_argv(self, monkeypatch):
        svc = await self._svc(monkeypatch)
        heard = []
        svc.add_listener(lambda iid, argv, out: heard.append((iid, argv, out.ok)))
        await svc.drive("i1", "click @e3", subject="owner")
        assert heard == [("i1", ["click", "@e3"], True)]

    async def test_a_refused_command_is_not_an_action(self, monkeypatch):
        from app.services.agent_browser import AgentBrowserError

        svc = await self._svc(monkeypatch)
        heard = []
        svc.add_listener(lambda *a: heard.append(a))
        with pytest.raises(AgentBrowserError):
            await svc.drive("i1", "state save /etc/x", subject="owner")
        with pytest.raises(InstanceNotDrivable):
            await svc.drive("nope", "click @e1", subject="owner")
        assert heard == []

    async def test_a_broken_listener_does_not_fail_the_action(self, monkeypatch):
        svc = await self._svc(monkeypatch)

        def boom(*a):
            raise RuntimeError("listener bug")

        svc.add_listener(boom)
        out = await svc.drive("i1", "reload", subject="owner")
        assert out.ok


@pytest.mark.asyncio
class TestTheRunner:
    async def test_a_process_that_exits_as_it_times_out_is_a_timeout_not_a_crash(
            self, monkeypatch):
        import app.services.agent_browser as ab

        class P:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                raise ProcessLookupError()

            async def wait(self):
                return 0

        async def fake_exec(*a, **k):
            return P()

        monkeypatch.setattr(ab.asyncio, "create_subprocess_exec", fake_exec)
        svc = AgentBrowserService(_Insts(_Inst()))
        with pytest.raises(TimeoutError, match="did not respond"):
            await svc._run(4242, ["get", "url"], timeout=0.01)

    async def test_the_stream_encoding_reaches_the_cli(self, monkeypatch):
        import app.services.agent_browser as ab

        seen = {}

        class P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*a, env=None, **k):
            seen.update(env or {})
            return P()

        monkeypatch.setattr(ab.asyncio, "create_subprocess_exec", fake_exec)
        await AgentBrowserService(_Insts(_Inst()))._run(4242, ["get", "url"], timeout=1)
        assert seen["AGENT_BROWSER_STREAM_QUALITY"] == "60"
        assert seen["AGENT_BROWSER_STREAM_MAX_WIDTH"] == "1024"


class TestTheToolRefusesLikeDriving:
    @pytest.mark.parametrize("origin,subject", [("task", None), ("interactive", "someone-else")])
    def test_refused_over_mcp(self, mcp, origin, subject):
        inst = SimpleNamespace(id="zz9", origin=origin, subject=subject)
        app.state.instances.running["zz9"] = inst
        try:
            result = rpc(mcp, "tools/call", {"name": "live_view",
                                             "arguments": {"instance_id": "zz9"}})["result"]
        finally:
            del app.state.instances.running["zz9"]
        assert result["isError"] is True
        assert "zz9" in result["content"][0]["text"]
        assert not any(c["type"] == "image" for c in result["content"])
