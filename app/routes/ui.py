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

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..services import sessions
from ..services.secret import WeakSecret
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


def _render(request: Request, result: Result | None = None, status: int = 200) -> Response:
    settings: Settings = request.app.state.settings.load()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "s": settings,
            "result": result,
            "pool_warning": settings.pool_warning(),
            "has_license": bool(settings.cloakbrowser_license_key),
            "has_proxy_password": bool(settings.proxy_password),
            "has_notion_token": bool(settings.notion_api_token),
            "proxy_checked_at": _when(settings.proxy_last_check_at),
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


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    if _authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "unconfigured": request.app.state.secret.current() is None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, secret: str = Form("")) -> Response:
    secret_service = request.app.state.secret
    if secret_service.current() is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "unconfigured": True}, status_code=503
        )
    if not secret_service.verify(secret):
        logger.warning("failed login attempt from %s", request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That is not the right secret.", "unconfigured": False},
            status_code=401,
        )

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
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response


# ── the settings page ───────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    _require(request)
    return _render(request)


@router.post("/settings/cloakbrowser", response_class=HTMLResponse)
async def save_cloakbrowser(
    request: Request,
    action: str = Form("save"),
    cloakbrowser_license_key: str = Form(""),
    cloakbrowser_version: str = Form(""),
) -> Response:
    _require(request)
    store = request.app.state.settings
    current = store.load()

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
    store = request.app.state.settings
    current = store.load()
    settings = store.update(
        proxy_user=proxy_user.strip(),
        proxy_password=_keep(proxy_password, current.proxy_password),
        proxy_host=proxy_host.strip(),
        proxy_port=proxy_port.strip(),
        proxy_country=proxy_country.strip() or current.proxy_country,
        proxy_region=proxy_region.strip() or current.proxy_region,
        # Saving invalidates the previous verdict, which described the values
        # that were there before. Carrying a "working" over an edited host would
        # be a measurement attributed to something we never measured.
        proxy_last_check_at=0.0,
        proxy_last_check_ok=None,
        proxy_last_check_summary="",
    )

    if action != "test":
        return _render(
            request,
            Result(
                "proxy", True,
                "Saved — but not tested. Use 'Save & test proxy' to check it can actually route.",
                level="warn" if settings.proxy_configured() else "ok",
            ),
        )

    if not settings.proxy_configured():
        return _render(
            request,
            Result("proxy", False, "Fill in the username, password, host, and port first."),
            status=400,
        )

    from ..services.geo import ProxyUnreachable, probe
    from ..services.proxy import ProxyParts, build_proxy_url, new_session_token

    url = build_proxy_url(new_session_token(), ProxyParts.from_settings(settings))
    try:
        measured = await probe(url)
    except ProxyUnreachable as exc:
        store.update(
            proxy_last_check_at=time.time(),
            proxy_last_check_ok=False,
            proxy_last_check_summary=_first_sentence(str(exc)),
        )
        return _render(request, Result("proxy", False, str(exc)), status=400)

    store.update(
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
    try:
        request.app.state.settings.update(
            max_instances=max_instances, interactive_reserve=interactive_reserve
        )
    except ValueError as exc:
        return _render(request, Result("pool", False, _first_error(exc)), status=400)
    return _render(request, Result("pool", True, "Saved."))


# ── Notion ──────────────────────────────────────────────────────────────────


@router.post("/settings/notion", response_class=HTMLResponse)
async def save_notion(
    request: Request, action: str = Form("save"), notion_api_token: str = Form("")
) -> Response:
    _require(request)
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


@router.post("/settings/notion/select", response_class=HTMLResponse)
async def select_database(request: Request, db_id: str = Form("")) -> Response:
    """Adopt an existing database, and say exactly what is wrong with it.

    Selecting is deliberately separate from verifying nothing: we store the id
    even when the schema has gaps, so the user can go fix them in Notion and
    press Verify again rather than lose their selection.
    """
    _require(request)
    from ..stores.notion import NotionError, NotionStore

    db_id = db_id.strip()
    if not db_id:
        return _render(request, Result("notion", False, "Pick a database first."), status=400)

    settings = request.app.state.settings.load()
    try:
        report = await NotionStore(settings.notion_api_token).verify_schema(db_id)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    request.app.state.settings.update(notion_db_id=db_id)
    return _render(request, Result("notion", report.usable, _schema_message(report), report,
                                   level=_schema_level(report)))


@router.post("/settings/notion/verify", response_class=HTMLResponse)
async def verify_database(request: Request) -> Response:
    _require(request)
    from ..stores.notion import NotionError, NotionStore

    settings = request.app.state.settings.load()
    if not settings.notion_db_id:
        return _render(request, Result("notion", False, "No database selected yet."), status=400)
    try:
        report = await NotionStore(settings.notion_api_token).verify_schema(settings.notion_db_id)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)
    return _render(request, Result("notion", report.usable, _schema_message(report), report,
                                   level=_schema_level(report)))


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
        return f"'{report.title}' has everything this app needs."
    if report.usable:
        skipped = ", ".join(
            i.name for i in [*report.missing_recommended, *report.mismatched_recommended]
        )
        return (
            f"'{report.title}' will sync, but these columns will be left empty: "
            f"{skipped}. Everything else saves normally — here is what each one costs "
            f"you and how to fix it."
        )
    return (
        f"'{report.title}' cannot sync yet — this app needs these columns before it can "
        f"save anything into it."
    )


