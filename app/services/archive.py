"""Archiving one page's readable content into a Notion page.

Blocking, unlike a sweep, because it fits: one page load, one extraction, one
append — roughly 40–60s. That sits inside Claude.ai's wall but right on top of
Claude Code's 60s default, which is a documentation problem rather than a design
one (`MCP_TOOL_TIMEOUT`).

It records itself as a task (models.ArchiveTask, on the volume via the job
store) for the same reason it is one: a leased identity, the same admission
gate, a browser open for a minute. Before that record existed the Tasks tab
showed nothing at all while an archive ran, which read as an idle server.

It runs on a leased ``task-N`` identity from the shared pool, exactly as a sweep
does — see services/task_profiles.py. It used to launch on ``archive-<host+path>``,
a durable profile minted per URL and never cleaned up, so the profile list grew by
one for every distinct page anyone ever archived.

**The Notion write scope is deliberately tiny**: this appends blocks to a page
the caller already named, and does nothing else. No page is created, no property
is set, no parent is chosen. The caller decided where this goes; we only fill it
in — and only if the extraction fully succeeded, so a blocked page can never
append a header announcing content that is not there.

It comes in two halves a sweep's triage reuses separately: `read` (the browser —
gate, pooled identity, retries, extraction; no Notion and no task record) and
`append` (the Notion write). `archive` is read, then the guard, then append.

**Appending is idempotent.** A page that already carries the section `prelude`
writes (a "Source Content" heading) is left alone, so a repeated call — or a
triage run over a row someone archived by hand earlier — never files the page
twice. `archive` looks for the section before it opens a browser at all, so a
repeat call costs one Notion read instead of a minute of page loading. The
check and the append run under one lock per Notion page, so an archive_page
call and a sweep's triage (or two sweeps) filing the same row at the same
moment cannot both find it empty and both append.

That makes a half-written section dangerous: the heading goes in the first
request of a long page, so a page whose later request failed would carry the
heading forever and be skipped as archived. So a failed append takes back what
it had already written (Notion reports the new blocks' ids; each is deleted),
and when that is impossible it says the page needs the section removed by hand.

**The guard.** A page can load "successfully" and still be a login wall, a
cookie screen, a 404, a "this listing has been removed" notice or an anti-bot
interstitial that the blocker's phrase list does not know. When a TypeSafe
Classifier key is saved, one yes/no question about the first few thousand
characters decides whether this is the page's real content, and below
GUARD_THRESHOLD nothing is written. Without a key there is no guard, and an
unreachable classifier never stops an archive: the guard protects the Notion
page from junk; it is not allowed to become the reason nothing gets archived.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import CONFIG
from ..models import ArchiveResult, ArchiveTask
from .blocker import text_contains_blocker
from .browsing import capture, gesture, scrape_with_retry, slug
from .extract import extract, md_to_blocks, prelude
from .jobs import JobStore
from .settings import SettingsService
from .task_profiles import TaskProfilePool
from .typesafe import TypeSafeError

logger = logging.getLogger("cloakbiz.archive")

_WAIT_MS = 12_000
_ATTEMPTS = 3
DEFAULT_HEADING = "Source Content"
# Notion's hard cap per request.
_BLOCKS_PER_REQUEST = 100


# ── the guard ───────────────────────────────────────────────────────────────
# Kept generic — "is this the page's real content?", not "is this a listing?" —
# because archive_page archives any page. The wording was checked live against
# real pages: a listing's own text scores ~0.96; a 404, a removed listing, a login
# wall, a cookie screen, an anti-bot check and an NDA gate all score 0.02–0.04.
# The threshold sits far from both clusters on purpose.
GUARD_QUESTION = (
    "The text is the real content of the page — for example a business listing's "
    "details — not a login or sign-up wall, a cookie or consent screen, an error or "
    "not-found page, a removed or no-longer-available notice, or an anti-bot check."
)
GUARD_THRESHOLD = 0.3
# What the question reads: the top of the page, where every wall and notice
# above is. More would cost tokens without changing the answer.
GUARD_CHARS = 4000
# archive_page's guard is best-effort (an unanswered guard archives anyway), so
# it gets one short attempt: the full retry budget made an outage cost every
# archive two more minutes, only to archive unchecked at the end.
GUARD_ATTEMPTS = 1
GUARD_TIMEOUT_SEC = 10.0


def guard_state(markdown: str) -> dict[str, str]:
    """The state the guard question is asked about. Exposed so a caller can
    bundle the guard into a request that asks other questions about the page."""
    return {"page_text": (markdown or "")[:GUARD_CHARS]}


def guard_question() -> dict[str, str]:
    """The guard as a TypeSafe question, for bundling into a wider `ask`."""
    return {"type": "noul", "instructions": GUARD_QUESTION}


async def guard(typesafe, markdown: str, *, attempts: int | None = None,
                timeout: float | None = None) -> float:
    """P(the page is its real content), from one classifier question.

    Raises TypeSafeError when the classifier cannot answer; what that means is
    the caller's decision (archive_page archives anyway). `attempts`/`timeout`
    override the client's retry budget for this one question."""
    budget = {k: v for k, v in (("attempts", attempts), ("timeout", timeout)) if v is not None}
    return await typesafe.noul(guard_state(markdown), GUARD_QUESTION, **budget)


