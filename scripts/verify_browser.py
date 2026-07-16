"""Launch a real browser through the proxy and report what the internet sees.

Runs inside the container, against the service layer directly — no HTTP façade
involved, which is the point: if behaviour lives in services/ then it is
testable without a route.

  docker compose exec app python scripts/verify_browser.py

Prints a JSON report: the binary that was used, the exit IP and geo as resolved
from the proxy, and the same values as observed from inside the page.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from app.config import CONFIG, bootstrap_binary_cache, purge_binary_env  # noqa: E402
from app.models import InstanceCreate  # noqa: E402
from app.services.instances import InstanceManager  # noqa: E402
from app.services.settings import SettingsService  # noqa: E402

# Tried in order. Free echo services rate-limit per exit IP, and a residential
# proxy hands out IPs other people have already spent the quota on — so one
# refusing to answer says nothing about the browser. Fall through to the next.
ECHO_URLS = (
    "http://ip-api.com/json/?fields=query,city,regionName,country,timezone,isp",
    "https://ipinfo.io/json",
    "https://api.ipify.org?format=json",
)


def _cache_state(cache: Path) -> dict:
    """Binary inventory: what is on the volume, how big, and when it landed."""
    entries = []
    for child in sorted(cache.glob("chromium-*")):
        chrome = child / "chrome"
        entries.append({
            "dir": child.name,
            "chrome_exists": chrome.exists(),
            "bytes": sum(f.stat().st_size for f in child.rglob("*") if f.is_file()),
            "mtime": chrome.stat().st_mtime if chrome.exists() else None,
        })
    return {
        "cache_dir": str(cache),
        "env_CLOAKBROWSER_CACHE_DIR": os.environ.get("CLOAKBROWSER_CACHE_DIR"),
        "binaries": entries,
        "markers": sorted(p.name for p in cache.glob("latest_pro_version_*")),
    }


def _running_binary(settings) -> dict:
    """Which Chromium is actually running, and which one the settings resolve to.

    The two must agree. `resolved` repeats the exact ensure_binary() call the
    launch made — cached by now, so it costs nothing and cannot download.
    `actually_running` reads it back off the live process instead of trusting us.
    """
    from cloakbrowser import ensure_binary

    resolved = ensure_binary(
        license_key=settings.cloakbrowser_license_key,
        browser_version=settings.cloakbrowser_version or None,
    )
    running = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            exe = os.readlink(proc / "exe")
        except OSError:
            continue
        if "chromium-" in exe:
            running.add(exe)
    return {
        "pin_in_settings": settings.cloakbrowser_version or "(none — track latest)",
        "resolved": resolved,
        "actually_running": sorted(running),
    }


async def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "verify"
    cache = bootstrap_binary_cache()
    settings_service = SettingsService(CONFIG.settings_path, CONFIG.dek_path)
    settings = settings_service.load()
    purge_binary_env()

    report: dict = {
        "profile": profile,
        "settings": settings.redacted(),
        "cache_before": _cache_state(cache),
        # Proof that the launch cannot be reading these from the environment.
        "env_after_purge": {
            "CLOAKBROWSER_LICENSE_KEY": os.environ.get("CLOAKBROWSER_LICENSE_KEY"),
            "CLOAKBROWSER_VERSION": os.environ.get("CLOAKBROWSER_VERSION"),
        },
    }

    manager = InstanceManager(settings_service)
    started = time.monotonic()
    try:
        inst = await manager.launch(InstanceCreate(profile=profile), origin="interactive")
    except Exception as exc:
        report["launch_error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, indent=2))
        return 1

    report["launch_sec"] = round(time.monotonic() - started, 1)
    report["binary"] = _running_binary(settings)
    report["instance"] = {
        "id": inst.id,
        "proxy_ip": inst.proxy_ip,
        "timezone": inst.timezone,
        "locale": inst.locale,
        "display": inst.display,
        "cdp_port": inst.cdp_port,
        "seed": inst.seed,
        "headed": inst.headed,
    }
    report["cache_after"] = _cache_state(cache)

    try:
        page = await inst.context.new_page()
        attempts = []
        for url in ECHO_URLS:
            try:
                await page.goto(url, timeout=90_000)
                body = await page.evaluate("() => document.body.innerText")
                echo = json.loads(body)
            except Exception as exc:
                attempts.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if isinstance(echo, dict) and echo.get("status") in (429, "fail"):
                attempts.append({"url": url, "refused": echo})  # rate limited, try the next
                continue
            report["page_sees"] = {"url": url, **echo}
            break
        else:
            report["page_sees_error"] = "every echo service refused"
        if attempts:
            report["echo_attempts"] = attempts
        report["page_intl"] = await page.evaluate(
            "() => ({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,"
            " locale: navigator.language, languages: navigator.languages,"
            " offsetMin: new Date().getTimezoneOffset() })"
        )
        report["page_ua"] = await page.evaluate("() => navigator.userAgent")
    except Exception as exc:
        report["page_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await manager.stop(inst.id)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