@router.post("/settings/notion/create", response_class=HTMLResponse)
async def create_database(
    request: Request, parent_page_id: str = Form(""), title: str = Form("Business Listings")
) -> Response:
    """Create a database — only ever from this explicit click (decision #5)."""
    _require(request)
    from ..stores.notion import NotionError, NotionStore

    parent_page_id = parent_page_id.strip()
    if not parent_page_id:
        return _render(
            request, Result("notion", False, "Pick a page to create the database under."),
            status=400,
        )

    settings = request.app.state.settings.load()
    try:
        notion = NotionStore(settings.notion_api_token)
        created = await notion.create_database(parent_page_id, title.strip() or "Business Listings")
        report = await notion.verify_schema(created.id)
    except NotionError as exc:
        return _render(request, Result("notion", False, str(exc)), status=400)

    request.app.state.settings.update(notion_db_id=created.id)
    # Say what the verification found, not what we expect it to find. We create
    # the schema from the same table we check it against, so "complete" should be
    # certain — but asserting it in prose rather than reading the report is how a
    # claim outlives the thing it was based on.
    message = f"Created '{created.title}' and selected it. " + (
        "It has every column this app writes."
        if report.complete
        else "Notion did not create it quite as expected — see below."
    )
    return _render(request, Result("notion", report.complete, message, report))


# ── security ────────────────────────────────────────────────────────────────


@router.post("/settings/secret", response_class=HTMLResponse)
async def rotate_secret(
    request: Request, current_secret: str = Form(""), new_secret: str = Form("")
) -> Response:
    """Rotate APP_SECRET.

    Requires the current secret as well as the new one. The session already
    proves someone knew it, but a session is a cookie and this is the one
    credential that protects everything else — re-proving it costs a paste and
    removes a hijacked-cookie lockout from the table.
    """
    _require(request)
    secret_service = request.app.state.secret

    if not secret_service.verify(current_secret):
        return _render(
            request, Result("secret", False, "The current secret is wrong."), status=401
        )
    try:
        rotated = secret_service.rotate(new_secret)
    except WeakSecret as exc:
        return _render(request, Result("secret", False, str(exc)), status=400)

    # Every cookie signed with the old secret is now void, including this one.
    # Re-issue immediately so rotating does not log the user out of the page
    # they are looking at.
    response = _render(
        request,
        Result(
            "secret",
            True,
            "Secret rotated. Every other session is now signed out. Update APP_SECRET in "
            "Railway's Variables tab too, so a future redeploy does not confuse you — "
            "though the stored secret stays authoritative either way.",
        ),
    )
    _set_session(response, sessions.issue(rotated))
    return response
