"""Shared browser plumbing: open an instance, drive it, retry past a block.

Ported from browserd (app/tasks/common.py). Concurrency is the pool's job, not
the caller's: each target acquires a task slot (`origin="task"`, `wait=True`)
which blocks on the budget (max − reserve), so a sweep can never starve the
interactive slots an agent or a human needs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from ..models import InstanceCreate

logger = logging.getLogger("cloakbiz.browsing")

# A browser launch that fails because the container is out of process/thread
# resources — not because anything is wrong with the request. This is the OS
# refusing a new thread (``pthread_create``) or process (``fork``) with EAGAIN,
# which surfaces as "Resource temporarily unavailable". It means the pool is
# oversubscribed for this container (see capacity.py), and the right response is
# to back off and retry a slot rather than fail the whole sweep: a misconfigured
# ``max_instances`` should degrade to slow, not broken. Scoped tightly so a
# genuine launch error (a bad proxy, an unresolvable binary) is never retried.
_RESOURCE_EXHAUSTION = re.compile(
    r"Resource temporarily unavailable|pthread_create|\bEAGAIN\b", re.IGNORECASE
)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:50] or "x"


async def gesture(page) -> None:
    """A small mouse move and scroll. Cheap, and pages that watch for interaction
    behave differently without it."""
    try:
        await page.mouse.move(520, 130)
        await page.mouse.wheel(0, 450)
    except Exception:
        pass


async def warmup(page, wait_ms: int, url: str) -> None:
    """Land on the site's own homepage before asking it for anything.

    Arriving cold on a deep search URL is itself a signal; a first-party referrer
    and a set of cookies is what a person's browser would have.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_timeout(max(0, wait_ms - 1500))
    await gesture(page)
    await page.wait_for_timeout(min(1500, wait_ms))


async def capture(page, directory: Path, meta: dict) -> dict:
    """Evidence for one attempt: what we asked for, and what came back.

    Every step is individually guarded because this runs on the failure path,
    where the page is often half-dead — and evidence that throws while recording
    a block would destroy the only record of it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    files: dict[str, str] = {}
    try:
        body = await page.locator("body").inner_text(timeout=8000)
        (directory / "snapshot.txt").write_text(body, encoding="utf-8")
        files["snapshot"] = str(directory / "snapshot.txt")
    except Exception:
        pass
    try:
        (directory / "page.html").write_text(await page.content(), encoding="utf-8")
        files["html"] = str(directory / "page.html")
    except Exception:
        pass
    try:
        await page.screenshot(path=str(directory / "page.png"), full_page=True)
        files["screenshot"] = str(directory / "page.png")
    except Exception:
        pass
    return files


def first_page(inst):
    return inst.context.pages[0] if inst.context.pages else None


async def open_instance(instances, profile: str, owner: str, wait_ms: int,
                        warmup_url: str | None, on_launch=None):
    inst = await instances.launch(
        InstanceCreate(profile=profile, headed=True, humanize=True),
        origin="task", owner=owner, wait=True,
    )
    # The browser is in hand — the slot wait (if any) is over. Let the caller
    # react (e.g. flip a "waiting for a slot" job summary to "scraping") before
    # the warmup, which itself takes real time on the page.
    if on_launch is not None:
        on_launch(inst)
    page = first_page(inst) or await inst.context.new_page()
    if warmup_url:
        await warmup(page, wait_ms, warmup_url)
    return inst, page


async def scrape_with_retry(instances, *, profile: str, owner: str, wait_ms: int,
                            attempts: int, scrape_once,
                            warmup_url: str | None = None, on_launch=None) -> dict:
    """Open an instance, run `scrape_once(inst, page)`, and retry past blocks.

    On a blocked or errored result: stop the instance, **rotate the profile's
    Evomi session token** so the next launch gets a fresh sticky exit IP, and
    back off briefly. A block almost always means the exit IP was flagged, so the
    IP is the lever that matters — retrying on the same one just spends time.
    Rotating keeps the profile's cookies and warmth; only the exit changes.

    A launch that fails with a resource-exhaustion signature (the container out
    of threads/processes) is treated as a *transient capacity* failure, not a
    sweep failure: back off within these same attempts and try for a slot again,
    so a pool set larger than the container can run degrades to slow rather than
    broken. Any other launch error is genuine and propagates immediately —
    retrying a bad proxy or an unresolvable binary just wastes attempts.
    """
    last: dict = {"blocked": False, "error": "no attempts run", "data": {}}
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1:
            instances.profiles.rotate_session(profile)
            await asyncio.sleep(min(10, 3 * (attempt - 1)))
        try:
            inst, page = await open_instance(
                instances, profile, owner, wait_ms, warmup_url, on_launch,
            )
        except Exception as exc:  # noqa: BLE001
            if _RESOURCE_EXHAUSTION.search(str(exc)) and attempt < max(1, attempts):
                # Out of OS resources: this slot could not be launched, but a
                # later attempt (after browsers free up) may. Record it and
                # retry rather than failing the sweep.
                logger.warning(
                    "attempt %d for %s hit resource exhaustion, backing off: %s",
                    attempt, profile, exc,
                )
                last = {"blocked": False, "error": str(exc), "data": {},
                        "attempts_used": attempt}
                continue
            raise
        try:
            res = await scrape_once(inst, page)
        except Exception as exc:  # noqa: BLE001 — an attempt failing is not the sweep failing
            logger.warning("attempt %d for %s failed: %s", attempt, profile, exc)
            res = {"blocked": False, "error": str(exc), "data": {}}
        res["instance_id"] = inst.id
        res["proxy_ip"] = inst.proxy_ip
        res["attempts_used"] = attempt
        await instances.stop(inst.id)
        last = res
        if not res.get("blocked") and not res.get("error"):
            return res
    return last
