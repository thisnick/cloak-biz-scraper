"""Verify a CloakBrowser licence, and warm the binary while we are at it.

We write no install logic (decision #16): `ensure_binary` already resolves the
version, downloads on demand, and is called by every launch path. This module
just calls it early, on purpose.

Early matters. The binary is ~150 MB and downloads lazily on first launch — and
lazily is not a compromise here, it is forced: at first boot the user has not
entered a licence yet, so there is nothing to authenticate a download with. That
leaves a real hazard, which this action removes: without it the first proof that
a licence key is even valid arrives during the user's first scrape, as a failure,
several minutes in. Clicking "Verify licence" turns that into an immediate,
legible answer *and* leaves the download warm on the volume.

So this is one action doing two jobs, and both are the point:
  * proof the key works, now, next to the field they just typed it into;
  * the 150 MB fetched before the first scrape rather than during it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("cloakbiz.license")


@dataclass(frozen=True)
class LicenseReport:
    ok: bool
    version: str = ""
    message: str = ""
    cache_dir: str = ""


async def verify(license_key: str, version_pin: str = "") -> LicenseReport:
    """Resolve and download the Pro binary. Returns a report; never raises."""
    from ..config import CONFIG
    from .instances import _diagnose_pin
    from .presentation import humanize_binary_error

    if not license_key:
        return LicenseReport(
            ok=False,
            message=(
                "No licence key yet. Paste your CloakBrowser Pro key above and save, "
                "then verify."
            ),
        )

    def _run() -> tuple[bool, str, str]:
        from cloakbrowser.browser import ensure_binary

        path = ensure_binary(
            license_key=license_key, browser_version=version_pin or None
        )
        # The resolved version is the directory the package cached it under —
        # the only place the *actual* resolution is observable, which is exactly
        # what an unpinned user needs to see.
        return True, _version_from_path(str(path)), str(path)

    try:
        _, version, path = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001 — every failure is a user-facing message
        diagnosis = _diagnose_pin(exc, version_pin)
        return LicenseReport(
            ok=False,
            message=humanize_binary_error(diagnosis or str(exc)),
        )

    return LicenseReport(
        ok=True,
        version=version,
        cache_dir=str(CONFIG.binary_cache_dir),
        message=(
            f"Licence accepted. CloakBrowser Pro {version} is downloaded and ready on "
            f"the volume, so your first scrape will not wait for it."
        ),
    )


def _version_from_path(path: str) -> str:
    """Pull the resolved version out of the cached binary's path.

    Best-effort and deliberately so: the path layout is the package's business,
    and a version we cannot read is worth degrading over, not failing over — the
    licence still verified, which is what was asked.
    """
    import re

    match = re.search(r"(\d+\.\d+\.\d+\.\d+(?:\.\d+)?)", path)
    return match.group(1) if match else "(version not reported)"