def guard_refusal(probability: float) -> str:
    """What a page the guard stopped is reported as."""
    return (
        f"The page doesn't look like its real content (P={probability:.2f}: a login "
        f"wall, error, removed listing or anti-bot page?) — nothing was written."
    )


@dataclass
class PageRead:
    """What reading one page produced — the browser half of an archive.

    No Notion and no task record: a sweep's triage reads detail pages with this
    too, under its own job's evidence. `failure` says in plain words why the read
    is unusable, or None when `markdown` is the page.
    """

    url: str = ""
    title: str = ""
    markdown: str = ""
    used_path: str = ""
    blocked: bool = False
    error: str | None = None
    attempts_used: int = 0
    evidence_dir: str = ""

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def failure(self) -> str | None:
        if self.blocked:
            host = urlparse(self.url).hostname or "The site"
            return (
                f"{host} served an anti-bot page instead of the page's content, on every "
                f"attempt and each from a different exit IP."
            )
        if self.error:
            return self.error
        if not self.markdown.strip():
            return "The page loaded but no readable content came out of it."
        return None


@dataclass(frozen=True)
class AppendResult:
    """What `append` did to the Notion page: blocks written, or nothing because
    the page already had its archived section."""

    blocks_appended: int = 0
    already_archived: bool = False


def describe(task: ArchiveTask) -> str:
    """Name an archive task for the dashboard — the counterpart of
    `scrape.describe`, colocated with the task it names (see the note there).

    The title once the page has been read, the host until then: while it is
    running there is nothing else to say, and afterwards the title is what the
    person recognises the row by. Truncated, because a listing page's <title>
    can be a paragraph.
    """
    subject = task.title.strip()
    if not subject:
        target = task.urls[0] if task.urls else ""
        subject = urlparse(target).hostname or target or "a page"
    if len(subject) > 60:
        subject = subject[:59].rstrip() + "…"
    return f"Archive · {subject}"


