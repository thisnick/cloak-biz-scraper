"""The settings UI — the core UX bet.

Railway sets exactly one variable. Everything else a user needs to configure is
here, in a form, because the audience is non-technical and the alternative is a
terminal they do not have.

Server-rendered on purpose. A settings form that saves and reports back is not a
reason to ship a build step, a bundle, and a second language into a container
whose whole pitch is "deploy this and fill in four boxes".

Routes are façades: they read a form, call a service, and render. Anything that
looks like logic belongs in services/ or stores/.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import CONFIG
from ..models import ScrapeResult
from ..services import sessions
from ..services.presentation import human_size
from ..services.ratelimit import client_key
from ..services.settings import Settings

logger = logging.getLogger("cloakbiz.ui")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


class NotAuthenticated(Exception):
    """No valid session. Handled app-wide by redirecting to the login page."""


@dataclass
class Result:
    """One banner in one section of the page.

    `level` exists because success and failure are not the whole story. A
    database that syncs everything except the prices is not a green "done" and
    not a red "broken" — it works, and it is quietly losing the column the user
    most wants to sort by. Green would hide that; red would send them fixing a
    blocker they do not have.
    """

    section: str
    ok: bool
    message: str
    detail: Any = None
    level: str = ""  # "ok" | "warn" | "bad"; derived from ok unless set

    def __post_init__(self) -> None:
        if not self.level:
            self.level = "ok" if self.ok else "bad"


def _authed(request: Request) -> bool:
    return sessions.verify(
        request.cookies.get(sessions.COOKIE_NAME), request.app.state.secret.current()
    )


def _require(request: Request) -> None:
    if not _authed(request):
        raise NotAuthenticated()


def _require_same_origin(request: Request) -> None:
    """CSRF defence-in-depth for a cookie-authenticated state change.

    The session cookie is `SameSite=lax`, which already stops a cross-site POST
    from carrying it — so this is a second layer, not the only one. It reuses the
    exact rule `/mcp` and the WS upgrades apply (routes/mcp.py `origin_allowed`),
    which matters because these actions launch browsers and spend proxy money and
    the reviewer will compare the two.

    **Absent Origin is allowed, on purpose.** `origin_allowed` returns True when
    no `Origin` is present, and that is the right policy here: a foreign `Origin`
    is the attack a browser actually sends, whereas *hard*-requiring the header
    would reject legitimate same-origin requests that omit it and break real
    users for a threat `SameSite` already covers. A present, foreign `Origin` is
    refused. Verified as `origin_allowed`'s own behaviour, not re-derived.
    """
    from .mcp import origin_allowed

    if not origin_allowed(request.headers):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def _ago(epoch: float) -> str:
    """A human "when", relative to now. The dashboard shows times to a person who
    thinks in "6 min ago", not epoch seconds."""
    if not epoch:
        return ""
    secs = max(0, int(time.time() - epoch))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return "Yesterday" if days == 1 else f"{days} days ago"


def _dur(seconds: float) -> str:
    """A short duration like 4m 12s / 21s. Never negative, never a bare float."""
    s = max(0, int(seconds))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _job_result(job) -> tuple[str, str]:
    """(css-state, label) for a finished task's Result cell. 'blocked' is called
    out because it is the failure a user can act on (rotate/retry), distinct from
    an ordinary stop.

    What "the result" IS depends on the kind of task — listings for a sweep,
    blocks appended for an archive — so each is asked its own question rather
    than one row template reaching for fields the other has never had.
    """
    if job.status == "completed":
        if job.kind == "archive":
            n = job.blocks_appended
            return "ok", f"{n} block{'' if n == 1 else 's'} appended"
        # Under sync, `job.listings` is narrowed to just the newly-inserted rows,
        # so its length is the new count, not the whole find — say so explicitly
        # rather than let a re-sweep that added nothing read as "0 listings".
        if job.synced is not None:
            return "ok", f"{job.synced.new} new, {job.synced.existing} known"
        return "ok", f"{len(job.listings)} listings"
    if job.status == "failed":
        if _blocked(job):
            return "bad", "Blocked"
        return "asleep", "Stopped"
    return "live", "Running"


def _blocked(job) -> bool:
    """Whether this task failed because the site served an anti-bot page.

    Matched per kind, not by one substring across both: an archive's Notion
    failure says "Notion accepted 3 block(s) and then refused the rest", which
    the sweep's "block" test would have labelled Blocked — sending the user to
    rotate an exit IP over a problem in their Notion page.
    """
    error = (job.error or "").lower()
    if not error:
        return False
    return "anti-bot" in error if job.kind == "archive" else "block" in error


def _job_where(job) -> str:
    """The one line under a running task's name: what it is doing right now.

    A sweep counts pages; an archive has a single page to read, so it names it.
    """
    if job.kind == "archive":
        return job.urls[0] if job.urls else ""
    return f"page {job.pages_crawled} / {job.max_pages}"


def _job_label(job) -> str:
    """The dashboard's name for one task, asked of the task itself.

    Each kind's label lives with that kind (`scrape.describe`, `archive.describe`
    — see the note in services/scrape.py); this is only the switch, so adding a
    task type never means editing a template.
    """
    from ..services.archive import describe as describe_archive
    from ..services.scrape import describe as describe_sweep

    return describe_archive(job) if job.kind == "archive" else describe_sweep(job)


def _render(request: Request, result: Result | None = None, status: int = 200,
            active: str | None = None, notion_mapping: Any = None) -> Response:
    settings: Settings = request.app.state.settings.load()
    from ..services.urls import public_base
    from ..services.views import browser_info, instance_view

    secret = request.app.state.secret.current()
    base = public_base(request)
    instances = [
        instance_view(i, secret=secret, base_url=base)
        for i in request.app.state.instances.running.values()
    ]
    jobs = request.app.state.jobs.all()
    running = [j for j in jobs if j.status == "working"]
    history = [j for j in jobs if j.status != "working"][:25]

    from ..services.profiles import DEFAULT_PROFILE

    profiles = request.app.state.instances.profiles.all()
    # Display hint only; the mutation guard is ProfileService. Include browsers
    # queued/opening/closing too so the UI does not offer an action the service
    # is about to refuse.
    profiles_in_use = request.app.state.instances.profile_names_in_use()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "s": settings,
            "result": result,
            "notion_mapping": notion_mapping,
            "pool_warning": settings.pool_warning(),
            "has_license": bool(settings.cloakbrowser_license_key),
            "browser": browser_info(settings, request.app.state.instances),
            "has_proxy_password": bool(settings.proxy_password),
            "has_notion_token": bool(settings.notion_api_token),
            "connected_apps": request.app.state.oauth.list_clients(),
            "proxy_checked_at": _when(settings.proxy_last_check_at),
            # dashboard sections
            "instances": instances,
            "running_jobs": running,
            "history_jobs": history,
            "server_url": base.rstrip("/") + "/mcp",
            "active_hint": active,
            # profiles (consumed by the New-browser dialog + Settings→Profiles)
            "profiles": profiles,
            "profiles_in_use": profiles_in_use,
            "default_profile": DEFAULT_PROFILE,
            "ago": _ago,
            "dur": _dur,
            "job_result": _job_result,
            "job_label": _job_label,
            "job_where": _job_where,
            "now": time.time(),
        },
        status_code=status,
    )


def _when(epoch: float) -> str:
    """UTC, spelled out. The container's clock is UTC and the reader's may not
    be, so an unlabelled local-looking time would be a small lie."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _first_error(exc: Exception) -> str:
    """The message a person needs, without the machinery around it.

    Pydantic's str() is written for a developer reading a traceback: it names the
    model, the field, the error class, and echoes the input — "1 validation error
    for Settings cloakbrowser_version Value error, Invalid browser version pin
    ... [type=value_error, input_value='latest']". The sentence our validators
    actually wrote is in there, and it is the only part worth showing someone who
    typed a value into a box.
    """
    from pydantic import ValidationError

    if isinstance(exc, ValidationError) and exc.errors():
        return exc.errors()[0]["msg"].removeprefix("Value error, ")
    return str(exc)


