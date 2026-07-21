"""Shared profile policy, including launch-time lifecycle races."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.models import InstanceCreate
from app.services.instances import Instance, InstanceManager
from app.services.profiles import (
    DEFAULT_PROFILE,
    ProfileConflict,
    ProfileInUse,
    ProfileNotFound,
    ProfileService,
    ProfileStore,
)
from app.services.settings import SettingsService

pytestmark = pytest.mark.asyncio


class _Context:
    def __init__(self):
        self.closed = False

    def on(self, event, handler):
        pass

    async def close(self):
        self.closed = True


def _instance(profile: str, iid: str = "i1") -> Instance:
    now = time.monotonic()
    return Instance(
        id=iid,
        profile=profile,
        origin="interactive",
        owner=None,
        subject="owner",
        context=_Context(),
        display=0,
        cdp_port=0,
        proxy_ip=None,
        timezone=None,
        locale=None,
        headed=True,
        geoip=False,
        humanize=True,
        seed=1,
        ttl_min=60,
        created_wall=time.time(),
        created_mono=now,
        last_used_mono=now,
    )


@pytest.fixture
def managed(tmp_path):
    settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
    manager = InstanceManager(settings)
    manager.profiles = ProfileStore(tmp_path / "profiles")
    manager.profiles.ensure_default(default_country="US", default_region="california")
    return manager, ProfileService(manager, settings), settings


async def test_safe_view_is_a_snapshot_and_exposes_no_identity_secrets(managed):
    manager, service, _ = managed
    internal = manager.profiles.get_or_create(
        "research", default_country="US", default_region="california"
    )
    internal.session_token = "SESSION_TOKEN_SENTINEL"
    internal.fingerprint_seed = 1_987_654_321
    internal.user_data_dir = "/tmp/USER_DATA_DIR_SENTINEL"

    payload = [view.model_dump() for view in await service.list_profiles()]
    raw = json.dumps(payload, sort_keys=True)
    assert set(payload[0]) == {
        "name", "country", "region", "is_default", "in_use", "proxy_configured",
    }
    for forbidden in (
        "session_token", "fingerprint_seed", "user_data_dir",
        "SESSION_TOKEN_SENTINEL", "1987654321", "USER_DATA_DIR_SENTINEL",
    ):
        assert forbidden not in raw


async def test_explicit_create_collides_but_launch_get_or_create_remains_separate(managed):
    _, service, _ = managed
    created = await service.create_profile("research", country="GB", region="london")
    assert created.name == "research" and created.country == "GB"
    with pytest.raises(ProfileConflict, match="already exists"):
        await service.create_profile("research")


async def test_missing_default_and_empty_updates_are_explicit(managed):
    _, service, _ = managed
    with pytest.raises(ProfileNotFound, match="no profile"):
        await service.update_profile("missing", country="GB")
    with pytest.raises(ProfileNotFound, match="no profile"):
        await service.delete_profile("missing")
    with pytest.raises(ValueError, match="Default"):
        await service.delete_profile(DEFAULT_PROFILE)
    with pytest.raises(ValueError, match="provide"):
        await service.update_profile(DEFAULT_PROFILE)


async def test_new_proxy_session_refuses_direct_and_partial_without_rotating(managed):
    manager, service, settings = managed
    profile = manager.profiles.get_or_create(
        "research", default_country="US", default_region="california"
    )
    before = profile.session_token

    with pytest.raises(ProfileConflict, match="direct mode"):
        await service.new_proxy_session("research")
    assert manager.profiles.rotate_session("missing") is None  # control: lookup remains explicit
    assert {p.name: p for p in manager.profiles.all()}["research"].session_token == before

    with pytest.raises(ProfileNotFound, match="missing"):
        await service.new_proxy_session("missing")

    settings.update(proxy_user="only-one-field")
    with pytest.raises(ProfileConflict, match="incomplete"):
        await service.new_proxy_session("research")
    assert {p.name: p for p in manager.profiles.all()}["research"].session_token == before


async def test_new_proxy_session_changes_only_internal_token_when_configured(managed):
    manager, service, settings = managed
    profile = manager.profiles.get_or_create(
        "research", default_country="US", default_region="california"
    )
    before = profile.session_token
    settings.update(
        proxy_user="u", proxy_password="p", proxy_host="proxy.example", proxy_port="1000",
    )
    view = await service.new_proxy_session("research")
    after = {p.name: p for p in manager.profiles.all()}["research"]
    assert after.session_token != before
    assert view.name == "research" and view.proxy_configured is True
    assert "session" not in view.model_dump_json()


async def test_opening_and_open_profile_block_rename_and_delete(managed, monkeypatch):
    manager, service, _ = managed
    await service.create_profile("busy")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def opening(req, origin, owner, subject=None):
        entered.set()
        await release.wait()
        return _instance(req.profile)

    monkeypatch.setattr(manager, "_do_launch", opening)
    launch = asyncio.create_task(
        manager.launch(InstanceCreate(profile="busy"), origin="interactive")
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    views = {view.name: view for view in await service.list_profiles()}
    assert views["busy"].in_use is True
    with pytest.raises(ProfileInUse, match="opening"):
        await service.update_profile("busy", new_name="renamed")
    with pytest.raises(ProfileInUse, match="opening"):
        await service.delete_profile("busy")

    release.set()
    instance = await asyncio.wait_for(launch, timeout=1)
    with pytest.raises(ProfileInUse, match="open"):
        await service.delete_profile("busy")
    await manager.stop(instance.id)
    renamed = await service.update_profile("busy", new_name="renamed")
    assert renamed.name == "renamed"


async def test_rename_also_guards_an_opening_destination(managed, monkeypatch):
    """Absent destination is not safe when launch-on-demand already reserved it."""
    manager, service, _ = managed
    await service.create_profile("source")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def opening(req, origin, owner, subject=None):
        entered.set()
        await release.wait()
        return _instance(req.profile, iid="dest-browser")

    monkeypatch.setattr(manager, "_do_launch", opening)
    launch = asyncio.create_task(
        manager.launch(InstanceCreate(profile="destination"), origin="interactive")
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(ProfileInUse, match="destination"):
        await service.update_profile("source", new_name="destination")
    assert {p.name for p in manager.profiles.all()} == {DEFAULT_PROFILE, "source"}

    release.set()
    instance = await asyncio.wait_for(launch, timeout=1)
    await manager.stop(instance.id)


async def test_failed_launch_releases_profile_reservation(managed, monkeypatch):
    manager, service, _ = managed
    await service.create_profile("failed")

    async def fail(req, origin, owner, subject=None):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(manager, "_do_launch", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        await manager.launch(InstanceCreate(profile="failed"), origin="interactive")
    assert manager._profiles_opening == {}
    assert (await service.update_profile("failed", new_name="recovered")).name == "recovered"


async def test_cancelled_capacity_wait_releases_profile_reservation(managed, monkeypatch):
    manager, service, settings = managed
    settings.update(max_instances=1, interactive_reserve=0)
    await service.create_profile("held")
    await service.create_profile("waiting")

    async def immediate(req, origin, owner, subject=None):
        return _instance(req.profile, iid=req.profile)

    monkeypatch.setattr(manager, "_do_launch", immediate)
    held = await manager.launch(
        InstanceCreate(profile="held"), origin="interactive", wait=False,
    )
    waiting = asyncio.create_task(
        manager.launch(InstanceCreate(profile="waiting"), origin="interactive", wait=True)
    )
    await asyncio.sleep(0)
    assert "waiting" in manager.profile_names_in_use()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert "waiting" not in manager.profile_names_in_use()
    assert manager._profiles_opening == {}
    await manager.stop(held.id)


async def test_cancelled_registration_closes_unreachable_browser_and_releases_profile(
    managed, monkeypatch
):
    """Cancellation after process launch must not orphan a browser or identity."""
    manager, service, _ = managed
    await service.create_profile("opened")
    process_open = asyncio.Event()
    let_launch_return = asyncio.Event()
    context = _Context()

    async def opened(req, origin, owner, subject=None):
        process_open.set()
        await let_launch_return.wait()
        instance = _instance(req.profile, iid="unregistered")
        instance.context = context
        return instance

    stopped: list[int] = []

    async def stop_display(display):
        stopped.append(display)

    monkeypatch.setattr(manager, "_do_launch", opened)
    monkeypatch.setattr(manager.displays, "stop", stop_display)
    launch = asyncio.create_task(
        manager.launch(InstanceCreate(profile="opened"), origin="interactive")
    )
    await asyncio.wait_for(process_open.wait(), timeout=1)

    # Hold the lifecycle lock so _do_launch can return but launch cannot move
    # the instance into ``running``. Cancellation now hits the exact seam.
    async with manager._cond:
        let_launch_return.set()
        await asyncio.sleep(0)
        launch.cancel()

    with pytest.raises(asyncio.CancelledError):
        await launch
    assert manager.running == {}
    assert manager._pending == {"task": 0, "interactive": 0}
    assert manager._profiles_opening == {}
    assert context.closed is True
    assert stopped == [0]
    assert (await service.update_profile("opened", new_name="recovered")).name == "recovered"


async def test_spontaneous_close_keeps_profile_guarded_until_cleanup(managed, monkeypatch):
    manager, service, _ = managed
    await service.create_profile("closing")
    instance = _instance("closing")
    manager.running[instance.id] = instance
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_display_stop(display):
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager.displays, "stop", slow_display_stop)
    closing = asyncio.create_task(manager._on_closed(instance.id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    with pytest.raises(ProfileInUse, match="closing"):
        await service.delete_profile("closing")
    release.set()
    await asyncio.wait_for(closing, timeout=1)
    assert (await service.delete_profile("closing")).ok is True