class ArchiveService:
    def __init__(self, instances, settings: SettingsService, jobs: JobStore,
                 task_profiles: TaskProfilePool | None = None, *,
                 notion_client=None, typesafe=None) -> None:
        self._instances = instances
        self._settings = settings
        # An archive is a task in every sense the dashboard means — a pooled
        # identity, the same admission gate, about a minute of browser — and it
        # was missing from the Tasks tab only because it wrote no record. Not
        # optional: a store that could be absent would make "did it show up?"
        # depend on how the service happened to be built.
        self._jobs = jobs
        # token -> a Notion client, for archive_page's own per-call writes (a
        # sweep's triage passes its shared one to `append` instead). Injectable,
        # so tests can stand in for Notion without a network.
        self._notion_client = notion_client or _default_notion_client
        # The process's TypeSafe client, asked the guard question only while a
        # key is saved (see _classifier). None: never asked.
        self._typesafe = typesafe
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
        # One lock per Notion page, held around "has it the section?" and the
        # append, so two writers of the same page (archive_page, a sweep's
        # triage) cannot both see it empty and both file it. Weak values: a
        # lock nobody holds or waits on is dropped, not kept per page forever.
        self._page_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary())

    def _page_lock(self, page_id: str) -> asyncio.Lock:
        key = page_id.replace("-", "").strip().lower()
        lock = self._page_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._page_locks[key] = lock
        return lock

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
                      heading: str = DEFAULT_HEADING) -> ArchiveResult:
        """Read `url`, check it is the real page, and append it to the Notion
        page `notion_page_id` — unless that page already has its `heading`,
        which is checked first, before any browser work."""
        url = (url or "").strip()
        notion_page_id = (notion_page_id or "").strip()
        if not url or not notion_page_id:
            return ArchiveResult(
                ok=False, url=url, notion_page_id=notion_page_id,
                error="Both a url and a notion_page_id are required — the page id says where "
                      "the content should go, and this never picks one for you.",
                summary="Nothing to do.",
            )

        host = urlparse(url).hostname or "unknown"

        # Write the task down BEFORE the browser starts, so the minute it spends
        # running is a minute it is visible under "Running now" rather than a
        # minute of nothing happening. The record is also what makes the capture
        # below reachable: evidence goes under the task id, exactly like a
        # sweep's, so /runs/<id>/evidence serves it and dropping the record takes
        # it along. (It used to go under `archive/<url slug>`, which no page
        # could reach and only "Clear task history" could ever remove.)
        task = self._jobs.create(
            kind="archive", status="working", urls=[url], notion_page_id=notion_page_id,
            summary=f"Archiving {host}…",
        )
        evidence = CONFIG.evidence_dir / task.id
        task.evidence_dir = str(evidence)
        self._jobs.save(task)

        try:
            result = await self._archive(url, notion_page_id, heading, host, evidence)
        except BaseException as exc:  # noqa: BLE001 — the task must record its own end
            # Including CancelledError: a caller that disconnects mid-archive
            # leaves a task nothing is advancing, and a record stuck on "working"
            # is the exact lie `adopt` exists to stop telling.
            self._record_crash(task, exc)
            raise
        self._record(task, result)
        return result

    def _record(self, task: ArchiveTask, result: ArchiveResult) -> None:
        """Close the task out with what the archive actually did.

        The status and the wording come straight off the ArchiveResult — the
        blocked page, the empty extraction and the refused Notion write each
        already say what happened, and re-phrasing them here is how the Tasks row
        and the tool's answer start disagreeing about the same run.
        """
        task.status = "completed" if result.ok else "failed"
        task.title = result.title
        task.blocks_appended = result.blocks_appended
        task.used_path = result.used_path
        task.evidence_dir = result.evidence_dir or task.evidence_dir
        task.error = result.error
        task.summary = result.summary
        self._jobs.save(task)

    def _record_crash(self, task: ArchiveTask, exc: BaseException) -> None:
        cancelled = isinstance(exc, asyncio.CancelledError)
        task.status = "failed"
        task.error = (
            "The caller went away before this finished, so it was stopped. Nothing was "
            "written to the Notion page."
            if cancelled else str(exc) or exc.__class__.__name__
        )
        task.summary = "Archive stopped early." if cancelled else "Archive failed."
        self._jobs.save(task)

    def _classifier(self):
        """The TypeSafe client while a key is saved right now, else None — so a
        key added or removed in Settings applies to the next archive."""
        if self._typesafe is None:
            return None
        return self._typesafe if self._settings.load().typesafe_configured() else None

    async def read(self, url: str, evidence_dir: Path, *, owner: str | None = None) -> PageRead:
        """Load `url` on a pooled identity and extract its readable content.

        The browser half of an archive, and all of it: the admission gate, a
        leased ``task-N`` identity, retries past blocks on fresh exit IPs, and
        the extraction, with evidence under `evidence_dir`. Nothing is written
        to Notion and no task is recorded — the caller owns both (archive_page
        records an ArchiveTask; a sweep keeps it under its own job).
        """
        parsed = urlparse(url)
        host = parsed.hostname or "unknown"
        key = slug((host + parsed.path) or host)
        # Lease a pooled task-N identity. The lease key is per CALL, not per URL:
        # what has to stay distinct is two reads running at once, and two reads
        # of the SAME url are exactly the case the old per-URL profile name
        # could not keep apart — they shared a user-data-dir and collided on
        # Chromium's singleton lock. The pool is the sole lease authority, so
        # distinct leases mean distinct profiles. Acquired before any launch and
        # released in the finally, so a launch failure returns it too.
        lease_key = f"archive:{key}:{uuid.uuid4().hex[:8]}"
        await self._enter_gate()
        try:
            profile = self._task_profiles.acquire(lease_key)
            try:
                res = await scrape_with_retry(
                    self._instances, profile=profile, owner=owner or f"archive:{key}",
                    wait_ms=_WAIT_MS, attempts=_ATTEMPTS,
                    scrape_once=lambda inst, page: self._extract_once(inst, page, url, evidence_dir),
                )
            finally:
                self._task_profiles.release(lease_key)
        finally:
            # Shielded for the same reason the sweep gate is: a caller that
            # disconnects mid-read must not leave the gate counter permanently
            # short, or later reads queue behind a slot nobody holds.
            await asyncio.shield(self._leave_gate())

        data = res.get("data") or {}
        return PageRead(
            url=url, title=data.get("title") or "", markdown=data.get("markdown") or "",
            used_path=data.get("used_path") or "", blocked=bool(res.get("blocked")),
            error=res.get("error") or None, attempts_used=res.get("attempts_used", 0),
            evidence_dir=str(evidence_dir),
        )

    async def append(self, client, page_id: str, markdown: str, url: str,
                     heading: str = DEFAULT_HEADING) -> AppendResult:
        """Append `markdown` to a Notion page under the `prelude` header — once.

        When the page already has that header's `heading` among its blocks it is
        left exactly as it is and `already_archived` says so: the section is
        there, whether this server or an earlier agent put it there. `client` is
        the caller's: archive_page makes one per call, a sweep's triage passes the
        one it shares for its whole phase. The check and the write hold the
        page's lock. A Notion failure raises; one part-way through is rolled
        back first (see `_append_blocks`).
        """
        async with self._page_lock(page_id):
            if await has_section(client, page_id, heading):
                return AppendResult(already_archived=True)
            blocks = prelude(url, heading) + await md_to_blocks(markdown, url)
            return AppendResult(
                blocks_appended=await _append_blocks(client, page_id, blocks, heading))

    async def _archive(self, url: str, notion_page_id: str, heading: str,
                       host: str, evidence: Path) -> ArchiveResult:
        result = ArchiveResult(url=url, notion_page_id=notion_page_id,
                               evidence_dir=str(evidence))
        # The Notion page first, before a minute of browser work: a page that
        # already has its section needs nothing read (a repeat call returns in
        # a second), and a page that can't be opened would refuse the append
        # anyway. `append` checks again — this is only the fast path.
        try:
            client = self._notion_client(self._settings.load().notion_api_token)
            already = await has_section(client, notion_page_id, heading)
        except Exception as exc:  # noqa: BLE001
            result.error = (
                f"Couldn't open the Notion page to archive into, so {host} was not read and "
                f"nothing was written. {exc}"
            )
            result.summary = "Could not open the Notion page; nothing read or written."
            return result
        if already:
            result.ok = True
            result.summary = (
                f"Nothing appended: the Notion page already has a '{heading}' section, so "
                f"{url} is already archived there."
            )
            return result

        read = await self.read(url, evidence)
        result.title = read.title
        result.used_path = read.used_path
        result.attempts_used = read.attempts_used
        if read.blocked:
            result.error = (
                f"{host} served an anti-bot page instead of the listing, on every attempt "
                f"and each from a different exit IP. Nothing was written to Notion. This "
                f"usually clears on its own — try again shortly."
            )
            result.summary = "Blocked by the site; nothing written."
            return result
        if read.error:
            result.error = read.error
            result.summary = "Could not read the page; nothing written."
            return result

        markdown = read.markdown
        if not markdown.strip():
            result.error = (
                "The page loaded but no readable content came out of it, so there was "
                "nothing to archive and nothing was written."
            )
            result.summary = "Empty extraction; nothing written."
            return result

        note = ""
        classifier = self._classifier()
        if classifier is not None:
            try:
                p_real = await guard(classifier, markdown, attempts=GUARD_ATTEMPTS,
                                     timeout=GUARD_TIMEOUT_SEC)
            except Exception as exc:  # noqa: BLE001 — the guard must never stop an archive
                if isinstance(exc, TypeSafeError):
                    logger.warning("archive guard unavailable for %s: %s", host, exc)
                else:
                    logger.exception("archive guard failed unexpectedly for %s", host)
                note = (
                    f" The check that this is the page's real content could not run "
                    f"({exc}), so it was archived unchecked."
                )
            else:
                _record_guard(evidence, p_real, len(markdown))
                if p_real < GUARD_THRESHOLD:
                    result.error = guard_refusal(p_real)
                    result.summary = "Not the page's real content; nothing written."
                    return result

        # The Notion write sits OUTSIDE the retry loop on purpose: a Notion
        # failure is not a browser block, and re-scraping would risk appending
        # the page twice for a problem re-scraping cannot fix.
        try:
            appended = await self.append(client, notion_page_id, markdown, url, heading)
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.summary = "Read the page, but could not write it to Notion."
            return result

        result.ok = True
        result.markdown_chars = len(markdown)
        if appended.already_archived:
            result.summary = (
                f"Nothing appended: the Notion page already has a '{heading}' section, "
                f"so '{result.title or url}' is already archived there."
            )
        else:
            result.blocks_appended = appended.blocks_appended
            result.summary = (
                f"Archived '{result.title or url}' into Notion ({appended.blocks_appended} blocks)."
            )
        result.summary += note
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