def _keep(new: str, existing: str) -> str:
    """Blank means 'leave it alone'.

    Secrets are never rendered back into the form — a saved licence key has no
    business sitting in the page source — so a blank box cannot mean "clear it"
    without every save wiping every secret the user did not retype.
    """
    return new.strip() or existing


# ── login ───────────────────────────────────────────────────────────────────


def _login_context(request: Request, error: str | None = None) -> dict[str, Any]:
    """What the login page needs — including how to get back in.

    The recovery instructions used to live only on the settings page, which is
    behind this login: the only person who could read them was the one who did
    not need them. Anyone actually locked out saw "that is not the right secret"
    and had nowhere to go.
    """
    secret_service = request.app.state.secret
    return {
        "error": error,
        "unconfigured": secret_service.current() is None,
    }


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", _login_context(request))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, secret: str = Form("")) -> Response:
    secret_service = request.app.state.secret
    if secret_service.current() is None:
        return templates.TemplateResponse(
            request, "login.html", _login_context(request), status_code=503
        )

    # Throttled before the secret is even looked at. MIN_SECRET_LENGTH allows a
    # 16-character memorable secret, and this box is on the public internet
    # guarding the browser, the proxy, and the Notion workspace — Step 3 measured
    # 30 wrong guesses in 0.0s with nothing in the way. See services/ratelimit.py
    # for what this does and does not buy.
    limiter = request.app.state.login_limiter
    key = client_key(request)
    wait = limiter.retry_after(key)
    if wait:
        logger.warning("throttled login from %s for %.1fs", key, wait)
        response = templates.TemplateResponse(
            request,
            "login.html",
            _login_context(
                request,
                f"Too many wrong attempts. Wait {int(wait) + 1} seconds and try again.",
            ),
            status_code=429,
        )
        response.headers["Retry-After"] = str(int(wait) + 1)
        return response

    if not secret_service.verify(secret):
        limiter.fail(key)
        logger.warning("failed login attempt from %s", key)
        return templates.TemplateResponse(
            request,
            "login.html",
            _login_context(request, "That is not the right secret."),
            status_code=401,
        )

    limiter.reset(key)
    response = RedirectResponse("/", status_code=303)
    _set_session(response, sessions.issue(secret_service.current()))
    return response


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        sessions.COOKIE_NAME,
        token,
        max_age=sessions.SESSION_TTL_SEC,
        httponly=True,   # the session is never a thing page scripts need to read
        secure=True,     # Railway is HTTPS; browsers treat localhost as secure too
        samesite="lax",  # blocks cross-site POSTs (CSRF) while surviving the
                         # top-level redirect Step 4's OAuth /authorize will need
        path="/",
    )


