"""Measure a proxy: does it route, where does it exit, and what time is it there?

This module exists because of a specific bug and a specific temptation.

**The bug.** Step 1 launched browsers with `tz = tz or "America/Los_Angeles"`.
When geoip resolution failed, that reported a timezone nobody had measured as
though it were the resolved truth. An unroutable proxy still launched, still
held a pool slot, and every page load failed `ERR_PROXY_CONNECTION_FAILED` —
while the instance cheerfully claimed a California timezone.

**The temptation** is to fix it by checking the package's `maybe_resolve_geoip`
for a None IP. That is not enough, and the reason is worth stating so nobody
"simplifies" this back. `cloakbrowser.geoip.resolve_proxy_geo_with_ip` does:

    ip = _resolve_exit_ip(proxy_url, ...)          # echo service THROUGH the proxy
    if ip is None and proxy_url and not expired:
        ip = _resolve_proxy_ip(proxy_url)          # ← DNS of the proxy HOSTNAME

That fallback returns the proxy **gateway's** address — the front door we dial,
not the residential exit we egress from. For a proxy that resolves in DNS but
cannot actually route (dead port, revoked account, wrong host), the echo call
fails and the fallback quietly succeeds. The caller gets a non-None IP and a
timezone geolocated from the *gateway*, which is a plausible-looking value that
was never measured at the exit and may be a continent away from it. So the
failure mode survives a None check; it just gets harder to see.

We therefore measure the exit ourselves, through the proxy, with no fallback:
an IP we did not read back through the tunnel is not an exit IP. Reaching an
echo service through the proxy is also the only honest proof the proxy routes at
all, which is what lets a launch fail fast instead of holding a slot.

**What this cannot tell you.** Evomi does not validate passwords (verified): a
garbage password still returns a working residential exit, and only a bad
*username* yields 407. A successful probe therefore proves the proxy routes and
says where it exits. It does **not** prove the credentials are correct, and
nothing in the UI may claim it does.

The GeoLite2 database is the `cloakbrowser` package's own cached copy — one ~70
MB file on the volume, refreshed on its schedule, giving the same tz the package
would compute. `test_geo.py` guards the private imports so a package upgrade
that moves them fails in CI rather than in front of a user.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("cloakbiz.geo")

# Fail-fast budget. A dead proxy usually refuses or NXDOMAINs in well under a
# second; this only bounds the blackholed case, where the cost of waiting is a
# user watching a spinner. The worst case is this times the number of echoes
# below (8s), which is the number to reason about — measured at 8.1s against a
# blackholed 192.0.2.1.
_PER_ECHO_TIMEOUT_SEC = 4.0

# Plain-text IP echoes. Two, so one being down is not an outage; both are HTTPS,
# so the proxy operator cannot rewrite the answer in flight.
_ECHO_URLS = ("https://api.ipify.org", "https://checkip.amazonaws.com")


class ProxyUnreachable(RuntimeError):
    """The proxy did not route a request. Never launch: it would hold a slot and
    fail every page load."""


class GeoUnresolved(RuntimeError):
    """The exit IP was measured but could not be geolocated."""


@dataclass(frozen=True)
class ProxyProbe:
    """What was actually measured at the proxy's exit. Every field is observed;
    nothing here is defaulted, inferred, or filled in from a constant."""

    exit_ip: str
    timezone: str | None = None  # None = looked up and not found. Never a guess.
    locale: str | None = None
    country: str | None = None
    city: str | None = None

    @property
    def geo_resolved(self) -> bool:
        return self.timezone is not None

    def describe(self) -> str:
        """A one-line summary for the UI. Says 'unknown', never a plausible default."""
        where = ", ".join(p for p in (self.city, self.country) if p) or "location unknown"
        tz = self.timezone or "unknown"
        return f"{self.exit_ip} — {where} (timezone {tz})"


async def measure_exit_ip(proxy_url: str) -> str:
    """Read our own address back through the tunnel, or raise.

    No hostname fallback by design: a DNS answer for the gateway is not evidence
    about the exit. If nothing comes back through the proxy, we know nothing.
    """
    errors: list[str] = []
    async with httpx.AsyncClient(
        proxy=proxy_url, timeout=_PER_ECHO_TIMEOUT_SEC, follow_redirects=False
    ) as client:
        for url in _ECHO_URLS:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                ip = resp.text.strip()
                ipaddress.ip_address(ip)  # a proxy error page is not an IP
                return ip
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                continue
    raise ProxyUnreachable(
        "Could not reach the internet through the proxy, so its exit IP is unknown. "
        "Check the host, port, and username — an unroutable proxy cannot be used, and "
        "launching a browser on it would fail every page load. "
        "Tried " + "; ".join(errors)
    )


def _geolocate(ip: str) -> tuple[str | None, str | None, str | None, str | None]:
    """(timezone, locale, country, city) for an IP; Nones when the DB cannot say."""
    try:
        import geoip2.database

        from cloakbrowser.geoip import COUNTRY_LOCALE_MAP, _ensure_geoip_db
    except ImportError as exc:  # geoip2 comes via cloakbrowser[geoip]
        logger.warning("geoip lookup unavailable: %s", exc)
        return None, None, None, None

    db_path = _ensure_geoip_db()
    if db_path is None:
        logger.warning("GeoLite2 database unavailable; cannot resolve the exit's geo")
        return None, None, None, None
    try:
        with geoip2.database.Reader(str(db_path)) as reader:
            resp = reader.city(ip)
            country = resp.country.iso_code
            return (
                resp.location.time_zone,
                COUNTRY_LOCALE_MAP.get(country) if country else None,
                country,
                resp.city.name,
            )
    except Exception as exc:
        logger.warning("geoip lookup failed for %s: %s", ip, exc)
        return None, None, None, None


async def probe(proxy_url: str, *, geo: bool = True) -> ProxyProbe:
    """Measure the proxy. Raises ProxyUnreachable if it does not route."""
    ip = await measure_exit_ip(proxy_url)
    if not geo:
        return ProxyProbe(exit_ip=ip)
    tz, locale, country, city = await asyncio.to_thread(_geolocate, ip)
    return ProxyProbe(exit_ip=ip, timezone=tz, locale=locale, country=country, city=city)