def _default_notion_client(token: str):
    """A fresh client per archive_page call. Imported here, not at the top, so
    the browser half of this module never needs the Notion store loaded."""
    from ..stores.notion import NotionClient

    return NotionClient(token)


def _record_guard(evidence: Path, probability: float, chars: int) -> None:
    """Keep the guard's verdict with the run's other evidence, so a page it
    stopped can be checked against what it actually said."""
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "guard.json").write_text(json.dumps({
            "question": GUARD_QUESTION, "p_real_content": round(probability, 4),
            "threshold": GUARD_THRESHOLD, "chars_read": min(chars, GUARD_CHARS),
            "chars_total": chars,
        }, indent=2, ensure_ascii=False))
    except OSError:
        logger.warning("could not record the archive guard's verdict under %s", evidence)


def _plain_text(block: dict[str, Any]) -> str:
    body = block.get(block.get("type") or "", {}) or {}
    return "".join(
        part.get("plain_text") or (part.get("text") or {}).get("content", "")
        for part in body.get("rich_text") or []
    )


async def has_section(client, page_id: str, heading: str = DEFAULT_HEADING) -> bool:
    """Whether a Notion page already carries the archived section `prelude`
    writes: a top-level Heading 1 whose text is `heading`.

    Pages through every top-level block, because an archived section can sit
    below a hundred blocks of someone's own notes. Stops at the first match.
    """
    wanted = heading.strip()
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = await client.request("GET", f"/blocks/{page_id}/children", params=params)
        for block in data.get("results", []):
            if block.get("type") == "heading_1" and _plain_text(block).strip() == wanted:
                return True
        if not data.get("has_more") or not data.get("next_cursor"):
            return False
        cursor = data["next_cursor"]


