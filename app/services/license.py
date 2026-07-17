"""Verify a CloakBrowser licence, and warm the binary while we are at it.

We write no install logic (decision #16): `ensure_binary` already resolves the
version, downloads on demand, and is called by every launch path. This module
calls it early, on purpose — and then checks what it actually got.

**That check is the point of this module.** `ensure_binary` does not raise on a
bad licence key. It logs "License validation failed, using free tier" and
returns the *free* Chromium. So "it did not throw" means nothing, and treating
it as success is how an invalid key silently runs a binary we never validated
anything against. Measured:

    real key  -> chromium-148.0.7778.215.5-pro   (Pro)
    bogus key -> chromium-146.0.7680.177.3       (free)

Why that is a correctness bug and not a wording bug: **the Step 0 fonts gate was
validated on Pro 148 only.** Free 146 is a different, older binary, so the "ship
without Windows fonts" conclusion this whole product rests on does not transfer
to it. A user silently on free 146 is outside everything we proved, will get
blocked, and will have nothing pointing at their licence. It is also exactly the
trap §5 records from our own testing, where a stray `-e` "silently downgraded a
test arm to License: Free / 146.x" and would have confounded the gate.

So this fails closed, for the same reason the geoip check does: silently running
free trades a *visible* failure for an *invisible* one, and silent blocks cannot
be diagnosed from a log.

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


class LicenseNotConfigured(RuntimeError):
    """No licence key in settings."""


class LicenseNotPro(RuntimeError):
    """A key is present but does not yield the Pro binary. Never run free."""


@dataclass(frozen=True)
class LicenseReport:
    ok: bool
    version: str = ""
    message: str = ""
    cache_dir: str = ""


def is_pro(binary_path: str | Path) -> bool:
    """Whether the resolved binary is the Pro build.

    The path is ground truth: it names the directory the package unpacked the
    binary into, so it describes what will actually execute — not what we asked
    for, and not what a validation call said a moment earlier. Pro builds cache
    under `chromium-<version>-pro`, free ones under `chromium-<version>`.
    """
    return Path(binary_path).parent.name.endswith("-pro")


def resolve_pro_binary(license_key: str, version_pin: str = "") -> str:
    """The Pro binary that will actually run, or an explanation. Never free.

    Validates before downloading rather than after, for two reasons: the failure
    can then name its real cause, and a rejected key never leaves a 150 MB free
    binary on the user's volume. The path check afterwards is not redundant —
    validation says what the server thinks, the path says what we got, and only
    the second one is the thing that runs.
    """
    if not license_key:
        raise LicenseNotConfigured(
            "CloakBrowser licence key is not configured. Add it under Settings — "
            "without it only the free binary is available, which this app does not use."
        )

    from cloakbrowser.browser import ensure_binary
    from cloakbrowser.license import validate_license

    info = validate_license(license_key)
    if info is None:
        raise LicenseNotPro(
            "Could not check your CloakBrowser licence: the licensing server did not "
            "respond, and this server has never validated a key successfully, so there "
            "is nothing cached to fall back on. Refusing to continue — without a verified "
            "licence the only browser available is the free one, which listing sites "
            "block. Try again shortly."
        )
    if not info.valid:
        raise LicenseNotPro(
            f"CloakBrowser rejected this licence key (plan: {info.plan}). Check it was "
            f"copied whole from your CloakBrowser account and has not expired. Refusing to "
            f"continue: an unrecognised key silently falls back to the free browser, which "
            f"listing sites block."
        )

    path = str(ensure_binary(license_key=license_key, browser_version=version_pin or None))
    if not is_pro(path):
        # Validation passed and we still did not get Pro. Should be unreachable —
        # but trust the path over the claim, because the path is what runs.
        raise LicenseNotPro(
            f"Your licence validated, but the browser that resolved is not the Pro build "
            f"({Path(path).parent.name}). Refusing to run it: the free browser is blocked "
            f"by listing sites. Verify your licence again from Settings."
        )
    return path


async def verify(license_key: str, version_pin: str = "") -> LicenseReport:
    """Prove the licence works and warm the binary. Returns a report; never raises.

    Early matters. The ~150 MB binary downloads lazily on first launch, and
    lazily is forced by design: at first boot there is no licence yet, so there
    is nothing to authenticate a download with. Without this action the first
    proof a key works arrives during the user's first scrape, as a failure.
    """
    from ..config import CONFIG
    from .instances import _diagnose_pin
    from .presentation import humanize_binary_error

    if not license_key:
        return LicenseReport(
            ok=False,
            message="No licence key yet. Paste your CloakBrowser Pro key above and save, "
                    "then verify.",
        )

    try:
        path = await asyncio.to_thread(resolve_pro_binary, license_key, version_pin)
    except (LicenseNotConfigured, LicenseNotPro) as exc:
        return LicenseReport(ok=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 — every failure is a user-facing message
        diagnosis = _diagnose_pin(exc, version_pin)
        return LicenseReport(ok=False, message=humanize_binary_error(diagnosis or str(exc)))

    version = _version_from_path(path)
    return LicenseReport(
        ok=True,
        version=version,
        cache_dir=str(CONFIG.binary_cache_dir),
        message=(
            f"Licence accepted. CloakBrowser Pro {version} is downloaded and ready on the "
            f"volume, so your first scrape will not wait for it."
        ),
    )


def _version_from_path(path: str) -> str:
    """Pull the resolved version out of the cached binary's path.

    Best-effort and deliberately so: the layout is the package's business, and a
    version we cannot read is worth degrading over, not failing over — the
    licence still verified, which is what was asked.
    """
    import re

    match = re.search(r"(\d+\.\d+\.\d+\.\d+(?:\.\d+)?)", path)
    return match.group(1) if match else "(version not reported)"
