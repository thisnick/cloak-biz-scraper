"""Resolve the selected CloakBrowser build, and warm it while we are at it.

We write no install logic (decision #16): `ensure_binary` already resolves the
version, downloads on demand, and is called by every launch path. This module
calls it early, on purpose — and then checks what it actually got.

There are two deliberate modes. With no key, the public build is a supported
choice. With a key, the user is explicitly asking for Pro, so a rejected,
expired, or otherwise non-Pro key must fail visibly instead of silently falling
back. `ensure_binary` does not enforce that second rule: it logs "License
validation failed, using free tier" and returns public Chromium. Measured:

    real key  -> chromium-148.0.7778.215.5-pro   (Pro)
    bogus key -> chromium-146.0.7680.177.3       (free)

Why the distinction is correctness rather than wording: **the Step 0 fonts and
listing-site gates were validated on Pro 148 only.** Public 146 is a different,
older binary with fewer bypasses, and we have not tested it against the listing
sites. Running it because the user deliberately left the key blank is honest.
Running it after they supplied a bad key is the exact "I thought I had Pro"
trap the guard exists to prevent.

So a present key fails closed, for the same reason the geoip check does:
silently downgrading trades a *visible* failure for an *invisible* one, and
silent blocks cannot be diagnosed from a log. A blank key takes the public path
without contacting the licensing server.

**Measured: failing closed does not put a licensing outage between a paying user
and their browser.** `validate_license` caches a successful validation on the
volume and falls back to it with `ignore_ttl=True` when the server is
unreachable, so an established deployment keeps resolving Pro right through an
outage:

    outage, licence cache present -> valid=True plan=team -> ...-pro
    outage, licence cache absent  -> None                 -> free 146

The cache is absent only if no validation ever succeeded on this volume — which
is also the only way there is no Pro binary to fall back to. So pinning
CLOAKBROWSER_BINARY_PATH would buy nothing: the package's own stale cache
already means "keep working with what we have".
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("cloakbiz.license")


class LicenseNotPro(RuntimeError):
    """A present key does not yield Pro. Never silently downgrade that key."""


@dataclass(frozen=True)
class LicenseReport:
    ok: bool
    version: str = ""
    message: str = ""
    cache_dir: str = ""
    pro: bool | None = None
    binary_path: str = ""


def is_pro(binary_path: str | Path) -> bool:
    """Whether the resolved binary is the Pro build.

    The path is ground truth: it names the directory the package unpacked the
    binary into, so it describes what will actually execute — not what we asked
    for, and not what a validation call said a moment earlier. Pro builds cache
    under `chromium-<version>-pro`, free ones under `chromium-<version>`.
    """
    return Path(binary_path).parent.name.endswith("-pro")


def resolve_browser_binary(license_key: str, version_pin: str = "") -> str:
    """The selected binary that will run: public if blank, Pro if keyed.

    A blank key is an intentional public-build selection and skips licence
    validation. A present key validates before downloading, so a rejected key
    never leaves a public binary behind as an accidental fallback. The path
    check afterwards is not redundant: validation says what the server thinks,
    the path says what we got, and only the second one is the thing that runs.
    """
    from cloakbrowser.browser import ensure_binary

    license_key = license_key.strip()
    if not license_key:
        path = str(ensure_binary(license_key=None, browser_version=version_pin or None))
        if is_pro(path):
            # This should only be possible through an unexpected package/local
            # override. Trust the artifact rather than labelling it public.
            raise RuntimeError(
                "No CloakBrowser key was supplied, but the resolved browser identifies as "
                f"Pro ({Path(path).parent.name}). Refusing to mislabel the running build."
            )
        return path

    from cloakbrowser.license import validate_license

    info = validate_license(license_key)
    if info is None:
        raise LicenseNotPro(
            "Could not check your CloakBrowser licence: the licensing server did not "
            "respond, and this server has never validated this key successfully, so there "
            "is nothing cached to fall back on. Refusing to silently switch your saved key "
            "to the public build. Try again shortly."
        )
    if not info.valid:
        raise LicenseNotPro(
            f"CloakBrowser rejected this licence key (plan: {info.plan}). Check it was "
            f"copied whole from your CloakBrowser account and has not expired. Your saved "
            f"key will not be silently downgraded to the public build."
        )

    path = str(ensure_binary(license_key=license_key, browser_version=version_pin or None))
    if not is_pro(path):
        # Validation passed and we still did not get Pro. Should be unreachable —
        # but trust the path over the claim, because the path is what runs.
        raise LicenseNotPro(
            f"Your licence validated, but the browser that resolved is not the Pro build "
            f"({Path(path).parent.name}). Refusing to silently downgrade your saved key. "
            f"Verify the licence again from Settings."
        )
    return path


async def verify(license_key: str, version_pin: str = "") -> LicenseReport:
    """Prove the selected build works and warm it. Returns a report; never raises.

    Early matters. The ~150 MB binary downloads lazily on first launch, and
    lazily is forced by design. Without this action the first proof a public
    build downloads, or that a Pro key works, arrives during the user's first
    scrape as a delay or failure.
    """
    from ..config import CONFIG
    from .instances import _diagnose_pin
    from .presentation import humanize_binary_error

    try:
        path = await asyncio.to_thread(resolve_browser_binary, license_key, version_pin)
    except LicenseNotPro as exc:
        return LicenseReport(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 — every failure is a user-facing message
        diagnosis = _diagnose_pin(exc, version_pin)
        return LicenseReport(ok=False, message=humanize_binary_error(diagnosis or str(exc)))

    version = _version_from_path(path)
    pro = is_pro(path)
    if not pro:
        return LicenseReport(
            ok=True,
            version=version,
            cache_dir=str(CONFIG.binary_cache_dir),
            pro=False,
            binary_path=path,
            message=(
                f"CloakBrowser public build {version} is downloaded and ready on the volume. "
                f"It has fewer bypasses than Pro and has not been tested by us against the "
                f"listing sites."
            ),
        )
    return LicenseReport(
        ok=True,
        version=version,
        cache_dir=str(CONFIG.binary_cache_dir),
        pro=True,
        binary_path=path,
        message=(
            f"Licence accepted. CloakBrowser Pro {version} is downloaded and ready on the "
            f"volume, so your first scrape will not wait for it."
        ),
    )


def _version_from_path(path: str) -> str:
    """Pull the resolved version out of the cached binary's path.

    Best-effort and deliberately so: the layout is the package's business, and a
    version we cannot read is worth degrading over, not failing over — the
    selected artifact still resolved, which is what was asked.
    """
    import re

    match = re.search(r"(\d+\.\d+\.\d+\.\d+(?:\.\d+)?)", path)
    return match.group(1) if match else "(version not reported)"