async def _append_blocks(client, page_id: str, blocks: list[dict],
                         heading: str = DEFAULT_HEADING) -> int:
    """Append children to a page, all of them or none.

    Chunked to Notion's 100-block cap. The heading is in the first chunk, so a
    later chunk that fails would leave a section that `has_section` finds on
    every later call — a half-archived page skipped as archived for good. So a
    failure after the first chunk deletes what this call appended (the ids
    Notion returned for each chunk) and raises saying the page is as it was;
    when that cannot be done, it raises saying the page is partly written and
    the section must be removed by hand.
    """
    from ..stores.notion import NotionError

    appended = 0
    written: list[str] | None = []   # ids of the blocks this call added; None: unknown
    for i in range(0, len(blocks), _BLOCKS_PER_REQUEST):
        chunk = blocks[i:i + _BLOCKS_PER_REQUEST]
        try:
            reply = await client.request("PATCH", f"/blocks/{page_id}/children",
                                         json={"children": chunk})
        except NotionError as exc:
            if not appended:
                raise NotionError(f"Notion refused the append, so nothing was written: {exc}"
                                  ) from exc
            raise await _roll_back(client, written, appended, heading, exc) from exc
        ids = _new_ids(reply, chunk)
        written = None if written is None or ids is None else written + ids
        appended += len(chunk)
    return appended