@router.post("/logout")
async def logout(request: Request) -> Response:
    # A cross-site page must not be able to force a logout. Low-severity CSRF, but
    # it is a state-changing cookie POST, so it takes the same Origin check as the
    # rest — one rule, no exceptions to remember.
    _require_same_origin(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response


# ── the settings page ───────────────────────────────────────────────────────


# The dashboard's tabs are one page; a `?view=` hint lets a server redirect land
# on a specific section (see the PRG redirects below). Unknown values fall back
# to Overview in the template, so this need not police the value.
_VIEWS = {"overview", "browsers", "tasks", "connect", "settings"}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    _require(request)
    view = request.query_params.get("view")
    return _render(request, active=view if view in _VIEWS else None)


# ── Runs: the evidence a sweep already captured, finally reachable ───────────
#
# Ported from browserd, which serves the same three shapes — and with the one
# change that matters. browserd has no auth at all, which was fine: it is a
# private sidecar on Nick's own machine. This is a public URL, and these files
# are screenshots of pages fetched through the user's optional proxy. Copying
# the routes as they stand would publish them.
#
# So the gate is the session cookie, the same one the settings pages use. On a
# single-user server that also settles the "guessable id" question properly: a
# job id is short hex and should be assumed guessable, and it does not matter,
# because holding the session *is* being the owner. Nothing rests on an id being
# hard to find.


def _evidence_root(job_id: str) -> Path:
    return (CONFIG.evidence_dir / job_id).resolve()


def _run_detail(job) -> dict[str, Any]:
    """The half of a run's JSON that only its own kind can answer.

    A sweep reports what it crawled and found; an archive reports where it wrote
    and how much. Neither is given the other's keys — an archive with
    `"listings": 0` would read as a sweep that found nothing.
    """
    if job.kind == "archive":
        return {
            "notion_page_id": job.notion_page_id, "title": job.title,
            "blocks_appended": job.blocks_appended, "used_path": job.used_path,
        }
    return {
        "source": job.source, "pages_crawled": job.pages_crawled,
        "listings": len(job.listings),
    }


@router.get("/runs")
async def list_runs(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    _require(request)
    jobs = request.app.state.jobs.all()
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return [
        {
            "job_id": j.id, "kind": j.kind, "status": j.status,
            "created_at": j.created_at, "error": j.error,
            "evidence": _evidence_root(j.id).is_dir(),
            **_run_detail(j),
        }
        for j in jobs[:limit]
    ]


@router.get("/runs/{job_id}")
async def get_run(request: Request, job_id: str) -> dict[str, Any]:
    _require(request)
    # get() validates the id's shape and returns None for anything that isn't
    # one, so a job_id of "../settings" never reaches the filesystem here.
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such run")
    root = _evidence_root(job_id)
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) \
        if root.is_dir() else []
    return {
        "job_id": job.id, "kind": job.kind, "status": job.status,
        "urls": job.urls, "error": job.error, "evidence": files,
        **_run_detail(job),
    }


@router.get("/runs/{job_id}/results", response_model=None)
async def get_run_results(request: Request, job_id: str) -> ScrapeResult | Any:
    """The full result of one task — what /runs/{job_id} only counts.

    Backs the Tasks-history "View" link, which opens this in a new tab as raw
    JSON, and every row in that table has one — so it answers for whatever kind
    the row is. For a sweep it returns the very payload
    `get_scrape_listing_results` / `ScrapeResult` exposes, built by the one
    `ScrapeResult.of` the tools use, so the dashboard and an agent can never see
    a different answer for the same job. An archive has no ScrapeResult to
    return: it hands back its own record, which is the whole of what it did.

    Same gate and id handling as the other /runs routes: the session cookie
    (`_require`) is the owner check on this public URL, and `jobs.get` returns
    None for a bad-shaped id — so a `job_id` like `../settings` is a 404, never a
    filesystem read.
    """
    _require(request)
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such run")
    if job.kind != "sweep":
        return job
    return ScrapeResult.of(job)


@router.get("/runs/{job_id}/evidence/{name:path}")
async def get_evidence(request: Request, job_id: str, name: str) -> Response:
    """Serve one captured file.

    `{name:path}` accepts slashes, so this is the classic traversal hole, and it
    is a real one rather than a theoretical one: /data holds `settings.json` and
    the `.dek` that decrypts it, two directories up from here. Serving
    `../../.dek` would hand over the licence, proxy and Notion credentials
    together.

    browserd sidesteps it by accident — it rglobs the last path segment, so the
    `..` is discarded before it means anything. That works and is impossible to
    read as deliberate. This resolves the path and requires the result to still
    be inside the run's own directory, which also covers a symlink pointing out
    (resolve() follows it, so it lands outside and is refused).
    """
    _require(request)
    if request.app.state.jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail="no such run")
    root = _evidence_root(job_id)
    target = (root / name).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        # One 404 for "escaped", "absent" and "not a file" alike: which of those
        # it was is not the caller's business.
        raise HTTPException(status_code=404, detail="no such evidence")
    return FileResponse(target)


# ── Sessions: full control from the cookie-authed page ───────────────────────
#
# The same three actions the MCP/REST tools expose, but driven by a human on the
# dashboard rather than an agent with a bearer token. They go through the one
# service layer (never logic here) so a browser started from the page and one
# started from a tool are the same object in the same pool.
#
# Every one is state-changing, so every one is POST and carries both CSRF layers:
# `_require` (the SameSite=lax session cookie) and `_require_same_origin` (a
# foreign Origin is refused). There is deliberately no GET form of any of these —
# SameSite=lax leaks the cookie on a top-level cross-site GET, so a state change
# behind GET would be reachable cross-site. The results page is /sessions, which
# these redirect to (PRG); it is built to the settled IA separately.


def _sessions_redirect(view: str = "browsers") -> Response:
    """Post/Redirect/Get back to the dashboard, landing on the relevant section.

    The dashboard is the single page at `/` (client-side tabs); there is no
    `/sessions` route, so redirecting there would send a *successful* action to a
    404 — a worse outcome than the error it was meant to avoid. `?view=` is a
    server-read hint the page honours on load."""
    return RedirectResponse(f"/?view={view}", status_code=303)


@router.post("/sessions/instances")
async def ui_new_instance(
    request: Request,
    profile: str = Form(""),
    new_profile: str = Form(""),
    country: str = Form(""),
    region: str = Form(""),
) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..models import InstanceCreate
    from ..services.geo import GeoUnresolved, ProxyUnreachable
    from ..services.instances import BrowserUnavailable, CapExceeded
    from ..services.license import LicenseNotPro
    from ..services.profiles import DEFAULT_PROFILE
    from ..services.proxy import ProxyNotConfigured
    from ..services.tokens import OWNER

    # A typed new name wins; otherwise the picked profile; otherwise the Default.
    # Defaulting to the Default (not a throwaway session-<time>) is the fix for the
    # reported confusion — clicking "+ New browser" twice reuses one identity and
    # stays logged in, instead of a fresh cookie-less browser every time.
    picked = new_profile.strip() or (profile.strip() if profile.strip() != "__new__" else "")
    req = InstanceCreate(
        profile=picked or DEFAULT_PROFILE,
        country=country.strip() or None,
        region=region.strip() or None,
    )
    # This is a form submit, so an error is shown by re-rendering the dashboard
    # with a banner (like the settings pages), NOT by raising — a raw
    # {"detail": ...} JSON body reads as "broken" to the first-boot user this
    # button is for. The status codes are unchanged (the same 429/400 the REST
    # twin returns); only the presentation differs. The REST endpoint
    # POST /api/instances keeps its JSON error — its caller is an agent, not a
    # browser. A present but unusable key is the licensing footgun this button
    # reaches: it must be a visible error, never a silent public downgrade.
    try:
        await request.app.state.instances.launch(
            req, origin="interactive", subject=OWNER
        )
    except CapExceeded as exc:
        return _render(request, Result("browsers", False, str(exc)), status=429)
    except (LicenseNotPro, ProxyNotConfigured, ProxyUnreachable,
            GeoUnresolved, BrowserUnavailable) as exc:
        return _render(request, Result("browsers", False, str(exc)), status=400)
    return _sessions_redirect()


@router.post("/sessions/sweep")
async def ui_run_sweep(
    request: Request,
    url: str = Form(""),
    max_pages: int = Form(1),
    sync: bool = Form(False),
) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..services.scrape import NotionNotConfigured
    from ..sources import UnsupportedURL

    # Same as ui_new_instance: a form submit, so errors re-render the dashboard
    # with a banner (in the Tasks section) rather than a raw JSON body, status
    # codes unchanged.
    try:
        request.app.state.scrape.start(
            [url.strip()], max_pages=max_pages, sync=sync
        )
    except UnsupportedURL as exc:
        return _render(request, Result("tasks", False, str(exc)), status=422)
    except NotionNotConfigured as exc:
        return _render(request, Result("tasks", False, str(exc)), status=409)
    return _sessions_redirect("tasks")


