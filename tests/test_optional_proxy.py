"""The proxy is optional; a proxy the user attempted to configure is not.

These tests drive the real ``InstanceManager._do_launch`` call path and inspect
the exact arguments handed to CloakBrowser. That is the boundary where a direct
launch could accidentally inherit proxy probing, a guessed timezone, or a
silent fallback after a configured proxy failed.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.models import InstanceCreate
from app.services import geo, instances
from app.services.geo import ProxyProbe, ProxyUnreachable
from app.services.instances import InstanceManager
from app.services.profiles import ProfileStore
from app.services.proxy import ProxyNotConfigured
from app.services.settings import SettingsService

pytestmark = pytest.mark.asyncio


class _Context:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    async def close(self) -> None:
        pass


@dataclass
class _Displays:
    allocated: int = 0
    started: int = 0
    stopped: int = 0

    async def allocate(self) -> int:
        self.allocated += 1
        return 100

    async def start(self, number: int, width: int, height: int) -> None:
        self.started += 1
        return None

    async def stop(self, number: int) -> None:
        self.stopped += 1


def _manager(tmp_path, monkeypatch, **settings_changes) -> tuple[InstanceManager, _Displays]:
    service = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    service.update(cloakbrowser_license_key="cb_test", **settings_changes)
    manager = InstanceManager(service)
    manager.profiles = ProfileStore(tmp_path / "profiles")
    displays = _Displays()
    manager.displays = displays
    monkeypatch.setattr(manager, "_alloc_cdp_port", lambda: 9333)
    monkeypatch.setattr(
        instances, "resolve_browser_binary", lambda key, version: "/fake/pro"
    )
    return manager, displays


def _capture_browser_launch(monkeypatch) -> list[dict]:
    import cloakbrowser

    calls: list[dict] = []

    async def fake_launch(**kwargs):
        calls.append(kwargs)
        return _Context()

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", fake_launch)
    return calls


async def test_no_proxy_launches_direct_without_probe_or_geolocation(tmp_path, monkeypatch):
    """No proxy means exactly proxy=None and honest unknown geo fields.

    ``geoip=False`` at the package boundary is important: CloakBrowser otherwise
    geolocates the machine's direct public IP internally. Direct mode must not
    grow a hidden network prerequisite or return geo values this service did not
    observe.
    """
    manager, displays = _manager(tmp_path, monkeypatch)
    launch_calls = _capture_browser_launch(monkeypatch)

    async def must_not_probe(*args, **kwargs):
        raise AssertionError("direct launch reached the proxy probe/geolocator")

    monkeypatch.setattr(geo, "probe", must_not_probe)
    monkeypatch.setattr(
        geo, "_geolocate",
        lambda ip: (_ for _ in ()).throw(AssertionError("direct launch geolocated")),
    )

    inst = await manager._do_launch(
        InstanceCreate(profile="Default", geoip=True), "interactive", None, "owner"
    )

    assert len(launch_calls) == 1
    call = launch_calls[0]
    assert call["proxy"] is None
    assert call["timezone"] is None and call["locale"] is None
    assert call["geoip"] is False
    assert (inst.proxy_ip, inst.timezone, inst.locale, inst.geoip) == (None, None, None, False)
    assert (displays.allocated, displays.started) == (1, 1)


async def test_configured_proxy_is_probed_and_passed_to_browser(tmp_path, monkeypatch):
    manager, displays = _manager(
        tmp_path, monkeypatch,
        proxy_user="u", proxy_password="pw", proxy_host="proxy.example", proxy_port="1000",
    )
    launch_calls = _capture_browser_launch(monkeypatch)
    probe_calls: list[tuple[str, bool]] = []

    async def fake_probe(url: str, *, geo: bool = True) -> ProxyProbe:
        probe_calls.append((url, geo))
        return ProxyProbe(
            exit_ip="203.0.113.8", timezone="America/New_York",
            locale="en-US", country="US", city="New York",
        )

    monkeypatch.setattr(geo, "probe", fake_probe)
    inst = await manager._do_launch(
        InstanceCreate(profile="Default", geoip=True), "interactive", None, "owner"
    )

    assert len(probe_calls) == 1
    proxy_url, requested_geo = probe_calls[0]
    assert proxy_url.startswith("http://u:pw_country-US_region-california_session-")
    assert requested_geo is True
    assert launch_calls[0]["proxy"] == proxy_url
    assert launch_calls[0]["timezone"] == "America/New_York"
    assert launch_calls[0]["locale"] == "en-US"
    assert launch_calls[0]["geoip"] is True
    assert (inst.proxy_ip, inst.timezone, inst.locale) == (
        "203.0.113.8", "America/New_York", "en-US"
    )
    assert (displays.allocated, displays.started) == (1, 1)


async def test_broken_configured_proxy_fails_before_launch_and_never_retries_direct(
    tmp_path, monkeypatch
):
    manager, displays = _manager(
        tmp_path, monkeypatch,
        proxy_user="u", proxy_password="pw", proxy_host="dead.example", proxy_port="1000",
    )
    launch_calls = _capture_browser_launch(monkeypatch)
    probe_calls = 0

    async def broken(url: str, *, geo: bool = True):
        nonlocal probe_calls
        probe_calls += 1
        raise ProxyUnreachable("configured proxy cannot route")

    monkeypatch.setattr(geo, "probe", broken)

    with pytest.raises(ProxyUnreachable, match="cannot route"):
        await manager._do_launch(
            InstanceCreate(profile="Default"), "interactive", None, "owner"
        )

    assert probe_calls == 1
    assert launch_calls == [], "a proxy failure was retried as a direct browser launch"
    assert (displays.allocated, displays.started) == (0, 0)


async def test_partial_proxy_fails_instead_of_becoming_direct(tmp_path, monkeypatch):
    manager, displays = _manager(tmp_path, monkeypatch, proxy_user="u")
    launch_calls = _capture_browser_launch(monkeypatch)

    async def must_not_probe(*args, **kwargs):
        raise AssertionError("an incomplete proxy should fail before probing")

    monkeypatch.setattr(geo, "probe", must_not_probe)

    with pytest.raises(ProxyNotConfigured, match="incomplete.*proxy_password"):
        await manager._do_launch(
            InstanceCreate(profile="Default"), "interactive", None, "owner"
        )

    assert launch_calls == []
    assert (displays.allocated, displays.started) == (0, 0)
