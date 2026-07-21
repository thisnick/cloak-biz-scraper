"""Numeric layout checks for the sidebar nav and the mobile keyboard affordance.

Nick can't eyeball these (he's away), and "looks aligned" is not a regression
guard. So this renders the real dashboard in a headless browser and asserts the
geometry with `getBoundingClientRect`/`getBBox`: a misalignment is a failing
test, not a matter of opinion.

The browser is optional infrastructure — if neither system Chrome nor a
Playwright-bundled Chromium is present, the module skips rather than fails, the
same way the other browser-dependent tests do. noVNC is an apt package that is
only in the Docker image, so its core import is stubbed here; none of these
checks need a real framebuffer.

Run directly (`python tests/test_layout.py <outdir>`) to also drop the
collapsed / expanded / mobile screenshots for a human to glance at.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app.main import app  # noqa: E402
from app.services.secret import SecretService  # noqa: E402
from app.services.settings import SettingsService  # noqa: E402

SECRET = "layout-secret-long-enough"
PAGE_URL = "https://dash.test/"

# A no-op stand-in for noVNC's RFB: the pane module imports it at load time, so
# without this the whole module errors and none of the keyboard wiring attaches.
# None of the layout checks touch a real socket.
STUB_RFB = """
export default class RFB {
  constructor(target, url){
    this._l={};
    // Record every connection so a test can prove WHICH pane got control:
    // the target element and the viewOnly it was set to.
    this._rec = {target, viewOnly: null};
    (window.__rfbLog = window.__rfbLog || []).push(this._rec);
  }
  disconnect(){}
  focus(){}
  blur(){}
  sendKey(){}
  addEventListener(t,f){ (this._l[t]=this._l[t]||[]).push(f); }
  set viewOnly(v){ this._rec.viewOnly = v; }
  set scaleViewport(v){} set background(v){}
}
"""


class _Fake:
    def __init__(self, iid, origin, vnc_port):
        self.id, self.origin, self.vnc_port, self.subject = iid, origin, vnc_port, None
        self.profile, self.proxy_ip = "p", "203.0.113.7"
        self.timezone, self.locale = "America/Los_Angeles", "en-US"
        self.geoip = self.humanize = True
        self.ttl_min, self.created_wall = 60, 1_700_000_000.0

    def age_sec(self):
        return 5.0

    def idle_sec(self):
        return 5.0


def _render_dashboard() -> str:
    """The real `/` HTML, signed in, with one interactive and one task browser so
    the badges, panes, and Take-control controls are all present."""
    import os

    from fastapi.testclient import TestClient

    prev = os.environ.get("APP_SECRET")
    os.environ["APP_SECRET"] = SECRET
    try:
        with TestClient(app, base_url="https://testserver") as c:
            d = pathlib.Path(tempfile.mkdtemp())
            app.state.settings = SettingsService(d / "s.json", d / ".dek")
            app.state.secret = SecretService()
            app.state.secret.bootstrap()

            async def _noop(iid):
                return True

            app.state.instances.stop = _noop
            app.state.instances.running = {
                "i1": _Fake("i1", "interactive", 6100),
                "j9": _Fake("j9", "task", 6101),
            }
            try:
                c.post("/login", data={"secret": SECRET})
                html = c.get("/").text
            finally:
                app.state.instances.running = {}
        assert 'id="app"' in html, "render did not produce the dashboard (login failed?)"
        return html
    finally:
        if prev is None:
            os.environ.pop("APP_SECRET", None)
        else:
            os.environ["APP_SECRET"] = prev


# Measures each nav icon's on-screen visual centre (mapping the SVG content
# bbox, in viewBox units, through the square box), plus item heights and gaps.
_MEASURE = r"""
() => {
  const items = [...document.querySelectorAll('.nav-item')];
  const rows = items.map(it => {
    const svg = it.querySelector('.ico');
    const box = svg.getBoundingClientRect();
    const bb = svg.getBBox();
    const scale = box.width / svg.viewBox.baseVal.width;   // square box & viewBox
    const r = it.getBoundingClientRect();
    return {
      nav: it.getAttribute('data-nav'),
      visCx: box.left + (bb.x + bb.width/2)*scale,   // absolute on-screen centre x
      vbCy: bb.y + bb.height/2,                        // content centre y, in viewBox units
      bbW: bb.width, bbH: bb.height,
      top: r.top, bottom: r.bottom, height: r.height,
    };
  });
  return rows;
}
"""


def _spread(xs):
    return max(xs) - min(xs)


@pytest.fixture(scope="module")
def measured():
    """Launch a browser once, render the dashboard, and return a probe with the
    nav geometry in both rail states plus a live `page` for the mobile check."""
    html = _render_dashboard()
    with sync_playwright() as p:
        browser = None
        for kw in ({"channel": "chrome"}, {}):
            try:
                browser = p.chromium.launch(headless=True, **kw)
                break
            except PWError:
                continue
        if browser is None:
            pytest.skip("no Chrome/Chromium available for layout measurement")

        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(PAGE_URL, lambda r: r.fulfill(
            status=200, content_type="text/html", body=html))
        page.route("**/novnc/core/rfb.js", lambda r: r.fulfill(
            status=200, content_type="text/javascript", body=STUB_RFB))
        page.route("**/sessions/instances/**", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"token":"t","path":"/instances/i1/vnc"}'))
        page.goto(PAGE_URL, wait_until="networkidle")

        expanded = page.evaluate(_MEASURE)
        page.evaluate("document.getElementById('app').classList.add('collapsed')")
        page.wait_for_timeout(60)
        collapsed = page.evaluate(_MEASURE)
        page.evaluate("document.getElementById('app').classList.remove('collapsed')")

        yield {"page": page, "expanded": expanded, "collapsed": collapsed}
        browser.close()


class TestNavIconsAreOneSet:
    """Commit 1: the five icons share a visual centre and a consistent weight."""

    def test_all_five_are_present(self, measured):
        assert [r["nav"] for r in measured["expanded"]] == \
            ["overview", "browsers", "tasks", "connect", "settings"]

    @pytest.mark.parametrize("state", ["expanded", "collapsed"])
    def test_icon_centres_share_one_vertical_axis(self, measured, state):
        assert _spread([r["visCx"] for r in measured[state]]) < 1.0

    @pytest.mark.parametrize("state", ["expanded", "collapsed"])
    def test_icon_content_is_vertically_centred_in_its_box(self, measured, state):
        # Each icon's content centre should sit at the viewBox centre (12), so they
        # line up vertically within their rows rather than riding high or low.
        vbcy = [r["vbCy"] for r in measured[state]]
        assert _spread(vbcy) < 1.0
        assert all(abs(y - 12) < 1.0 for y in vbcy)

    def test_icons_are_a_consistent_size(self, measured):
        # The bug this replaces: sizes ranged 16–20 wide / 14–20 tall.
        rows = measured["expanded"]
        assert _spread([r["bbW"] for r in rows]) < 3.0
        assert _spread([r["bbH"] for r in rows]) < 3.0

    def test_the_badge_items_do_not_shift_their_icon(self, measured):
        """Browsers and Tasks carry a count badge; it must not nudge the icon off
        the shared axis."""
        rows = {r["nav"]: r for r in measured["expanded"]}
        axis = rows["overview"]["visCx"]
        assert abs(rows["browsers"]["visCx"] - axis) < 1.0
        assert abs(rows["tasks"]["visCx"] - axis) < 1.0


class TestRailRhythmIsStable:
    """Commit 2: collapsing the rail must not change item height or spacing."""

    def test_item_height_is_equal_across_states(self, measured):
        eh = [r["height"] for r in measured["expanded"]]
        ch = [r["height"] for r in measured["collapsed"]]
        assert _spread(eh) < 0.5 and _spread(ch) < 0.5, "heights differ within a state"
        assert abs(eh[0] - ch[0]) < 0.5, f"item height jumped {eh[0]}→{ch[0]} on collapse"

    @pytest.mark.parametrize("state", ["expanded", "collapsed"])
    def test_inter_item_gaps_are_uniform(self, measured, state):
        rows = measured[state]
        gaps = [rows[i + 1]["top"] - rows[i]["bottom"] for i in range(len(rows) - 1)]
        assert _spread(gaps) < 0.5, f"uneven gaps: {gaps}"


class TestMobileKeyboard:
    """Commit 3: on a phone-width viewport the Keyboard button pops the soft
    keyboard by focusing the offscreen input."""

    def test_taking_control_reveals_keyboard_which_focuses_the_input(self, measured):
        page = measured["page"]
        page.set_viewport_size({"width": 390, "height": 780})
        # Drive the page's own controls (the buttons live inside an accordion, so
        # dispatch clicks directly rather than fight Playwright actionability).
        page.evaluate("document.querySelector('[data-nav=browsers]').click()")
        page.wait_for_timeout(60)
        assert page.query_selector('[data-section=browsers].on'), "Browsers tab is shown"

        kb = page.query_selector('[data-keyboard-for="i1"]')
        assert kb is not None, "the keyboard button is rendered for a controllable browser"
        assert kb.get_attribute("hidden") is not None, "hidden until control is taken"

        page.evaluate("document.querySelector('[data-control-for=\"i1\"]').click()")  # take control
        page.wait_for_timeout(60)
        assert page.query_selector('[data-keyboard-for="i1"]:not([hidden])'), \
            "keyboard button shows once driving"

        # It must be reachable by a real thumb, not just present in the DOM: assert
        # the button sits inside the 390px viewport (the pane's aspect-ratio once
        # pushed it off-screen).
        vw = page.viewport_size["width"]
        rect = page.evaluate(
            "() => { const b=document.querySelector('[data-keyboard-for=\"i1\"]')"
            ".getBoundingClientRect(); return {left:b.left, right:b.right, w:b.width}; }")
        assert rect["w"] > 0 and rect["left"] >= 0 and rect["right"] <= vw, \
            f"keyboard button is off-screen on mobile: {rect} (viewport {vw})"

        page.evaluate("document.querySelector('[data-keyboard-for=\"i1\"]').click()")  # tap Keyboard
        focused = page.evaluate(
            "() => document.activeElement && document.activeElement.classList.contains('pane-kbd')")
        assert focused, "tapping Keyboard focuses the offscreen input (pops the soft keyboard)"

        page.set_viewport_size({"width": 1280, "height": 900})


class TestControlLandsOnTheRightPane:
    """The discriminating guard for the pane-targeting fix (6b54dc5).

    One instance is shown by two panes at once: a view-only preview on Overview
    and the full pane on Browsers, both `.pane[data-instance="i1"]`. Take control
    must attach the DRIVABLE (viewOnly=false) connection to the Browsers pane, not
    to the first-in-DOM Overview preview. The earlier keyboard test keyed off
    whether the button appeared, so it passed even with the bug present; this
    keys off which element the RFB connection actually attached to.
    """

    def test_take_control_drives_the_browsers_pane_not_the_overview_preview(self, measured):
        page = measured["page"]
        page.set_viewport_size({"width": 1280, "height": 900})
        page.evaluate("() => document.getElementById('app').classList.remove('drawer-open','collapsed')")

        # The bug is only discriminable when the duplicate actually exists.
        dupes = page.evaluate("() => document.querySelectorAll('.pane[data-instance=\"i1\"]').length")
        assert dupes == 2, f"need the Overview-preview + Browsers duplicate to test targeting; got {dupes}"

        page.evaluate("document.querySelector('[data-nav=browsers]').click()")
        page.wait_for_timeout(80)
        # Start from a known state: if i1 is already controlling, release it.
        page.evaluate("""() => {
          const btn = document.querySelector('[data-control-for="i1"]');
          const body = btn.closest('.acc-body');
          const pane = body && body.querySelector('.pane');
          if (pane && pane.classList.contains('controlling')) btn.click();
        }""")
        page.wait_for_timeout(80)

        page.evaluate("window.__rfbLog = []")
        page.evaluate("document.querySelector('[data-control-for=\"i1\"]').click()")  # take control
        page.wait_for_timeout(150)

        log = page.evaluate("""() => (window.__rfbLog || []).map(e => ({
          viewOnly: e.viewOnly,
          preview: !!(e.target && e.target.closest('.pane')
                      && e.target.closest('.pane').classList.contains('preview')),
          section: e.target && e.target.closest('[data-section]')
                   && e.target.closest('[data-section]').getAttribute('data-section'),
        }))""")
        control = [e for e in log if e["viewOnly"] is False]
        assert control, f"taking control should open a drivable (viewOnly=false) connection; log={log}"
        assert all(e["section"] == "browsers" and not e["preview"] for e in control), (
            f"control attached to the wrong pane — the Overview preview, not Browsers. log={log}"
        )


class TestLicenceVerifySubmission:
    """The loading state must not turn Verify into an ordinary Save.

    HTML serializes only successful controls. A disabled submit button is not
    successful, so disabling every button from the submit handler used to erase
    its ``name=action value=verify`` before the browser built the POST body. This
    runs the real dashboard JavaScript in Chrome and asks the DOM's own FormData
    implementation what would be sent after the loading-state handler fires.
    """

    def test_disabled_loading_controls_preserve_verify_in_the_serialized_post(self, measured):
        page = measured["page"]
        result = page.evaluate("""() => new Promise(resolve => {
          const form = document.getElementById('licence-form');
          const verify = form.querySelector('button[name="action"][value="verify"]');
          form.addEventListener('submit', event => {
            event.preventDefault();
            const fields = [...new FormData(form).entries()];
            resolve({
              fields,
              allButtonsDisabled: [...form.querySelectorAll('button')].every(b => b.disabled),
              waitShown: !document.getElementById('licence-wait').hidden,
              buttonText: verify.textContent,
            });
          }, {once: true});
          verify.click();
        })""")

        actions = [value for name, value in result["fields"] if name == "action"]
        assert actions == ["verify"], f"serialized POST lost/duplicated verify: {result['fields']}"
        assert result["allButtonsDisabled"], "loading state must still prevent repeat submits"
        assert result["waitShown"] and "about ten seconds" in result["buttonText"]


_PROBE = r"""
() => {
  const vis = e => { if(!e) return false; const s=getComputedStyle(e);
    return s.display!=='none' && s.visibility!=='hidden'; };
  const side = document.querySelector('.side').getBoundingClientRect();
  const main = document.querySelector('main').getBoundingClientRect();
  // Only elements actually on the page — a control in a hidden section (an
  // accordion in a tab that isn't open) has height 0 and isn't a tap target now.
  const H = sel => [...document.querySelectorAll(sel)]
    .map(e => e.getBoundingClientRect().height).filter(h => h > 0);
  const wrap = document.querySelector('.tablewrap');
  return {
    vw: innerWidth,
    bodyScrollW: document.documentElement.scrollWidth,
    sideLeft: side.left, sideRight: side.right,
    mainW: main.width,
    hamburger: vis(document.getElementById('hamburger')),
    collapse: vis(document.getElementById('collapse')),
    drawerOpen: document.getElementById('app').classList.contains('drawer-open'),
    labelsVisible: vis(document.querySelector('.nav-item .txt')),
    navH: H('.nav-item'), accH: H('.acc-head'), btnH: H('.btn'),
    tableOverflowX: wrap ? getComputedStyle(wrap).overflowX : null,
  };
}
"""


class TestResponsive:
    """Commit for the full mobile pass: the whole dashboard, measured at phone,
    tablet, and desktop widths."""

    def _at(self, page, width, height=820):
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("() => { const a=document.getElementById('app');"
                      " a.classList.remove('drawer-open','collapsed'); }")
        page.wait_for_timeout(320)  # let the drawer transition settle before measuring
        return page.evaluate(_PROBE)

    def test_phone_drawer_hidden_and_no_horizontal_scroll(self, measured):
        m = self._at(measured["page"], 390)
        assert m["sideRight"] <= 1, "the drawer is off-screen by default"
        assert m["hamburger"] and not m["collapse"], "hamburger replaces the collapse toggle"
        assert abs(m["mainW"] - 390) < 2, "main content is full width"
        assert m["bodyScrollW"] <= 390, f"horizontal overflow: scrollWidth {m['bodyScrollW']} > 390"

    def test_phone_hamburger_opens_drawer_with_labels(self, measured):
        page = measured["page"]
        self._at(page, 390)
        page.evaluate("document.getElementById('hamburger').click()")
        page.wait_for_timeout(260)
        m = page.evaluate(_PROBE)
        assert m["drawerOpen"] and m["sideLeft"] >= -1, "hamburger slides the drawer in"
        assert m["labelsVisible"], "the drawer shows full labels, not an icon-only rail"
        assert m["bodyScrollW"] <= 390, "the open drawer overlays; it does not widen the page"

    def test_phone_touch_targets_are_at_least_44px(self, measured):
        page = measured["page"]
        self._at(page, 390)
        # Browsers shows all three kinds of target at once and unhidden: nav items,
        # accordion heads (one per browser), and buttons (New browser / Close).
        page.evaluate("document.querySelector('[data-nav=browsers]').click()")
        page.wait_for_timeout(80)
        m = page.evaluate(_PROBE)
        assert m["navH"] and all(h >= 44 for h in m["navH"]), f"nav taps: {m['navH']}"
        assert m["accH"] and all(h >= 44 for h in m["accH"]), f"accordion heads: {m['accH']}"
        assert m["btnH"] and all(h >= 42 for h in m["btnH"]), f"buttons: {m['btnH']}"

    def test_phone_wide_tables_scroll_in_their_container(self, measured):
        m = self._at(measured["page"], 390)
        assert m["tableOverflowX"] in ("auto", "scroll"), \
            "the history table scrolls inside its own box, so the page never does"

    def test_tablet_has_no_horizontal_overflow(self, measured):
        m = self._at(measured["page"], 768)
        assert m["bodyScrollW"] <= 768, f"overflow at 768px: {m['bodyScrollW']}"

    def test_desktop_keeps_the_rail_and_collapse_toggle(self, measured):
        m = self._at(measured["page"], 1280)
        assert m["sideLeft"] >= -1 and m["sideRight"] > 100, "the rail is in-flow, not a drawer"
        assert m["collapse"] and not m["hamburger"], "desktop shows the collapse toggle"
        assert m["bodyScrollW"] <= 1280


def _screenshots(outdir: str) -> None:
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    html = _render_dashboard()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route(PAGE_URL, lambda r: r.fulfill(
            status=200, content_type="text/html", body=html))
        page.route("**/novnc/core/rfb.js", lambda r: r.fulfill(
            status=200, content_type="text/javascript", body=STUB_RFB))
        page.route("**/sessions/instances/**", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"token":"t","path":"/instances/i1/vnc"}'))
        page.goto(PAGE_URL, wait_until="networkidle")

        def reset(width, height=860):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("() => document.getElementById('app').classList.remove('drawer-open','collapsed')")
            page.evaluate("document.querySelector('[data-nav=overview]').click()")
            page.wait_for_timeout(380)  # let the drawer transition fully settle before the shot

        # desktop: the rail, expanded and collapsed
        reset(1280)
        page.locator(".side").screenshot(path=str(out / "nav-expanded.png"))
        page.evaluate("document.getElementById('app').classList.add('collapsed')")
        page.wait_for_timeout(80)
        page.locator(".side").screenshot(path=str(out / "nav-collapsed.png"))
        page.screenshot(path=str(out / "desktop.png"))

        # tablet
        reset(768)
        page.screenshot(path=str(out / "tablet.png"))

        # phone: drawer closed, then open
        reset(390, 800)
        page.screenshot(path=str(out / "phone-closed.png"))
        page.evaluate("document.getElementById('hamburger').click()")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "phone-drawer.png"))

        # phone: driving a browser, keyboard revealed
        reset(390, 800)
        page.evaluate("document.querySelector('[data-nav=browsers]').click()")
        page.wait_for_timeout(80)
        page.evaluate("document.querySelector('[data-control-for=\"i1\"]').click()")
        page.evaluate("document.querySelector('[data-keyboard-for=\"i1\"]').click()")
        page.wait_for_timeout(80)
        page.screenshot(path=str(out / "mobile-control.png"), full_page=True)
        browser.close()
    print(f"wrote screenshots to {out}")


if __name__ == "__main__":
    import sys

    _screenshots(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
