"""The pool budget: interactive sessions must never be starved by a sweep.

These exercise the accounting and the wait-for-slot path with a stubbed launch —
a real browser is verified separately by scripts/verify_browser.py inside the
container. Never launch a browser on the host: it would go out over the real IP.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.models import InstanceCreate
from app.services.instances import CapExceeded, Instance, InstanceManager
from app.services.settings import SettingsService

pytestmark = pytest.mark.asyncio


class FakeContext:
    """Stands in for a Playwright BrowserContext."""

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    async def close(self):
        pass


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """A manager whose launches are instant and browserless."""
    service = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    service.update(
        cloakbrowser_license_key="cb_test",
        proxy_user="u", proxy_password="p", proxy_host="h", proxy_port="1000",
    )
    mgr = InstanceManager(service)
    monkeypatch.setattr(mgr, "profiles", _NullProfiles(tmp_path))

    async def fake_launch(req, origin, owner, subject=None):
        await asyncio.sleep(0.01)
        now = time.monotonic()
        return Instance(
            id=uuid.uuid4().hex[:12], profile=req.profile, origin=origin, owner=owner,
            subject=subject,
            context=FakeContext(), display=0, cdp_port=0, proxy_ip="203.0.113.1",
            timezone="America/Los_Angeles", locale="en-US", headed=True, geoip=True,
            humanize=True, seed=1, ttl_min=60, created_wall=time.time(),
            created_mono=now, last_used_mono=now,
        )

    monkeypatch.setattr(mgr, "_do_launch", fake_launch)
    mgr._settings_service = service
    return mgr


class _NullProfiles:
    def __init__(self, root):
        self.root = root

    def get_or_create(self, name, **kw):
        raise AssertionError("stubbed launch should not reach the profile store")


async def _launch(mgr, origin, wait=None, profile="p"):
    return await mgr.launch(InstanceCreate(profile=profile), origin=origin, wait=wait)


async def test_defaults_leave_one_slot_reserved(manager):
    assert manager.counts() == {
        "task": 0, "interactive": 0, "total": 0, "max": 4, "task_budget": 3, "reserve": 1
    }


async def test_tasks_cannot_consume_the_interactive_reserve(manager):
    for _ in range(3):  # task_budget = 4 - 1
        await _launch(manager, "task", wait=False)
    assert manager.counts()["task"] == 3

    with pytest.raises(CapExceeded, match="task budget full"):
        await _launch(manager, "task", wait=False)

    # The point of the reserve: a human still gets in with the sweep at full tilt.
    inst = await _launch(manager, "interactive", wait=False)
    assert inst.origin == "interactive"


async def test_interactive_is_bounded_only_by_max(manager):
    for _ in range(4):
        await _launch(manager, "interactive", wait=False)
    assert manager.counts()["total"] == 4
    with pytest.raises(CapExceeded, match="pool full"):
        await _launch(manager, "interactive", wait=False)


async def test_a_task_waits_for_a_slot_instead_of_failing(manager):
    held = [await _launch(manager, "task", wait=False) for _ in range(3)]
    waiting = asyncio.create_task(_launch(manager, "task", wait=True))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "a task at budget must wait, not fail"

    await manager.stop(held[0].id)
    got = await asyncio.wait_for(waiting, timeout=2)
    assert got.origin == "task"
    assert manager.counts()["task"] == 3


async def test_tasks_wait_by_default_and_interactive_does_not(manager):
    for _ in range(3):
        await _launch(manager, "task", wait=False)
    queued = asyncio.create_task(_launch(manager, "task"))  # wait defaults to True
    await asyncio.sleep(0.05)
    assert not queued.done()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert manager._profiles_opening == {}

    for _ in range(1):
        await _launch(manager, "interactive", wait=False)
    with pytest.raises(CapExceeded):  # wait defaults to False
        await _launch(manager, "interactive")


async def test_budget_change_in_settings_applies_without_a_restart(manager):
    for _ in range(3):
        await _launch(manager, "task", wait=False)
    with pytest.raises(CapExceeded):
        await _launch(manager, "task", wait=False)

    manager._settings_service.update(max_instances=8)
    assert manager.counts()["task_budget"] == 7
    await _launch(manager, "task", wait=False)  # no restart, no reconstruction


async def test_a_failed_launch_does_not_leak_its_slot(manager, monkeypatch):
    async def boom(req, origin, owner, subject=None):
        raise RuntimeError("launch exploded")

    monkeypatch.setattr(manager, "_do_launch", boom)
    for _ in range(5):
        with pytest.raises(RuntimeError, match="launch exploded"):
            await _launch(manager, "task", wait=False)
    # A leaked pending count would wedge the pool permanently.
    assert manager._pending == {"task": 0, "interactive": 0}
    assert manager._profiles_opening == {}
    assert manager.counts()["total"] == 0


async def test_a_waiting_task_is_released_when_a_launch_fails(manager, monkeypatch):
    held = [await _launch(manager, "task", wait=False) for _ in range(3)]

    async def boom(req, origin, owner, subject=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(manager, "_do_launch", boom)
    waiting = asyncio.create_task(_launch(manager, "task", wait=True))
    await asyncio.sleep(0.05)
    await manager.stop(held[0].id)
    with pytest.raises(RuntimeError, match="nope"):
        await asyncio.wait_for(waiting, timeout=2)
    assert manager._pending["task"] == 0
    assert manager._profiles_opening == {}


async def test_stopping_an_unknown_instance_is_not_an_error(manager):
    assert await manager.stop("does-not-exist") is False
