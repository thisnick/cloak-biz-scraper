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
                        warmup_url: str | None):
    inst = await instances.launch(
        InstanceCreate(profile=profile, headed=True, humanize=True),
        origin="task", owner=owner, wait=True,
    )
    page = first_page(inst) or await inst.context.new_page()
    if warmup_url:
        await warmup(page, wait_ms, warmup_url)
    return inst, page


async def scrape_with_retry(instances, *, profile: str, owner: str, wait_ms: int,
                            attempts: int, scrape_once,
                            warmup_url: str | None = None) -> dict:
    """Open an instance, run `scrape_once(inst, page)`, and retry past blocks.

    On a blocked or errored result: stop the instance, **rotate the profile's
    Evomi session token** so the next launch gets a fresh sticky exit IP, and
    back off briefly. A block almost always means the exit IP was flagged, so the
    IP is the lever that matters — retrying on the same one just spends time.
    Rotating keeps the profile's cookies and warmth; only the exit changes.
    """
    last: dict = {"blocked": False, "error": "no attempts run", "data": {}}
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1:
            instances.profiles.rotate_session(profile)
            await asyncio.sleep(min(10, 3 * (attempt - 1)))
        inst, page = await open_instance(instances, profile, owner, wait_ms, warmup_url)
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