@router.post("/sessions/instances/{instance_id}/close")
async def ui_close_instance(request: Request, instance_id: str) -> Response:
    _require(request)
    _require_same_origin(request)
    await request.app.state.instances.stop(instance_id)
    return _sessions_redirect()


# ── live-pane tokens ──────────────────────────────────────────────────────────
#
# The dashboard renders live noVNC panes, but the page HTML carries no VNC token:
# a token baked into the markup lands in the DOM, the page's view-source, and
# whatever the browser caches of it. Instead the pane's JavaScript asks for one
# here, at connect time, over the cookie session — so the token is minted fresh
# per pane, lives ten minutes, and never sits in the page. Both endpoints mint a
# `vnc:<id>` token scoped to the one instance and bound to the subject the WS
# guard will check it against; the difference is only whether it also grants
# input.


def _vnc_subject_for(inst) -> str:
    """The subject a pane token must bear to pass the WS guard for this instance.

    Mirrors `routes/ws_guard.authorize`: a browser with no recorded subject (a
    sweep's own) falls back to the deployment's single owner, not to "anyone".
    Minting against a different subject than the guard checks would hand out
    tokens the guard then refuses — a silently broken live view.
    """
    from ..services.tokens import OWNER

    return getattr(inst, "subject", None) or OWNER


@router.get("/sessions/instances/{instance_id}/vnc-token")
async def ui_vnc_token(request: Request, instance_id: str) -> dict[str, str]:
    """A fresh, view-only VNC token for one pane. The default grant: watching."""
    _require(request)
    _require_same_origin(request)
    from ..services.tokens import VNC, issue

    inst = request.app.state.instances.get(instance_id)
    if inst is None or not getattr(inst, "vnc_port", None):
        raise HTTPException(status_code=404, detail="this browser has no live view")
    secret = request.app.state.secret.current()
    token = issue(instance_id, secret, kind=VNC, subject=_vnc_subject_for(inst))
    return {"token": token, "path": f"/instances/{instance_id}/vnc"}


@router.post("/sessions/instances/{instance_id}/control")
async def ui_take_control(request: Request, instance_id: str) -> dict[str, str]:
    """A control-grant VNC token — the explicit "Take control" switch.

    A control token lets its holder drive the browser (full keyboard and mouse)
    over the same socket a viewer uses. That is a deliberate escalation, so it is
    a POST behind both CSRF layers, never a default, and refused outright for a
    sweep's browser — that one is view-only however it is asked for, because it is
    mid-navigation and a click would corrupt the run.
    """
    _require(request)
    _require_same_origin(request)
    from ..services.tokens import VNC, issue

    inst = request.app.state.instances.get(instance_id)
    if inst is None or not getattr(inst, "vnc_port", None):
        raise HTTPException(status_code=404, detail="this browser has no live view")
    if inst.origin == "task":
        raise HTTPException(
            status_code=409,
            detail="a sweep's browser is view-only; it is mid-navigation on its own schedule",
        )
    secret = request.app.state.secret.current()
    token = issue(instance_id, secret, kind=VNC, subject=_vnc_subject_for(inst), control=True)
    return {"token": token, "path": f"/instances/{instance_id}/vnc"}


@router.post("/settings/cloakbrowser", response_class=HTMLResponse)
async def save_cloakbrowser(
    request: Request,
    action: str = Form("save"),
    cloakbrowser_license_key: str = Form(""),
    cloakbrowser_version: str = Form(""),
) -> Response:
    _require(request)
    _require_same_origin(request)
    store = request.app.state.settings
    current = store.load()

    if action == "public":
        # Secret inputs are intentionally write-only, so blank retains a saved
        # key on ordinary saves. This explicit action is the unambiguous way to
        # remove one and deliberately choose the public build.
        try:
            store.update(
                cloakbrowser_license_key="",
                cloakbrowser_version=cloakbrowser_version.strip(),
            )
        except ValueError as exc:
            return _render(
                request, Result("cloakbrowser", False, _first_error(exc)), status=400
            )
        request.app.state.instances.forget_binary()
        return _render(
            request,
            Result(
                "cloakbrowser",
                True,
                "Licence key cleared. Later launches will use the public build, which "
                "has fewer bypasses than Pro and has not been tested by us against the "
                "listing sites.",
                level="warn",
            ),
        )

    try:
        settings = store.update(
            cloakbrowser_license_key=_keep(cloakbrowser_license_key, current.cloakbrowser_license_key),
            cloakbrowser_version=cloakbrowser_version.strip(),
        )
    except ValueError as exc:
        # A malformed pin is caught by the store's validator before it can reach
        # a download URL, so the page just reports it.
        return _render(request, Result("cloakbrowser", False, _first_error(exc)), status=400)

    if action != "verify":
        return _render(request, Result("cloakbrowser", True, "Saved."))

    from ..services import license as license_service

    report = await license_service.verify(
        settings.cloakbrowser_license_key, settings.cloakbrowser_version
    )
    if report.ok and report.binary_path:
        request.app.state.instances.note_binary(report.binary_path, settings)
    elif not report.ok:
        request.app.state.instances.forget_binary()
    return _render(
        request,
        Result("cloakbrowser", report.ok, report.message, report),
        status=200 if report.ok else 400,
    )


