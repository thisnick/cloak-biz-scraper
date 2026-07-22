"""scrape_with_retry's launch handling — with the browser and pool faked.

The block/rotate retry is exercised through the scrape service elsewhere; what
is pinned here is the launch path: a resource-exhaustion launch failure (the
container out of threads) is a *transient capacity* failure and is retried
within the same attempts, while a genuine launch error propagates at once. The
distinction is what keeps a misconfigured pool degrading to slow rather than
broken, without papering over real failures.
"""
from __future__ import annotations

import pytest

from app.services.browsing import scrape_with_retry


class _Ctx:
    def __init__(self):
        self.pages = [object()]  # first_page() returns this; the fake never uses it

    async def new_page(self):
        return object()


class _Inst:
    def __init__(self, iid):
        self.id = iid
        self.proxy_ip = "1.2.3.4"
        self.context = _Ctx()


class _Profiles:
    def __init__(self):
        self.rotations = 0

    def rotate_session(self, profile):
        self.rotations += 1


class _Instances:
    """Launch outcomes are scripted one per attempt: an Exception is raised, None
    launches cleanly."""

    def __init__(self, launch_outcomes):
        self.profiles = _Profiles()
        self._outcomes = list(launch_outcomes)
        self.launches = 0
        self.stopped: list[str] = []

    async def launch(self, req, *, origin, owner, wait):
        outcome = self._outcomes[self.launches]
        self.launches += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return _Inst(f"inst-{self.launches}")

    async def stop(self, iid):
        self.stopped.append(iid)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _instant(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.browsing.asyncio.sleep", _instant)


async def _ok(inst, page):
    return {"blocked": False, "error": None, "data": {"listings": [], "pages_crawled": 1}}


async def _run(instances, attempts=3):
    return await scrape_with_retry(
        instances, profile="task-1", owner="job:x", wait_ms=0,
        attempts=attempts, scrape_once=_ok, warmup_url=None,
    )


class TestResourceExhaustionRetry:
    @pytest.mark.asyncio
    async def test_pthread_failure_is_retried_then_succeeds(self):
        insts = _Instances([
            RuntimeError("pthread_create: Resource temporarily unavailable (11)"),
            None,
        ])
        res = await _run(insts)
        assert res["error"] is None
        assert res["attempts_used"] == 2
        assert insts.launches == 2, "the transient launch was retried"

    @pytest.mark.asyncio
    async def test_eagain_signature_is_retried(self):
        insts = _Instances([OSError("fork: EAGAIN"), None])
        res = await _run(insts)
        assert res["error"] is None
        assert insts.launches == 2

    @pytest.mark.asyncio
    async def test_a_genuine_launch_error_is_not_retried(self):
        insts = _Instances([RuntimeError("proxy is unroutable")])
        with pytest.raises(RuntimeError, match="unroutable"):
            await _run(insts)
        assert insts.launches == 1, "a genuine error fails fast, no wasted attempts"

    @pytest.mark.asyncio
    async def test_resource_exhaustion_on_the_last_attempt_surfaces(self):
        # Two attempts, both exhausted: it must raise, not loop forever.
        insts = _Instances([
            RuntimeError("Resource temporarily unavailable"),
            RuntimeError("Resource temporarily unavailable"),
        ])
        with pytest.raises(RuntimeError, match="Resource temporarily unavailable"):
            await _run(insts, attempts=2)
        assert insts.launches == 2


class TestOnLaunchCallback:
    @pytest.mark.asyncio
    async def test_on_launch_fires_with_the_instance_once_open(self):
        insts = _Instances([None])
        seen = []
        await scrape_with_retry(
            insts, profile="task-1", owner="job:x", wait_ms=0, attempts=1,
            scrape_once=_ok, warmup_url=None, on_launch=lambda inst: seen.append(inst.id),
        )
        assert seen == ["inst-1"], "the caller is told when the browser is obtained"
