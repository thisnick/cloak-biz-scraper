"""Archiving one page's readable content into a Notion page.

Blocking, unlike a sweep, because it fits: one page load, one extraction, one
append — roughly 40–60s. That sits inside Claude.ai's wall but right on top of
Claude Code's 60s default, which is a documentation problem rather than a design
one (`MCP_TOOL_TIMEOUT`).

It runs on a leased ``task-N`` identity from the shared pool, exactly as a sweep
does — see services/task_profiles.py. It used to launch on ``archive-<host+path>``,
a durable profile minted per URL and never cleaned up, so the profile list grew by
one for every distinct page anyone ever archived.

**The Notion write scope is deliberately tiny**: this appends blocks to a page
the caller already named, and does nothing else. No page is created, no property
is set, no parent is chosen. The caller decided where this goes; we only fill it
in — and only if the extraction fully succeeded, so a blocked page can never
append a header announcing content that is not there.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..config import CONFIG
from ..models import ArchiveResult
from .blocker import text_contains_blocker
from .browsing import capture, gesture, scrape_with_retry, slug
from .extract import extract, md_to_blocks, prelude
from .settings import SettingsService
from .task_profiles import TaskProfilePool

logger = logging.getLogger("cloakbiz.archive")

_WAIT_MS = 12_000
_ATTEMPTS = 3
_DEFAULT_HEADING = "Source Content"
# Notion's hard cap per request.
_BLOCKS_PER_REQUEST = 100


class ArchiveService:
    def __init__(self, instances, settings: SettingsService, appender=None,
                 task_profiles: TaskProfilePool | None = None) -> None:
        self._instances = instances
        self._settings = settings
        self._append = appender or _notion_append
        # The instance manager's pool, shared with sweeps. Archiving used to mint
        # a durable ``archive-<host+path>`` profile per URL, which is the same
        # unbounded accumulation task_profiles.py was written to end for sweeps:
        # a hundred archived pages left a hundred cookie jars on the volume
        # forever, each cold on first use. Injectable for tests.
        self._task_profiles = task_profiles
        if self._task_profiles is None and instances is not None:
            self._task_profiles = instances.task_profiles
        # Admission gate, for the same reason ScrapeService has one: a profile is
        # leased BEFORE the launch that waits for a pool slot, so without a bound
        # here the number of leases held at once — and therefore the number of
        # task-N profiles ever minted — would track how many archive calls happen
        # to overlap rather than the pool budget. Excess archives queue here; they
        # would have queued on the instance slot a moment later anyway. Re-read
        # from settings on each wake so the Pool setting applies without a
        # restart. Sweeps are bounded separately by their own gate, so a volume
        # where both saturate at once can hold up to two budgets' worth of leases;
        # that is the ceiling on minting, and every one of them is reused after.
        self._gate = asyncio.Condition()
        self._past_gate = 0

    async def _enter_gate(self) -> None:
        async with self._gate:
            await self._gate.wait_for(
                lambda: self._past_gate < self._settings.load().task_budget
            )
            self._past_gate += 1

    async def _leave_gate(self) -> None:
        async with self._gate:
            self._past_gate -= 1
            self._gate.notify_all()

    async def archive(self, url: str, notion_page_id: str,
                      heading: str = _DEFAULT_HEADING) -> ArchiveResult:
        url = (url or "").strip()
        notion_page_id = (notion_page_id or "").strip()
        if not url or not notion_page_id:
            return ArchiveResult(
                ok=False, url=url, notion_page_id=notion_page_id,
                error="Both a url and a notion_page_id are required — the page id says where "
                      "the content should go, and this never picks one for you.",
                summary="Nothing to do.",
            )

        parsed = urlparse(url)
        host = parsed.hostname or "unknown"
        # Still per-URL for evidence, which is a record of one page.
        key = slug((host + parsed.path) or host)
        evidence = CONFIG.evidence_dir / "archive" / key

        # Lease a pooled task-N identity. The lease key is per CALL, not per URL:
        # what has to stay distinct is two archives running at once, and two
        # archives of the SAME url are exactly the case the old per-URL profile
        # name could not keep apart — they shared a user-data-dir and collided on
        # Chromium's singleton lock. The pool is the sole lease authority, so
        # distinct leases mean distinct profiles. Acquired before any launch and
        # released in the finally, so a launch failure returns it too.
        lease_key = f"archive:{key}:{uuid.uuid4().hex[:8]}"
        await self._enter_gate()
        try:
            profile = self._task_profiles.acquire(lease_key)
            try:
                res = await scrape_with_retry(
                    self._instances, profile=profile, owner=f"archive:{key}",
                    wait_ms=_WAIT_MS, attempts=_ATTEMPTS,
                    scrape_once=lambda inst, page: self._extract_once(inst, page, url, evidence),
                )
            finally:
                self._task_profiles.release(lease_key)
        finally:
            # Shielded for the same reason the sweep gate is: a caller that
            # disconnects mid-archive must not leave the gate counter permanently
            # short, or later archives queue behind a slot nobody holds.
            await asyncio.shield(self._leave_gate())

        data = res.get("data") or {}
        result = ArchiveResult(
            url=url, notion_page_id=notion_page_id, title=data.get("title") or "",
            used_path=data.get("used_path") or "", attempts_used=res.get("attempts_used", 0),
            evidence_dir=str(evidence),
        )
        if res.get("blocked"):
            result.error = (
                f"{host} served an anti-bot page instead of the listing, on every attempt "
                f"and each from a different exit IP. Nothing was written to Notion. This "
                f"usually clears on its own — try again shortly."
            )
            result.summary = "Blocked by the site; nothing written."
            return result
        if res.get("error"):
            result.error = res["error"]
            result.summary = "Could not read the page; nothing written."
            return result

        markdown = data.get("markdown") or ""
        if not markdown.strip():
            result.error = (
                "The page loaded but no readable content came out of it, so there was "
                "nothing to archive and nothing was written."
            )
            result.summary = "Empty extraction; nothing written."
            return result

        # The Notion write sits OUTSIDE the retry loop on purpose: a Notion
        # failure is not a browser block, and re-scraping would risk appending
        # the page twice for a problem re-scraping cannot fix.
        try:
            blocks = prelude(url, heading) + await md_to_blocks(markdown, url)
            appended = await self._append(self._settings.load().notion_api_token,
                                          notion_page_id, blocks)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.summary = "Read the page, but could not write it to Notion."
            return result

        result.ok = True
        result.blocks_appended = appended
        result.markdown_chars = len(markdown)
        result.summary = (
            f"Archived '{result.title or url}' into Notion ({appended} blocks)."
        )
        return result

    async def _extract_once(self, inst, page, url: str, evidence: Path) -> dict:
        inst.touch()
        await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(_WAIT_MS)
        await gesture(page)

        title = await page.title()
        body = ""
        try:
            body = await page.locator("body").inner_text(timeout=8000)
        except Exception:
            pass
        if text_contains_blocker(body, title):
            await capture(page, evidence / "blocked",
                          {"url": url, "reason": "blocked", "proxy_ip": inst.proxy_ip})
            return {"blocked": True, "error": None, "data": {}}

        res = await extract(page, url=url)
        if res.get("error"):
            await capture(page, evidence / "error",
                          {"url": url, "reason": res["error"], "proxy_ip": inst.proxy_ip})
            return {"blocked": False, "error": res["error"], "data": {}}

        files = await capture(page, evidence / "final",
                              {"url": url, "reason": "success",
                               "used_path": res.get("usedPath"), "proxy_ip": inst.proxy_ip})
        (evidence / "final" / "article.md").write_text(res.get("markdown", ""), encoding="utf-8")
        files["article"] = str(evidence / "final" / "article.md")
        return {
            "blocked": False, "error": None,
            "data": {"title": res.get("title"), "byline": res.get("byline"),
                     "used_path": res.get("usedPath"), "markdown": res.get("markdown", ""),
                     "files": files},
        }


async def _notion_append(token: str, page_id: str, blocks: list[dict]) -> int:
    """Append children to a page — the only Notion mutation this module makes.

    Chunked to Notion's 100-block cap. A failure mid-way reports how many blocks
    already landed, because the page is then genuinely half-written and saying
    otherwise would send someone looking for content that is not there.
    """
    from ..stores.notion import NotionClient, NotionError

    client = NotionClient(token)
    appended = 0
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        chunk = blocks[i:i + _BLOCKS_PER_REQUEST]
        try:
            await client.request("PATCH", f"/blocks/{page_id}/children", json={"children": chunk})
        except NotionError as exc:
            raise NotionError(
                f"Notion accepted {appended} block(s) and then refused the rest, so the page "
                f"is partly written: {exc}"
            ) from exc
        appended += len(chunk)
    return appended
