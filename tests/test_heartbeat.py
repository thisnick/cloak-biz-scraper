"""The heartbeat, and the bill it must not run up.

Two failure modes, opposite directions, both expensive:

  * Never beating — a sweep parsing markdown for ten minutes generates no
    outbound traffic, Railway sleeps the container underneath it, and the job
    dies mid-flight.
  * Always beating — the sleep timer never expires, the service never scales to
    zero, and the user quietly pays for a machine that is doing nothing. The
    plan calls this out explicitly: it is easy to never sleep and silently pay
    24/7.

So "only while a sweep is in flight" is the whole design, and it is what these
pin down.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.services import heartbeat


@respx.mock
@pytest.mark.asyncio
async def test_a_beat_is_an_outbound_request():
    """Outbound is the point: Railway measures egress, and a request to
    ourselves would never leave the machine."""
    route = respx.get(heartbeat._BEACON_URL).mock(return_value=httpx.Response(204))
    assert await heartbeat.beat() is True
    assert route.called
    assert not heartbeat._BEACON_URL.startswith("http://127.0.0.1")


@respx.mock
@pytest.mark.asyncio
async def test_a_failed_beat_never_raises():
    """A heartbeat that could fail a sweep would be worse than the sleep it
    prevents."""
    respx.get(heartbeat._BEACON_URL).mock(side_effect=httpx.ConnectError("no network"))
    assert await heartbeat.beat() is False


@pytest.mark.asyncio
async def test_it_beats_while_a_sweep_is_in_flight(monkeypatch):
    beats = 0

    async def fake_beat():
        nonlocal beats
        beats += 1
        return True

    monkeypatch.setattr(heartbeat, "beat", fake_beat)
    task = asyncio.create_task(heartbeat.loop(lambda: 1, interval_sec=0.01))
    await asyncio.sleep(0.06)
    task.cancel()

    assert beats >= 2, "a running sweep must keep the machine awake"


@pytest.mark.asyncio
async def test_it_is_silent_when_nothing_is_running(monkeypatch):
    """The expensive one. An unconditional heartbeat resets Railway's sleep
    timer forever and bills the user for an idle service."""
    beats = 0

    async def fake_beat():
        nonlocal beats
        beats += 1
        return True

    monkeypatch.setattr(heartbeat, "beat", fake_beat)
    task = asyncio.create_task(heartbeat.loop(lambda: 0, interval_sec=0.01))
    await asyncio.sleep(0.06)
    task.cancel()

    assert beats == 0, "idle must stay idle, or the service never scales to zero"


@pytest.mark.asyncio
async def test_a_broken_in_flight_check_does_not_kill_the_loop(monkeypatch):
    """The loop outlives the app; one bad read must not end the heartbeat for
    every sweep that follows."""
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return 0

    task = asyncio.create_task(heartbeat.loop(flaky, interval_sec=0.01))
    await asyncio.sleep(0.05)
    running = not task.done()
    task.cancel()

    assert running, "the loop must survive a failing check"
    assert calls > 1
