"""Self-contained OAuth 2.1 — this server is its own Authorization Server.

There is no third-party IdP because there is no third party: one person deploys
this, and the thing they already hold is APP_SECRET. Signing up for Auth0 to
protect your own browser would be a worse product and more secrets to lose. So
`/authorize` asks for APP_SECRET, and proving it is what authorizes a client.

**What state must survive, and why that decides the design.** Railway scales this
to zero after ~6 minutes of no outbound traffic. Anything held only in memory is
therefore gone between two tool calls of the same conversation:

* **Registered clients live on the volume.** ChatGPT and Claude register once via
  DCR and expect that client_id to keep working. An in-memory registry would
  de-register every client on the first nap, and the failure would look like
  "the connector randomly logs itself out" — the worst kind of bug to diagnose,
  because it needs a 6-minute idle to reproduce and never happens while you are
  watching.
* **Access and refresh tokens are stateless**, signed with APP_SECRET
  (services/signing.py). Nothing to persist, nothing to garbage-collect, and
  they survive sleep for free. Their revocation story is APP_SECRET rotation,
  which kills every token at once — appropriate when there is one user.
* **Authorization codes live on the volume**, because single-use is a
  requirement (OAuth 2.1 / RFC 6749 §10.5) and single-use is the one property a
  stateless token cannot have: you cannot know it was already spent without
  writing down that it was. They are keyed by **hash**, never stored in the
  clear — the store is encrypted at rest anyway, but a live credential should
  not be recoverable from a backup even so.

**Why no revocation endpoint.** Access tokens are stateless, so `/revoke` could
not honour a revocation of one; advertising an endpoint that silently does
nothing is worse than not advertising it. Rotating APP_SECRET in the settings UI
revokes everything, and that is what the docs point at.

**Why no scope theatre.** There is exactly one scope, `mcp`, and every token
carries it. A single-user deployment has no partial privileges to express — a
token either drives your browser or it does not — so a scope check here would
always pass, and a check that always passes is a comment pretending to be code.
The one scope exists because DCR clients expect to negotiate one.

**Audience/resource binding (RFC 8707) is recorded, not enforced, and that is
deliberate.** Resource indicators exist so a token minted for server A cannot be
replayed against server B by a malicious A. That attack needs a shared
Authorization Server issuing for several Resource Servers. We are our own AS and
our own RS, and tokens are HMAC'd with this deployment's APP_SECRET — a token
from another deployment fails the signature check before anything reads its
audience. So the `resource` value is carried through and logged for audit, while
the property it would buy is already structural. Enforcing a string comparison
against a Host-derived URL would add a way for a trailing slash to break a real
client, in exchange for nothing.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import signing
from .crypto import Cipher
# The one resource owner: whoever can prove APP_SECRET. Imported rather than
# re-declared so the CDP token and the access token cannot drift apart on who
# "owner" is — that agreement is what the subject check rests on.
from .tokens import OWNER

logger = logging.getLogger("cloakbiz.oauth")

SCOPE = "mcp"
SCOPES = [SCOPE]

# An hour is short enough that a token scraped from a log has a bounded life, and
# long enough that a working conversation never stalls mid-sweep to refresh.
ACCESS_TTL_SEC = 60 * 60
# Thirty days: the connector should still work when its owner comes back from
# holiday, and re-authorising means finding APP_SECRET again.
REFRESH_TTL_SEC = 30 * 24 * 3600
# The code is exchanged by a machine within a second of being issued. Two minutes
# is clock skew, not user think-time — the thinking already happened at the login
# form, which is upstream of this.
CODE_TTL_SEC = 120
# The login form's own lifetime: issued at /authorize, spent at /authorize/login.
# This one IS user think-time (finding the secret in Railway's Variables tab).
PENDING_TTL_SEC = 15 * 60

_AUD_ACCESS = "oauth:access"
_AUD_REFRESH = "oauth:refresh"
_AUD_PENDING = "oauth:pending"


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class PendingInvalid(ValueError):
    """The login blob is forged, expired, or was minted for another secret."""


class OAuthStore:
    """Registered clients and live authorization codes, encrypted on the volume.

    One file, one lock, read-through cached — the same shape as SettingsService,
    because this has the same access pattern (tiny, read often, written rarely)
    and inventing a second persistence style for it would be a second thing to
    get wrong.
    """

    def __init__(self, path: Path, dek_path: Path) -> None:
        self._path = path
        self._cipher = Cipher.from_volume(dek_path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self._path.exists():
            self._data = {"clients": {}, "codes": {}}
            return self._data
        self._data = json.loads(self._cipher.decrypt(self._path.read_bytes()))
        self._data.setdefault("clients", {})
        self._data.setdefault("codes", {})
        return self._data

    def _flush(self) -> None:
        data = self._data or {"clients": {}, "codes": {}}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._cipher.encrypt(json.dumps(data).encode())
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_bytes(blob)
        tmp.chmod(0o600)
        tmp.replace(self._path)

    # ── clients ─────────────────────────────────────────────────────────────
    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            raw = self._load()["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self._lock:
            self._load()["clients"][client.client_id] = json.loads(client.model_dump_json())
            self._flush()
        logger.info("registered oauth client %s (%s)", client.client_id, client.client_name or "unnamed")

    def client_count(self) -> int:
        with self._lock:
            return len(self._load()["clients"])

    # ── authorization codes ─────────────────────────────────────────────────
    def put_code(self, code: str, record: dict) -> None:
        with self._lock:
            codes = self._load()["codes"]
            self._expire(codes)
            codes[_hash(code)] = record
            self._flush()

    def peek_code(self, code: str) -> dict | None:
        """Read without consuming — for the PKCE check that happens first."""
        with self._lock:
            record = self._load()["codes"].get(_hash(code))
        if record is None or record["expires_at"] < time.time():
            return None
        return record

    def take_code(self, code: str) -> dict | None:
        """Read and destroy. Single-use is enforced *here*, by the pop.

        Doing it anywhere else would leave a window where two concurrent
        exchanges both see a live code — the exact race the single-use rule
        exists to close.
        """
        with self._lock:
            codes = self._load()["codes"]
            self._expire(codes)
            record = codes.pop(_hash(code), None)
            self._flush()
        if record is None:
            return None
        if record["expires_at"] < time.time():
            return None
        return record

    @staticmethod
    def _expire(codes: dict) -> None:
        now = time.time()
        for key in [k for k, v in codes.items() if v.get("expires_at", 0) < now]:
            del codes[key]


class OAuthProvider:
    """The MCP SDK's OAuthAuthorizationServerProvider, backed by APP_SECRET.

    The SDK owns the parts that are pure protocol — validating the client, the
    redirect_uri, and PKCE — and calls in here for the parts that are ours.
    Reusing it rather than hand-rolling `/authorize` and `/token` is the whole
    reason PKCE enforcement and the RFC-shaped error redirects are not our bugs
    to have.
    """

    def __init__(self, store: OAuthStore, secret_service) -> None:
        self._store = store
        self._secrets = secret_service

    def _secret(self) -> str:
        secret = self._secrets.current()
        if not secret:
            # No secret means nobody can log in at all; minting a token signed
            # with "" would be a skeleton key.
            raise TokenError("server_error", "This deployment has no APP_SECRET set yet.")
        return secret

    def client_count(self) -> int:
        """How many clients have registered — for the boot log, so an operator
        can see at a glance whether their connector's registration survived."""
        return self._store.client_count()

    # ── clients (DCR) ───────────────────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.save_client(client_info)

    # ── authorize ───────────────────────────────────────────────────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Hand the browser to our own login page.

        The SDK has already checked the client and the PKCE challenge by now. All
        that is missing is proof the human at the other end is the owner, which
        is what /authorize/login collects.

        The pending request travels **in the URL, signed**, rather than in a
        server-side table. It is not secret — the client chose every value in it
        — but it is load-bearing: an attacker who could edit `redirect_uri` here
        would have the code delivered to themselves, so the signature is what
        makes the round trip safe. Being stateless also means a redeploy between
        the two halves of a login does not strand the user on a dead form.
        """
        blob = signing.issue(
            {
                "aud": _AUD_PENDING,
                "cid": client.client_id,
                "state": params.state,
                "scopes": params.scopes,
                "cc": params.code_challenge,
                "ru": str(params.redirect_uri),
                "rux": params.redirect_uri_provided_explicitly,
                "res": params.resource,
            },
            self._secret(),
            ttl_sec=PENDING_TTL_SEC,
        )
        return f"/authorize/login?p={blob}"

    def read_pending(self, blob: str | None) -> dict:
        """The signed authorize request, or an explanation of why it is not one."""
        claims = signing.verify(blob, self._secrets.current(), audience=_AUD_PENDING)
        if claims is None:
            raise PendingInvalid(
                "This sign-in link has expired or is not valid. Start again from your "
                "MCP client so it can hand over a fresh request."
            )
        return claims

    def complete(self, pending: dict) -> str:
        """Mint the authorization code, once the secret has been proven.

        Returns the client's redirect_uri with `code` and `state` attached. The
        subject is stamped in here and rides all the way through to the access
        token and, from there, onto any CDP URL it mints.
        """
        code = secrets.token_urlsafe(32)  # 256 bits; RFC 6749 §10.10 wants ≥128
        self._store.put_code(
            code,
            {
                "client_id": pending["cid"],
                "code_challenge": pending["cc"],
                "redirect_uri": pending["ru"],
                "redirect_uri_provided_explicitly": pending["rux"],
                "scopes": pending.get("scopes") or SCOPES,
                "resource": pending.get("res"),
                "subject": OWNER,
                "expires_at": time.time() + CODE_TTL_SEC,
            },
        )
        from mcp.server.auth.provider import construct_redirect_uri

        return construct_redirect_uri(pending["ru"], code=code, state=pending.get("state"))

    # ── code exchange ───────────────────────────────────────────────────────
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Load without consuming.

        The SDK calls this first and checks PKCE against what it returns, then
        calls exchange_authorization_code. Consuming here would burn the code on
        a *failed* PKCE check, turning a client's retry-able mistake into a dead
        end; consuming happens at the exchange instead.
        """
        record = self._store.peek_code(authorization_code)
        if record is None or record["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            client_id=record["client_id"],
            code_challenge=record["code_challenge"],
            redirect_uri=record["redirect_uri"],
            redirect_uri_provided_explicitly=record["redirect_uri_provided_explicitly"],
            resource=record.get("resource"),
            subject=record.get("subject"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        record = self._store.take_code(authorization_code.code)
        if record is None:
            # Already spent, or expired between the load and here. Either way the
            # honest answer is "that code is not exchangeable", and a replay of a
            # spent code lands here.
            raise TokenError("invalid_grant", "authorization code has already been used")
        return self._mint(
            subject=record.get("subject") or OWNER,
            client_id=client.client_id,
            scopes=record["scopes"],
            resource=record.get("resource"),
        )

    # ── refresh ─────────────────────────────────────────────────────────────
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = signing.verify(refresh_token, self._secrets.current(), audience=_AUD_REFRESH)
        if claims is None or claims.get("cid") != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=claims["cid"],
            scopes=claims.get("scopes") or SCOPES,
            expires_at=claims.get("exp"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        claims = signing.verify(refresh_token.token, self._secrets.current(), audience=_AUD_REFRESH)
        if claims is None:
            raise TokenError("invalid_grant", "refresh token is not valid")
        return self._mint(
            subject=claims.get("sub") or OWNER,
            client_id=client.client_id,
            scopes=scopes or refresh_token.scopes,
            resource=claims.get("res"),
        )

    # ── verification (the Resource Server half) ─────────────────────────────
    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.verify_access(token)

    def verify_access(self, token: str | None) -> AccessToken | None:
        """Synchronous because the guard middleware wants it on every request and
        there is nothing to await: it is one HMAC."""
        claims = signing.verify(token, self._secrets.current(), audience=_AUD_ACCESS)
        if claims is None:
            return None
        return AccessToken(
            token=token or "",
            client_id=claims.get("cid", ""),
            scopes=claims.get("scopes") or SCOPES,
            expires_at=claims.get("exp"),
            resource=claims.get("res"),
            subject=claims.get("sub"),
        )

    async def revoke_token(self, token) -> None:
        """Not reachable: the revocation endpoint is not advertised or mounted.

        Present because the SDK's provider protocol names it. Rotating APP_SECRET
        is the revocation lever, and it revokes everything at once.
        """
        return None

    # ── minting ─────────────────────────────────────────────────────────────
    def _mint(self, *, subject: str, client_id: str, scopes: list[str],
              resource: str | None) -> OAuthToken:
        secret = self._secret()
        common = {"sub": subject, "cid": client_id, "scopes": scopes, "res": resource}
        return OAuthToken(
            access_token=signing.issue({**common, "aud": _AUD_ACCESS}, secret, ttl_sec=ACCESS_TTL_SEC),
            token_type="Bearer",
            expires_in=ACCESS_TTL_SEC,
            scope=" ".join(scopes),
            refresh_token=signing.issue({**common, "aud": _AUD_REFRESH}, secret, ttl_sec=REFRESH_TTL_SEC),
        )
