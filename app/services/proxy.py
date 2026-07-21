"""Optional Evomi residential proxy URL builder.

Ported from browserd (app/proxy.py). The parts come from the settings store
rather than the environment, and an entirely empty proxy is now the supported
direct-egress mode.

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
    """Some proxy fields are present, but they do not form a usable proxy."""


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
    def optional_from_settings(cls, settings: Settings) -> "ProxyParts | None":
        """A complete proxy, or None only when direct mode is intentional.

        A partial proxy is not the same thing as no proxy. Silently treating a
        typo or a half-filled form as direct mode would turn a configured-but-
        broken proxy into an egress fallback, violating the fail-closed promise.
        """
        if not settings.proxy_present():
            return None
        return cls.from_settings(settings)

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
                f"Proxy settings are incomplete (missing: {', '.join(missing)}). "
                f"Complete the Evomi Proxy fields, or choose direct connection in Settings. "
                f"The browser will not silently bypass a proxy you started configuring."
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
    """Compose the full proxy URL for a given sticky session token.

    Country/region are included only when set: an empty value would otherwise
    emit a bare ``_country-_region-``, which is not valid Evomi targeting.
    """
    c = country or parts.country
    r = region or parts.region
    pw = parts.password
    if c:
        pw += f"_country-{c}"
    if r:
        pw += f"_region-{r}"
    pw += f"_session-{session}_lifetime-{lifetime}"
    return f"http://{parts.user}:{pw}@{parts.host}:{parts.port}"


def masked(url: str) -> str:
    """Redact all proxy userinfo for logging.

    Usernames are account identifiers too, not harmless labels. Splitting on the
    LAST '@' also matters: one inside credentials must not leave the password
    tail sitting in the log line pretending to be a hostname.
    """
    try:
        scheme, rest = url.split("://", 1)
        _, hostport = rest.rsplit("@", 1)
        return f"{scheme}://***@{hostport}"
    except ValueError:
        return "***"