@router.post("/settings/proxy", response_class=HTMLResponse)
async def save_proxy(
    request: Request,
    action: str = Form("save"),
    proxy_user: str = Form(""),
    proxy_password: str = Form(""),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_country: str = Form(""),
    proxy_region: str = Form(""),
) -> Response:
    _require(request)
    _require_same_origin(request)
    store = request.app.state.settings
    current = store.load()

    if action == "direct":
        # Password fields are intentionally never rendered back and blank means
        # "keep the saved secret", so an explicit action is the only unambiguous
        # way to disable a previously configured proxy. Clear the whole
        # connection atomically; leaving one stale field would create an
        # incomplete configuration that correctly fails rather than launching.
        store.update(
            proxy_user="", proxy_password="", proxy_host="", proxy_port="",
            proxy_last_check_at=0.0, proxy_last_check_ok=None,
            proxy_last_check_summary="",
        )
        return _render(
            request,
            Result(
                "proxy", True,
                "Direct connection selected. Browsers will use this server's "
                "datacenter internet address until you configure a proxy.",
            ),
        )

    changes = dict(
        proxy_user=proxy_user.strip(),
        proxy_password=_keep(proxy_password, current.proxy_password),
        proxy_host=proxy_host.strip(),
        proxy_port=proxy_port.strip(),
        proxy_country=proxy_country.strip() or current.proxy_country,
        proxy_region=proxy_region.strip() or current.proxy_region,
    )

    if action != "test":
        # Plain Save: persist as typed, and drop the previous verdict — it
        # described the values that were there before, and carrying a "working"
        # over an edited host would attribute a measurement to something we never
        # measured. Testing is the user's next step, by their own choice.
        settings = store.update(
            **changes,
            proxy_last_check_at=0.0, proxy_last_check_ok=None, proxy_last_check_summary="",
        )
        proxy_status = settings.proxy_status()
        if proxy_status == "direct":
            message = "Saved. Browsers will use this server's direct datacenter connection."
        elif proxy_status == "incomplete":
            message = (
                "Saved, but the proxy settings are incomplete. Complete all four connection "
                "fields or choose direct connection before launching a browser."
            )
        else:
            message = "Saved — but not tested. Use 'Save & test proxy' to check it can route."
        return _render(
            request,
            Result(
                "proxy", True, message,
                level="warn" if proxy_status in {"incomplete", "untested"} else "ok",
            ),
        )

    # action == "test": test the SUBMITTED values before writing anything.
    #
    # The old order wrote first and tested second, so a proxy that failed the
    # test was already saved — and if the user had a *working* proxy and mistyped
    # one field, clicking "Save & test" replaced it with the broken one on the
    # strength of that typo. So build a candidate that is NOT persisted, probe it,
    # and only write once it has proven it can route. A failed test leaves the
    # stored proxy, and its verdict, exactly as they were.
    candidate = Settings.model_validate({**current.model_dump(), **changes})
    if not candidate.proxy_configured():
        return _render(
            request,
            Result("proxy", False, "Fill in the username, password, host, and port first."),
            status=400,
        )

    from ..services.geo import ProxyUnreachable, probe
    from ..services.proxy import ProxyParts, build_proxy_url, new_session_token

    url = build_proxy_url(new_session_token(), ProxyParts.from_settings(candidate))
    try:
        measured = await probe(url)
    except ProxyUnreachable as exc:
        # A failed test must never downgrade a proxy that currently works: the
        # user may have a routing proxy and mistyped one field, and the old order
        # (write, then test) replaced the good config with the broken one on that
        # typo. So if a proven-working proxy is stored, keep it and say so.
        if current.proxy_last_check_ok:
            return _render(
                request,
                Result("proxy", False,
                       str(exc) + " Your previously working proxy was kept unchanged."),
                status=400,
            )
        # Nothing working is at stake, so record the attempt: persist what they
        # typed with a failed verdict, so a later visit shows "not working"
        # rather than a green light, and the values are there to fix.
        store.update(
            **changes,
            proxy_last_check_at=time.time(),
            proxy_last_check_ok=False,
            proxy_last_check_summary=_first_sentence(str(exc)),
        )
        return _render(request, Result("proxy", False, str(exc)), status=400)

    settings = store.update(
        **changes,
        proxy_last_check_at=time.time(),
        proxy_last_check_ok=True,
        proxy_last_check_summary=measured.describe(),
    )
    return _render(request, Result("proxy", True, measured.describe(), measured))


def _first_sentence(text: str) -> str:
    """Enough to recognise the failure on a later visit, without the full essay."""
    head = text.split(". ")[0].strip()
    return head if head.endswith(".") else head + "."


@router.post("/settings/pool", response_class=HTMLResponse)
async def save_pool(
    request: Request, max_instances: int = Form(4), interactive_reserve: int = Form(1)
) -> Response:
    _require(request)
    _require_same_origin(request)
    try:
        request.app.state.settings.update(
            max_instances=max_instances, interactive_reserve=interactive_reserve
        )
    except ValueError as exc:
        return _render(request, Result("pool", False, _first_error(exc)), status=400)
    return _render(request, Result("pool", True, "Saved."))


# ── Profiles: durable browser identities
#
# Every one is a cookie-authed state change, so — like the session and settings
# POSTs — it is POST-only and carries both CSRF layers. The operation itself is
# the same ProfileService used by REST and MCP; in particular, the in-use guard
# is not a UI-only scan that misses a browser while it is opening.


def _settings_redirect() -> Response:
    return RedirectResponse("/?view=settings", status_code=303)


def _profile_error_status(exc: Exception, *, missing: int = 404) -> int:
    from ..services.profiles import ProfileConflict, ProfileInUse, ProfileNotFound

    if isinstance(exc, ProfileNotFound):
        return missing
    if isinstance(exc, (ProfileInUse, ProfileConflict)):
        return 409
    return 400


@router.post("/settings/profiles/create")
async def profile_create(
    request: Request, name: str = Form(""), country: str = Form(""), region: str = Form("")
) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..services.profiles import ProfileError

    try:
        await request.app.state.profile_service.ensure_profile(
            name, country=country.strip() or None, region=region.strip() or None,
        )
    except ProfileError as exc:
        return _render(
            request, Result("profiles", False, str(exc)), status=_profile_error_status(exc)
        )
    return _settings_redirect()


@router.post("/settings/profiles/edit")
async def profile_edit(
    request: Request,
    name: str = Form(""),
    new_name: str | None = Form(None),
    country: str | None = Form(None),
    region: str | None = Form(None),
) -> Response:
    """Rename and relocate a profile in one submit — the dialog behind each row.

    The dialog sends only the fields it actually offered: Default's name box is
    disabled because Default cannot be renamed, and the geography boxes exist
    only when a proxy is configured. So an absent field means "leave this
    alone", which is deliberately distinct from an empty one — an empty region
    clears the region, and an emptied name is a mistake worth reporting rather
    than silently ignoring. A submit that changes nothing is a no-op redirect,
    not the service's "provide something to update" error: the user pressed
    Save on an unchanged form, which is not a failure.
    """
    _require(request)
    _require_same_origin(request)
    from ..services.profiles import ProfileError

    changes: dict[str, str] = {}
    if new_name is not None and new_name.strip() != name:
        changes["new_name"] = new_name.strip()
    if country is not None:
        changes["country"] = country.strip()
    if region is not None:
        changes["region"] = region.strip()
    if not changes:
        return _settings_redirect()

    try:
        await request.app.state.profile_service.update_profile(name, **changes)
    except ProfileError as exc:
        return _render(
            request, Result("profiles", False, str(exc)),
            status=_profile_error_status(exc, missing=400),
        )
    return _settings_redirect()


