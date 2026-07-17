"""The deployment's own public URL, as a client outside would write it.

**Why this is not just `request.base_url`.** Railway terminates TLS at its edge
and speaks plain HTTP to the container, so the socket uvicorn sees is http and
`request.url.scheme` says so. Everything derived from it is then wrong in the
one place it matters and right everywhere we would test it:

* the OAuth issuer would advertise `http://…`, which RFC 8414 clients refuse
  outright (the SDK's own `validate_issuer_url` refuses it too, for anything but
  localhost) — so discovery fails for every real client;
* `cdp_url`/`vnc_url` would come out `ws://` on an `https://` page, which
  browsers block as mixed content.

Both are invisible locally, where http *is* the truth. This is the same shape as
the `421` FastMCP bug from Step 3: passes every test on the developer's machine,
fails for every actual user.

`X-Forwarded-Proto` is the fix, and reading it here rather than relying on
uvicorn's `--forwarded-allow-ips` is deliberate belt-and-braces: that flag
defaults to trusting only 127.0.0.1, Railway's edge is not 127.0.0.1, and a
silently-ignored header would put us straight back to `http`. The flag is set in
the Dockerfile as well, because it is also what makes `request.client.host`
report the real caller.

**Trusting a client-writable header is a bounded concession.** On Railway the
container is only reachable through the edge, which overwrites this header. Run
directly on a LAN and a caller can lie about it — the worst they achieve is
being handed a URL with the wrong scheme, which harms only themselves. Nothing
here is an authorization decision.
"""
from __future__ import annotations


def base_from(headers, fallback_scheme: str, fallback_host: str = "") -> str:
    """The core: everything here is derived from headers, not from a request.

    Split out because the OAuth guard runs as ASGI middleware, where there is a
    scope and no Request — and building one just to read two headers invited a
    fake request object that only pretended to be one.
    """
    forwarded = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    scheme = forwarded if forwarded in ("http", "https") else fallback_scheme
    host = (headers.get("host") or fallback_host or "").strip()
    return f"{scheme}://{host}"


def public_base(request) -> str:
    """`https://host` — no trailing slash, no path."""
    return base_from(request.headers, request.url.scheme, request.url.netloc)


def websocket_base(request) -> str:
    """`wss://host` — the same origin, spelled for a WebSocket client."""
    base = public_base(request)
    return base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
