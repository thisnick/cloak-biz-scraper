"""Keeping the machine awake for the length of a sweep.

Railway sleeps a service when it sends no **outbound** packets for ten minutes;
inbound traffic is explicitly excluded and only wakes it. A running sweep mostly
pins the machine by doing its job — Chromium's egress through the proxy counts,
and it is measured at the service's network interface rather than per-process.

The gap this closes is the CPU-bound stretch: Readability and Turndown parsing,
markdown → blocks, a long backoff between attempts. During those the sweep is
plainly alive and generating no egress at all. Ten minutes of that is unlikely
but not impossible, and the failure it would cause is the expensive kind — the
container sleeps underneath a running job.

So this is a deliberately tiny outbound request, and it runs **only while a
sweep is in flight**. That condition is the whole design: a heartbeat that ran
unconditionally would reset the sleep timer forever and quietly bill the user
24/7 for an idle service, which is the exact trap the plan warns about. Idle
must stay idle.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("cloakbiz.heartbeat")

_INTERVAL_SEC = 60
# Something tiny, stable, and unrelated to our own service — a request to
# ourselves would never leave the machine and so would not count as outbound.
_BEACON_URL = "https://www.gstatic.com/generate_204"
_TIMEOUT_SEC = 10


async def beat() -> bool:
    """One outbound request. Never raises: a heartbeat that could fail a sweep
    would be worse than the sleep it prevents.

    **It fails loudly, though, and that is a deliberate change.** This used to log
    at DEBUG as "harmless", which was fair while the sweep's own egress was
    believed to do the pinning and this only covered the gaps. It cannot be
    believed: the sweep's traffic and this beacon run over the same interval by
    construction, so no measurement can separate them — and at 60s against a
    7-10 minute sleep threshold, **this beacon alone is sufficient**. That makes
    it the mechanism, not the spare.

    A silent beacon is therefore the expensive failure: nothing else is
    *demonstrably* keeping the container awake through a CPU-bound stretch, and
    at DEBUG nobody would ever know.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
            await client.get(_BEACON_URL)
        return True
    except Exception as exc:  # noqa: BLE001
        # Not "harmless" and not fatal: a sweep's own traffic may keep the
        # container awake anyway. What is lost is the guarantee, which is the
        # only reason this exists.
        logger.warning(
            "heartbeat beacon failed (%s: %s) — nothing is now guaranteeing this "
            "container stays awake while a sweep runs; a long CPU-bound stretch "
            "could let it sleep mid-job",
            type(exc).__name__, exc,
        )
        return False


async def loop(in_flight, *, interval_sec: int = _INTERVAL_SEC) -> None:
    """Beat every `interval_sec` for as long as `in_flight()` is non-zero."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            if in_flight():
                await beat()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat loop error")