@router.post("/settings/profiles/rotate")
async def profile_rotate(request: Request, name: str = Form("")) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..services.profiles import ProfileError

    try:
        await request.app.state.profile_service.new_proxy_session(name)
    except ProfileError as exc:
        return _render(
            request, Result("profiles", False, str(exc)), status=_profile_error_status(exc)
        )
    return _settings_redirect()


@router.post("/settings/profiles/clear")
async def profile_clear(request: Request, name: str = Form("")) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..services.profiles import ProfileError

    # POST, never GET: this destroys cookies and logins, and the session cookie
    # is SameSite=lax — which a cross-site top-level GET would still carry.
    # Unlike delete, this is offered on Default too: it is the only way to reset
    # the one profile that cannot be deleted.
    try:
        await request.app.state.profile_service.clear_profile(name)
    except ProfileError as exc:
        return _render(
            request, Result("profiles", False, str(exc)), status=_profile_error_status(exc)
        )
    return _settings_redirect()


@router.get("/settings/profiles/sizes")
async def profile_sizes(request: Request, refresh: bool = False) -> dict[str, Any]:
    """How much disk each profile's saved data is using.

    Its own endpoint, fetched by the page after it paints, because measuring is
    a walk of thousands of small files and the settings page — which also holds
    the licence, proxy, and Notion configuration — must never wait on it. GET is
    right: it changes nothing. `refresh` re-walks instead of serving the cache.

    `age_sec` is computed here rather than left to the browser: "measured 4m
    ago" must not depend on the viewer's clock agreeing with the server's.
    """
    _require(request)
    try:
        sizes = await request.app.state.profile_service.sizes.snapshot(refresh=refresh)
    except OSError:
        # The walk already skips individual unreadable files; this is the volume
        # itself failing. A settings page with no sizes is fine — one that
        # cannot report the licence state because a directory listing failed is
        # not. Narrow on purpose: a bug here should still surface as a 500.
        logger.exception("could not measure profile sizes")
        sizes = []
    now = time.time()
    return {
        "profiles": [
            {
                "name": s.name, "bytes": s.bytes, "files": s.files,
                "measured_at": s.measured_at, "age_sec": max(0.0, now - s.measured_at),
            }
            for s in sizes
        ]
    }


@router.post("/settings/profiles/delete")
async def profile_delete(request: Request, name: str = Form("")) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..services.profiles import ProfileError, ProfileNotFound

    # The shared service refuses opening/open/closing profiles; the store also
    # keeps Default undeletable. The client confirm dialog is UX only.
    try:
        await request.app.state.profile_service.delete_profile(name)
    except ProfileNotFound:
        # The settings form's delete has historically been idempotent. Keep its
        # successful redirect while REST/MCP report a missing profile explicitly.
        return _settings_redirect()
    except ProfileError as exc:
        return _render(
            request, Result("profiles", False, str(exc)), status=_profile_error_status(exc)
        )
    return _settings_redirect()


# ── Disk space: what the volume is actually full of ─────────────────────────
#
# Two leaks, one section. The browser cache keeps one directory per CloakBrowser
# release (747 MB each, and the version is normally unpinned), and every task's
# screenshots and page copies stay forever. Both are measured the same way the
# profile sizes are — a walk, in a worker thread, fetched by the page after it
# paints — because the settings page also carries the licence, proxy, and Notion
# configuration and must render whatever the volume is doing.
#
# The numbers are shown next to the buttons that act on them, so nobody is asked
# to press "Remove" to find out what removing would free.


async def _builds_payload(request: Request, refresh: bool) -> dict[str, Any] | None:
    view = await request.app.state.browser_builds.snapshot(refresh=refresh)
    in_use = view.in_use
    return {
        "cache_dir": view.cache_dir,
        "in_use_known": view.in_use_known,
        "reason": view.reason,
        "in_use": (
            {"name": in_use.name, "version": in_use.version, "pro": in_use.pro,
             "bytes": in_use.bytes, "files": in_use.files}
            if in_use is not None else None
        ),
        "stale_count": len(view.stale),
        "stale_bytes": view.stale_bytes,
        "total_bytes": view.total_bytes,
        "builds": [
            {"name": b.name, "version": b.version, "pro": b.pro,
             "bytes": b.bytes, "files": b.files, "in_use": b.in_use}
            for b in view.builds
        ],
        "measured_at": view.measured_at,
    }


async def _history_payload(request: Request, refresh: bool) -> dict[str, Any] | None:
    view = await request.app.state.task_history.snapshot(refresh=refresh)
    return {
        "runs": view.runs, "bytes": view.bytes, "files": view.files,
        "orphans": view.orphans, "measured_at": view.measured_at,
    }


async def _uploads_payload(request: Request, refresh: bool) -> dict[str, Any] | None:
    view = await request.app.state.staged_uploads.snapshot(refresh=refresh)
    return {
        "handles": view.handles, "files": view.files, "bytes": view.bytes,
        "expired": view.expired, "measured_at": view.measured_at,
    }


@router.get("/settings/storage")
async def storage_sizes(request: Request, refresh: bool = False) -> dict[str, Any]:
    """What the browser cache, the task history, and staged uploads are using.

    One request for both, filled in after the page paints. Each half degrades on
    its own: a volume that cannot be walked answers `null` for that section and
    the page simply keeps its placeholder, because a settings page that cannot
    report the licence state because a directory listing failed is a far worse
    outcome than one that cannot report a size. Narrow on purpose — a bug in
    here should still surface as a 500.
    """
    _require(request)
    payload: dict[str, Any] = {}
    for key, build in (("builds", _builds_payload), ("history", _history_payload),
                       ("uploads", _uploads_payload)):
        try:
            payload[key] = await build(request, refresh)
        except OSError:
            logger.exception("could not measure %s", key)
            payload[key] = None
    return payload


@router.post("/settings/storage/builds/prune", response_class=HTMLResponse)
async def prune_builds(request: Request) -> Response:
    """Remove every cached browser build except the one in use.

    POST with both CSRF layers, like every other destructive action here: this
    is an rmtree of hundreds of megabytes, and the session cookie is SameSite=lax
    — which a cross-site top-level GET would still carry.
    """
    _require(request)
    _require_same_origin(request)
    from ..services.binaries import BuildsError

    try:
        removed = await request.app.state.browser_builds.remove_stale()
    except BuildsError as exc:
        # Both refusals — "we cannot tell which build is running" and "a browser
        # is open" — are states the user can resolve, so each says how.
        return _render(request, Result("storage", False, str(exc)), status=409)
    if not removed.removed:
        return _render(
            request,
            Result("storage", True,
                   "Nothing to remove — the only browser build on the volume is the one "
                   "this server is using."),
        )
    return _render(
        request,
        Result("storage", True,
               f"Removed {len(removed.removed)} older browser "
               f"version{'' if len(removed.removed) == 1 else 's'} and freed "
               f"{human_size(removed.bytes)}. The build in use ({removed.kept}) was kept."),
    )


