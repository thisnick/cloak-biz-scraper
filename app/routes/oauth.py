"""The OAuth 2.1 doors: discovery, registration, authorize, token.

Most of this is the MCP SDK's own handlers, mounted onto our app. Two things
are ours, and both are here because the SDK could not do them for us:

**1. The metadata is built per request, not once at startup.**
`create_auth_routes()` wants an `issuer_url` at construction time and bakes it
into a frozen document. We do not have one: Railway assigns the deployment's
domain and never tells the app what it is, and the whole product promise is that
the user sets exactly one variable. Asking them for their own URL to fill this
in would be the second variable. So the issuer is read from the request that
arrives — which is also the only value that can be right for a deployment
reachable by more than one name.

Handing the `Host` header back inside a signed-nothing JSON document sounds
worse than it is: an attacker who sends `Host: evil.example` gets a document
naming evil.example, delivered to themselves. There is no victim in that flow —
they had to be talking to us directly to get it. It would matter behind a shared
cache, and there is none.

**2. `/authorize` collects APP_SECRET.**
The SDK validates the client, the redirect_uri and the PKCE challenge, then asks
the provider where to send the browser. We send it to our own login form, which
is the entire "authorization" step: proving APP_SECRET *is* being the owner.

**Discovery is served at both the bare and the /mcp-suffixed path.** RFC 9728
§3.1 says the metadata for resource `https://host/mcp` lives at
`https://host/.well-known/oauth-protected-resource/mcp`, but clients in the wild
ask for the bare path too, and a 404 on discovery is an unconnectable server.
Serving both costs one route.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.settings import ClientRegistrationOptions

from ..services import oauth as oauth_service
from ..services.oauth import PendingInvalid
from ..services.ratelimit import client_key
from ..services.urls import public_base

logger = logging.getLogger("cloakbiz.oauth.routes")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Never expire a registered client's secret. A connector that silently stops
# working weeks later, for a reason the user cannot see or fix from the UI, is a
# worse outcome than a long-lived registration on a single-user server.
REGISTRATION_OPTIONS = ClientRegistrationOptions(
    enabled=True,  # without DCR, ChatGPT and Claude simply cannot connect
    client_secret_expiry_seconds=None,
    valid_scopes=oauth_service.SCOPES,
    default_scopes=oauth_service.SCOPES,
)

RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"


def _provider(request: Request):
    return request.app.state.oauth


# ── discovery ───────────────────────────────────────────────────────────────


def _as_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": oauth_service.SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Both, because DCR hands some clients a secret and others none: a public
        # client authenticates with no secret at all, and the SDK's registration
        # handler decides which by the client's own token_endpoint_auth_method.
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "client_secret_basic", "none",
        ],
        # S256 only. "plain" is a PKCE challenge that is not a challenge, and
        # OAuth 2.1 removes it; the SDK refuses it at /authorize regardless.
        "code_challenge_methods_supported": ["S256"],
    }


def _resource_metadata(base: str) -> dict:
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": oauth_service.SCOPES,
        "resource_name": "cloak-biz-scraper",
        "bearer_methods_supported": ["header"],
    }


def _cors(payload: dict) -> JSONResponse:
    """Discovery is fetched cross-origin by browser-based clients (the MCP
    Inspector is one), and it is public by definition — it names endpoints, not
    secrets. Everything that matters is still behind the token."""
    return JSONResponse(payload, headers={"Access-Control-Allow-Origin": "*"})


# The same reasoning the SDK's own create_auth_routes applies, and the reason we
# have to apply it ourselves: these handlers are mounted directly rather than
# through that helper, so its CORS wrapper is not in the path.
#
# `/authorize` is deliberately NOT in this list. It is a page a browser is
# redirected to, not something a script fetches, and it is where APP_SECRET gets
# typed — there is nothing a cross-origin reader of it could want that is not
# the thing we are protecting.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, mcp-protocol-version",
    "Access-Control-Max-Age": "86400",
}


def _allow_cross_origin(response: Response) -> Response:
    for name, value in _CORS_HEADERS.items():
        response.headers[name] = value
    return response


@router.options("/register")
@router.options("/token")
async def oauth_preflight() -> Response:
    """A browser will not POST cross-origin without asking first.

    Registration and the token exchange are both reachable from a page (the MCP
    Inspector runs in one), and both are safe to be: neither hands anything to a
    caller who was not already holding the code, the verifier, and the client's
    own credentials.
    """
    return _allow_cross_origin(Response(status_code=204))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> Response:
    return _cors(_as_metadata(public_base(request)))


@router.get(RESOURCE_METADATA_PATH)
@router.get(f"{RESOURCE_METADATA_PATH}/mcp")
async def protected_resource_metadata(request: Request) -> Response:
    return _cors(_resource_metadata(public_base(request)))


# ── the SDK's endpoints, wired to our provider ──────────────────────────────


@router.post("/register")
async def register(request: Request) -> Response:
    """Dynamic Client Registration — open, because it has to be.

    ChatGPT and Claude register themselves; requiring a credential here would
    mean they could never connect, which is the whole point of DCR.

    **Open is not the same as unlimited, and this endpoint writes to disk.** Every
    registration re-encrypts and rewrites the whole client store, so an
    unthrottled flood is not just rows in a file — it is O(n) disk work per
    request against a file the flood itself is growing, on a volume the user
    pays for. Throttled, it is a trickle.

    Registrations are counted whether or not they succeed. Unlike the login,
    there is no "wrong" registration to single out: the flood is made of
    perfectly valid ones.
    """
    limiter = request.app.state.register_limiter
    key = client_key(request)
    wait = limiter.retry_after(key)
    if wait:
        logger.warning("throttled /register from %s for %.1fs", key, wait)
        return JSONResponse(
            {"error": "temporarily_unavailable",
             "error_description": "Too many registrations. Try again shortly."},
            status_code=429,
            headers={"Retry-After": str(int(wait) + 1)},
        )
    limiter.record(key)
    return _allow_cross_origin(
        await RegistrationHandler(_provider(request), options=REGISTRATION_OPTIONS).handle(request)
    )


@router.api_route("/authorize", methods=["GET", "POST"])
async def authorize(request: Request) -> Response:
    return await AuthorizationHandler(_provider(request)).handle(request)


@router.post("/token")
async def token(request: Request) -> Response:
    provider = _provider(request)
    return _allow_cross_origin(
        await TokenHandler(provider, ClientAuthenticator(provider)).handle(request)
    )


# ── the login step (proving APP_SECRET) ─────────────────────────────────────


def _login_page(request: Request, blob: str, error: str | None = None,
                status: int = 200) -> Response:
    return templates.TemplateResponse(
        request,
        "authorize.html",
        {
            "p": blob,
            "error": error,
            "unconfigured": request.app.state.secret.current() is None,
        },
        status_code=status,
    )


@router.get("/authorize/login", response_class=HTMLResponse)
async def authorize_login_form(request: Request, p: str = "") -> Response:
    try:
        _provider(request).read_pending(p)
    except PendingInvalid as exc:
        return _expired(request, exc)
    return _login_page(request, p)


@router.post("/authorize/login", response_class=HTMLResponse)
async def authorize_login(request: Request, p: str = Form(""), secret: str = Form("")) -> Response:
    """Prove APP_SECRET, and get a code delivered to the client that asked.

    The pending blob is re-verified here rather than trusted from the form: this
    is a POST from a page we rendered, but nothing stops someone POSTing their
    own, and the redirect_uri inside is where the authorization code goes.
    """
    provider = _provider(request)
    try:
        pending = provider.read_pending(p)
    except PendingInvalid as exc:
        return _expired(request, exc)

    limiter = request.app.state.login_limiter
    key = client_key(request)
    wait = limiter.retry_after(key)
    if wait:
        logger.warning("throttled /authorize login from %s for %.1fs", key, wait)
        return _throttled(request, p, wait)

    secret_service = request.app.state.secret
    if secret_service.current() is None:
        return _login_page(request, p, status=503)
    if not secret_service.verify(secret):
        limiter.fail(key)
        logger.warning("failed /authorize login from %s", key)
        return _login_page(request, p, "That is not the right secret.", status=401)

    limiter.reset(key)
    destination = provider.complete(pending)
    logger.info("authorized client %s", pending["cid"])
    # 303: the browser must re-issue as GET. A 307 would replay the POST — with
    # the secret in its body — against the client's redirect_uri, handing the
    # deployment's one credential to a third party.
    return RedirectResponse(destination, status_code=303)


def _expired(request: Request, exc: PendingInvalid) -> Response:
    return templates.TemplateResponse(
        request, "authorize.html", {"p": "", "error": str(exc), "dead": True}, status_code=400
    )


def _throttled(request: Request, blob: str, wait: float) -> Response:
    response = _login_page(
        request, blob,
        f"Too many wrong attempts. Wait {int(wait) + 1} seconds and try again.",
        status=429,
    )
    response.headers["Retry-After"] = str(int(wait) + 1)
    return response