def _new_ids(reply: Any, chunk: list[dict]) -> list[str] | None:
    """The ids of the blocks one append created, or None when the reply does not
    say which they are.

    Notion answers an append with the new blocks. Only a reply that is exactly
    those — as many as were sent, of the same types, in order — is trusted: a
    reply listing anything else (an older API answered with the page's
    children) would have the rollback delete someone's own blocks.
    """
    results = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(results, list) or len(results) != len(chunk):
        return None
    ids = []
    for sent, got in zip(chunk, results):
        if not isinstance(got, dict) or not got.get("id") or got.get("type") != sent.get("type"):
            return None
        ids.append(str(got["id"]))
    return ids


async def _roll_back(client, written: list[str] | None, appended: int, heading: str,
                     exc: Exception) -> Exception:
    """Delete the blocks a failed append had written; the error to raise either way."""
    from ..stores.notion import NotionError, NotionNotFound

    left = appended
    if written is not None:
        left = 0
        for block_id in reversed(written):
            try:
                await client.request("DELETE", f"/blocks/{block_id}")
            except NotionNotFound:
                pass  # already gone
            except Exception as err:  # noqa: BLE001 — counted, and said below
                logger.warning("could not delete block %s while rolling back: %s", block_id, err)
                left += 1
        if not left:
            return NotionError(
                f"Notion accepted {appended} block(s) and then refused the rest, so the "
                f"{appended} it had accepted were deleted again: the page is as it was, and "
                f"archiving it again is safe. ({exc})"
            )
    return NotionError(
        f"Notion accepted {appended} block(s) and then refused the rest, and "
        f"{'they' if left == appended else f'{left} of them'} could not be removed again, so "
        f"the page is partly written: its '{heading}' section holds only part of the page. "
        f"Delete that section from the Notion page by hand — until then the page counts as "
        f"archived and is skipped. ({exc})"
    )