@router.post("/settings/storage/history/clear", response_class=HTMLResponse)
async def clear_history(request: Request) -> Response:
    """Remove every finished run and the evidence it captured.

    A run that is still working keeps both — the service re-reads each job's
    status as it removes it, so starting a sweep and clearing history at the
    same time cannot delete the sweep out from under itself.
    """
    _require(request)
    _require_same_origin(request)

    cleared = await request.app.state.task_history.clear()
    if not cleared.runs and not cleared.orphans:
        return _render(
            request,
            Result("storage", True, "Nothing to clear — there is no saved task history."),
        )
    parts = [f"Cleared {cleared.runs} run{'' if cleared.runs == 1 else 's'}"]
    if cleared.orphans:
        parts.append(
            f"and {cleared.orphans} leftover evidence "
            f"folder{'' if cleared.orphans == 1 else 's'} from runs already gone"
        )
    message = " ".join(parts) + f", freeing {human_size(cleared.bytes)}."
    if cleared.kept:
        message += (
            f" {cleared.kept} run{'' if cleared.kept == 1 else 's'} still working "
            f"{'was' if cleared.kept == 1 else 'were'} kept."
        )
    return _render(request, Result("storage", True, message))


@router.post("/settings/storage/uploads/clear", response_class=HTMLResponse)
async def clear_uploads(request: Request, scope: str = Form("expired")) -> Response:
    """Remove staged uploads. Expired ones by default; everything on request.

    Two scopes rather than one button, because a live ticket may be mid-flight —
    the same reason `clear_history` keeps a run that is still working. The
    default is the safe one; the full clear is the human's escape hatch for a
    volume filling up and is behind its own confirmation. Neither will touch a
    ticket with a write streaming into it: that is the service's own guard,
    re-read at the moment of removal rather than from the listing this started
    with.

    The banner reports what was actually freed, never what it set out to free.
    """
    _require(request)
    _require_same_origin(request)

    cleared = await request.app.state.uploads.clear(expired_only=(scope != "all"))
    request.app.state.staged_uploads.invalidate()

    if not cleared.handles:
        message = (
            "Nothing to clear — there are no uploaded files."
            if not cleared.kept
            else f"Nothing to clear — {_tickets(cleared.kept)} still in use, and "
                 "uploaded files expire on their own within two hours."
        )
        return _render(request, Result("storage", True, message))

    message = f"Cleared {_tickets(cleared.handles)} and freed {human_size(cleared.bytes)}."
    if cleared.kept:
        message += f" {_tickets(cleared.kept).capitalize()} still in use {'was' if cleared.kept == 1 else 'were'} kept."
    if cleared.refused:
        message += (
            f" {cleared.refused} item{'' if cleared.refused == 1 else 's'} could not be "
            "removed safely and {} left alone.".format(
                "was" if cleared.refused == 1 else "were")
        )
    return _render(request, Result("storage", True, message))


def _tickets(count: int) -> str:
    return f"{count} upload{'' if count == 1 else 's'}"


# ── Notion ──────────────────────────────────────────────────────────────────


@router.post("/settings/notion", response_class=HTMLResponse)
async def save_notion(
    request: Request, action: str = Form("save"), notion_api_token: str = Form("")
) -> Response:
    _require(request)
    _require_same_origin(request)
    store = request.app.state.settings
    current = store.load()
    settings = store.update(
        notion_api_token=_keep(notion_api_token, current.notion_api_token)
    )

    if action == "save":
        return _render(request, Result("notion", True, "Saved."))

    from ..stores.notion import NotionError, NotionStore

    try:
        notion = NotionStore(settings.notion_api_token)
        if action == "list":
            databases = await notion.list_databases()
            pages = await notion.list_parent_pages()
            if not databases and not pages:
                return _render(
                    request,
                    Result(
                        "notion",
                        False,
                        "The token works, but nothing has been shared with this "
                        "integration yet. In Notion, open the database or page you want "
                        "to use, click the ••• menu, and share it with your integration.",
                    ),
                )
            return _render(
                request,
                Result(
                    "notion",
                    True,
                    f"Found {len(databases)} database(s) and {len(pages)} page(s) shared "
                    f"with '{await notion.whoami()}'.",
                    {"databases": databases, "pages": pages},
                ),
            )
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    return _render(request, Result("notion", False, f"Unknown action '{action}'."), status=400)


async def _notion_mapping_view(store, db_id: str, column_map) -> dict[str, Any]:
    """The mapping table's view model: one row per field plus the database's
    columns for the dropdowns. Fetches the live column list so the picker only
    ever offers columns that actually exist."""
    from ..stores.notion import _display, build_map_rows

    columns = await store.column_types(db_id)  # name -> notion type
    return {
        "db_id": db_id,
        "rows": build_map_rows(column_map or None, columns),
        "columns": [{"name": n, "type": _display(t) or t} for n, t in sorted(columns.items())],
    }


@router.post("/settings/notion/select", response_class=HTMLResponse)
async def select_database(request: Request, db_id: str = Form("")) -> Response:
    """Adopt an existing database, default its column mapping by identity, and say
    exactly what is wrong with it.

    Selecting is deliberately separate from verifying nothing: we store the id
    (and a fresh default map) even when the schema has gaps, so the user can fix
    the mapping below — or the columns in Notion — and re-verify rather than lose
    their selection.
    """
    _require(request)
    _require_same_origin(request)
    from ..stores.notion import NotionError, NotionStore, default_column_map

    db_id = db_id.strip()
    if not db_id:
        return _render(request, Result("notion", False, "Pick a database first."), status=400)

    settings = request.app.state.settings.load()
    store = NotionStore(settings.notion_api_token)
    try:
        columns = await store.column_types(db_id)
        column_map = default_column_map(set(columns))
        report = await store.verify_schema(db_id, column_map)
        mapping = await _notion_mapping_view(store, db_id, column_map)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    request.app.state.settings.update(notion_db_id=db_id, notion_column_map=column_map)
    return _render(
        request,
        Result("notion", report.usable, _schema_message(report), report, level=_schema_level(report)),
        notion_mapping=mapping,
    )


