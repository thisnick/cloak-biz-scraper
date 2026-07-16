"""Evomi residential proxy URL builder.

Ported from browserd (app/proxy.py). The only change: the parts come from the
settings store rather than the environment, because in this app the user fills
them into a web form.

Composes a per-session sticky proxy. The session token pins a sticky exit IP;
country/region pin geography so geoip stays coherent even as the exact IP drifts
within region across the session lifetime.

Live shape (suffix on the password, per Evomi):
  http://USER:PASSWORD_country-US_region-california_session-<tok>_lifetime-2@HOST:PORT
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from .settings import Settings

_ALPHABET = string.ascii_uppercase + string.digits


class ProxyNotConfigured(RuntimeError):
    """No usable proxy in settings — never launch bare, the real IP would leak."""


def new_session_token(n: int = 9) -> str:
    """A fresh sticky-session token (uppercase alnum, matches Evomi examples)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


@dataclass(frozen=True)
class ProxyParts:
    user: str
    password: str
    host: str
    port: str
    country: str
    region: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProxyParts":
        if not settings.proxy_configured():
            missing = [
                name
                for name, value in (
                    ("proxy_user", settings.proxy_user),
                    ("proxy_password", settings.proxy_password),
                    ("proxy_host", settings.proxy_host),
                    ("proxy_port", settings.proxy_port),
                )
                if not value
            ]
            raise ProxyNotConfigured(
                f"Proxy is not configured (missing: {', '.join(missing)}). Add your "
                f"residential proxy under Settings before launching a browser."
            )
        return cls(
            user=settings.proxy_user,
            password=settings.proxy_password,
            host=settings.proxy_host,
            port=settings.proxy_port,
            country=settings.proxy_country,
            region=settings.proxy_region,
        )


def build_proxy_url(
    session: str,
    parts: ProxyParts,
    *,
    country: str | None = None,
    region: str | None = None,
    lifetime: int = 2,
) -> str:
    """Compose the full proxy URL for a given sticky session token."""
    c = country or parts.country
    r = region or parts.region
    pw = f"{parts.password}_country-{c}_region-{r}_session-{session}_lifetime-{lifetime}"
    return f"http://{parts.user}:{pw}@{parts.host}:{parts.port}"


def masked(url: str) -> str:
    """Redact the password portion for logging.

    Splits on the LAST '@' rather than the first: the credentials are the user's
    to choose, and one containing '@' would otherwise split early and leave the
    tail of the password sitting in the log line pretending to be a hostname.
    """
    try:
        scheme, rest = url.split("://", 1)
        creds, hostport = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{hostport}"
    except ValueError:
        return "***"