@router.post("/settings/notion/verify", response_class=HTMLResponse)
async def verify_database(request: Request) -> Response:
    _require(request)
    _require_same_origin(request)
    from ..stores.notion import NotionError, NotionStore

    settings = request.app.state.settings.load()
    if not settings.notion_db_id:
        return _render(request, Result("notion", False, "No database selected yet."), status=400)
    column_map = settings.notion_column_map or None
    store = NotionStore(settings.notion_api_token)
    try:
        report = await store.verify_schema(settings.notion_db_id, column_map)
        mapping = await _notion_mapping_view(store, settings.notion_db_id, column_map)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)
    return _render(
        request,
        Result("notion", report.usable, _schema_message(report), report, level=_schema_level(report)),
        notion_mapping=mapping,
    )


@router.post("/settings/notion/mapping", response_class=HTMLResponse)
async def save_mapping(request: Request) -> Response:
    """Save the user's column mapping, then re-verify against it.

    One <select> per field, named `map_<field-key>`; its value is the chosen
    column name, or empty for "don't sync" (optional fields) / unset (required).
    A submitted name is only kept if the database really has that column, so a
    stale form can never make us believe in a column that is not there.
    """
    _require(request)
    _require_same_origin(request)
    from ..stores.notion import KNOWN_PROPS, NotionError, NotionStore

    settings = request.app.state.settings.load()
    if not settings.notion_db_id:
        return _render(request, Result("notion", False, "No database selected yet."), status=400)

    form = await request.form()
    store = NotionStore(settings.notion_api_token)
    try:
        columns = await store.column_types(settings.notion_db_id)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    new_map: dict[str, str | None] = {}
    for prop in KNOWN_PROPS:
        chosen = str(form.get(f"map_{prop.key}", "")).strip()
        if chosen and chosen in columns:
            new_map[prop.key] = chosen
        elif prop.required:
            continue  # unmapped — the user must still choose a column
        else:
            new_map[prop.key] = None  # "don't sync"

    request.app.state.settings.update(notion_column_map=new_map)
    try:
        report = await store.verify_schema(settings.notion_db_id, new_map)
        mapping = await _notion_mapping_view(store, settings.notion_db_id, new_map)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)
    return _render(
        request,
        Result("notion", report.usable, _schema_message(report), report, level=_schema_level(report)),
        notion_mapping=mapping,
    )


def _schema_level(report) -> str:
    if report.complete:
        return "ok"
    return "warn" if report.usable else "bad"


def _schema_message(report) -> str:
    """Lead with what it means for them, not with a count of type mismatches.

    The two failure grades are genuinely different and must not read the same:
    a missing required column means nothing will sync until they fix it, while a
    text 'Asking Price' means everything syncs except the prices. Collapsing
    both into "schema problems" would send someone chasing a blocker they do not
    have, or ignoring one they do.
    """
    if report.complete:
        return f"'{report.title}' is ready — new listings will sync into your columns."
    if report.usable:
        n = len(report.missing_recommended) + len(report.mismatched_recommended)
        s = "s" if n != 1 else ""
        return (
            f"'{report.title}' will sync. {n} column{s} won't be filled in — "
            f"the rest saves normally."
        )
    return f"'{report.title}' can't sync yet — add the columns below first."


@router.post("/settings/notion/create", response_class=HTMLResponse)
async def create_database(
    request: Request, parent_page_id: str = Form(""), title: str = Form("Business Listings")
) -> Response:
    """Create a database — only ever from this explicit click (decision #5)."""
    _require(request)
    _require_same_origin(request)
    from ..stores.notion import NotionError, NotionStore

    parent_page_id = parent_page_id.strip()
    if not parent_page_id:
        return _render(
            request, Result("notion", False, "Pick a page to create the database under."),
            status=400,
        )

    settings = request.app.state.settings.load()
    from ..stores.notion import default_column_map

    try:
        notion = NotionStore(settings.notion_api_token)
        created = await notion.create_database(parent_page_id, title.strip() or "Business Listings")
        # A database we just created has every default column, so its map is a
        # clean identity map. Default it from the live columns anyway, so what we
        # store is what Notion actually made rather than what we asked for.
        columns = await notion.column_types(created.id)
        column_map = default_column_map(set(columns))
        report = await notion.verify_schema(created.id, column_map)
        mapping = await _notion_mapping_view(notion, created.id, column_map)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    request.app.state.settings.update(notion_db_id=created.id, notion_column_map=column_map)
    # Say what the verification found, not what we expect it to find. We create
    # the schema from the same table we check it against, so "complete" should be
    # certain — but asserting it in prose rather than reading the report is how a
    # claim outlives the thing it was based on.
    message = f"Created '{created.title}' and selected it. " + (
        "It has every column this app writes."
        if report.complete
        else "Notion did not create it quite as expected — see below."
    )
    return _render(request, Result("notion", report.complete, message, report),
                   notion_mapping=mapping)


# ── Connected apps ────────────────────────────────────────────────────────────
#
# The OAuth clients (ChatGPT, Claude, Claude Code) that registered themselves via
# DCR and can drive this server with a bearer token. This is the cookie-authed
# owner's view of them, with a Disconnect that removes the registration.
#
# UI-only on purpose: this list and its delete are never exposed over MCP or the
# bearer-auth /api surface. One connected app must not be able to enumerate or
# disconnect another — the only principal allowed to manage the roster is the
# person holding the session cookie, here.


@router.post("/settings/connections/disconnect", response_class=HTMLResponse)
async def disconnect_app(request: Request, client_id: str = Form("")) -> Response:
    """Remove one OAuth client's registration.

    Same shape as every other settings mutation: POST-only, session cookie
    (`_require`) plus same-origin (`_require_same_origin`). Deleting the
    registration is what revokes: the client can no longer exchange a code or
    refresh, so it loses access within the access-token TTL (≤1h) and must
    re-authorize. Idempotent — disconnecting an already-gone client still lands
    on a clean re-render, never an error.
    """
    _require(request)
    _require_same_origin(request)
    client_id = client_id.strip()
    if not client_id:
        return _render(
            request, Result("connections", False, "No app was specified."), status=400
        )
    request.app.state.oauth.delete_client(client_id)
    return _render(
        request,
        Result("connections", True, "That app has been disconnected. It loses access within an hour."),
    )


# APP_SECRET is managed in Railway's Variables tab, not in this settings UI.
# SecretService reads that environment value directly; changing it means editing
# the variable and redeploying, and recovery means reading the value there.
