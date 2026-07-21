"""Instance pool — launch/stop/track CloakBrowser instances, each on its own
X display and optionally its own Evomi per-session proxy.

Ported from browserd (app/instances.py), which in turn adapted the
launch/track/DISPLAY-per-instance/CDP-port/singleton-cleanup/on-close skeleton
from CloakBrowser-Manager (MIT) backend/browser_manager.py, and added the Evomi
proxy builder, geoip resolution, provenance, the reserve split, and
condition-based wait-for-slot acquisition.

Changes made in the port:
  * the pool budget comes from the settings store, not the environment, and is
    re-read per launch so a change in the UI takes effect without a restart;
  * the license key and version pin are passed as launch arguments from settings
    — we write no install logic, ensure_binary() downloads on demand into the
    volume (CLOAKBROWSER_CACHE_DIR, set at process start);
  * when a proxy is configured, timezone/locale are measured at its exit or
    reported as unknown — never defaulted, and an unroutable proxy fails fast
    rather than holding a pool slot on a browser that cannot load a page;
  * with no proxy fields configured, launch uses direct server egress and skips
    proxy probing/geolocation entirely; a partial proxy still fails visibly and
    is never treated as permission to fall back to direct egress;
  * Xvfb stands in for KasmVNC until live inspection is built.

Two consumer classes share one memory-bound pool via a reservation:
  * interactive (agent/human) — guaranteed a floor of slots, never preempted
  * task (batch scraping)     — capped at MAX - RESERVE, waits for a slot
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import CONFIG
from . import geo
from .display import DisplayManager
from .geo import GeoUnresolved
from .license import resolve_pro_binary
from .profiles import ProfileStore
from .proxy import ProxyParts, build_proxy_url, masked
from .settings import SettingsService

logger = logging.getLogger("cloakbiz.instances")

_BASE_CDP_PORT = 9222
_CDP_RANGE = 100
_IDLE_TTL_MIN = 15
_HARD_TTL_MIN = 60

Origin = Literal["task", "interactive"]


class CapExceeded(RuntimeError):
    pass


class PinUnavailable(RuntimeError):
    """The pinned version could not be downloaded."""


def _diagnose_pin(exc: BaseException, pin: str) -> str | None:
    """Correct the retry advice on a 404 for a pinned version, or return None.

    Worth the string matching. The package reports a pinned version it cannot
    fetch as "the Pro binary could not be downloaded right now. Retry in a
    moment", which sends you off debugging your network or your license for a
    condition that is permanent and entirely about the pin.

    Deliberately names no cause. A 404 tells us only that the version is not
    downloadable — not whether it was retired, mistyped, or never existed. The
    platform tag is included as context because it forms part of the request,
    NOT because it is implicated: probing the download API shows withdrawn
    versions return 404 on every architecture, so architecture is not the
    discriminator and guessing that it is would send the reader hunting for an
    arch-specific build that exists for nobody.
    """
    if not pin or "404" not in str(exc):
        return None

    from cloakbrowser.config import get_platform_tag

    return (
        f"CloakBrowser {pin} is not available for download, so this pin cannot be "
        f"satisfied and retrying will not help. Check that the version exists and is "
        f"spelled as published — builds are withdrawn once superseded. Clear the pin "
        f"in Settings to track the latest build instead. "
        f"(Resolved platform: {get_platform_tag()}. Underlying error: {exc})"
    )


@dataclass
class Instance:
    id: str
    profile: str
    origin: Origin
    owner: str | None
    context: Any
    display: int
    cdp_port: int
    proxy_ip: str | None
    timezone: str | None
    locale: str | None
    headed: bool
    geoip: bool
    humanize: bool
    seed: int
    ttl_min: int
    created_wall: float
    created_mono: float
    # The OAuth subject that asked for this browser. Deliberately NOT `owner`,
    # which already means something else here: `owner` is job attribution
    # ("job:abc123", "archive:foo") and says which piece of work a browser
    # belongs to, not which principal. Overloading the two silently broke both —
    # a sweep's browser has owner="job:…", which no OAuth subject ever equals, so
    # every task browser was refused as "invalid token" rather than "belongs to a
    # sweep", and its live view could not be opened at all.
    #
    # None for browsers launched by a sweep: the job owns them, and the endpoints
    # fall back to the deployment's single subject rather than to "anyone".
    subject: str | None = None
    # None when this browser has no live view: the display fell back to Xvfb
    # because Xvnc was absent. Callers omit vnc_url rather than mint one that
    # would connect to nothing. Defaulted so that "no live view" is what an
    # instance built without thinking about VNC gets, rather than a port that
    # was never opened.
    vnc_port: int | None = None
    last_used_mono: float = field(default=0.0)

    def touch(self) -> None:
        self.last_used_mono = time.monotonic()

    def idle_sec(self) -> float:
        return time.monotonic() - self.last_used_mono

    def age_sec(self) -> float:
        return time.monotonic() - self.created_mono


class InstanceManager:
    def __init__(self, settings: SettingsService) -> None:
        self.running: dict[str, Instance] = {}
        self.displays = DisplayManager()
        self.profiles = ProfileStore(CONFIG.profiles_dir)
        self._settings = settings
        self._cond = asyncio.Condition()
        self._pending: dict[str, int] = {"task": 0, "interactive": 0}
        self._next_cdp = _BASE_CDP_PORT

    # ── capacity accounting (call under self._cond) ──────────────────────────
    def _running_by(self, origin: str) -> int:
        return sum(1 for i in self.running.values() if i.origin == origin)

    def _total(self) -> int:
        return len(self.running) + self._pending["task"] + self._pending["interactive"]

    def _task_count(self) -> int:
        return self._running_by("task") + self._pending["task"]

    def _can(self, origin: str) -> bool:
        s = self._settings.load()
        if self._total() >= s.max_instances:
            return False
        if origin == "task":
            return self._task_count() < s.task_budget
        return True  # interactive: only bounded by MAX (reserve is a floor, not a cap)

    def counts(self) -> dict[str, int]:
        s = self._settings.load()
        return {
            "task": self._running_by("task"),
            "interactive": self._running_by("interactive"),
            "total": len(self.running),
            "max": s.max_instances,
            "task_budget": s.task_budget,
            "reserve": s.interactive_reserve,
        }

    async def _acquire(self, origin: str, wait: bool) -> None:
        s = self._settings.load()
        async with self._cond:
            if wait:
                await self._cond.wait_for(lambda: self._can(origin))
            elif not self._can(origin):
                if origin == "task":
                    raise CapExceeded(f"task budget full ({s.task_budget})")
                raise CapExceeded(f"pool full ({s.max_instances}); reserve in use")
            self._pending[origin] += 1

    async def _release_pending(self, origin: str) -> None:
        async with self._cond:
            self._pending[origin] -= 1
            self._cond.notify_all()

    def _alloc_cdp_port(self) -> int:
        for _ in range(_CDP_RANGE):
            port = self._next_cdp
            self._next_cdp = _BASE_CDP_PORT + ((self._next_cdp + 1 - _BASE_CDP_PORT) % _CDP_RANGE)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("no free CDP port")

    async def launch(self, req, *, origin: Origin = "interactive",
                     owner: str | None = None, subject: str | None = None,
                     wait: bool | None = None) -> Instance:
        if wait is None:
            wait = origin == "task"
        await self._acquire(origin, wait)
        try:
            inst = await self._do_launch(req, origin, owner, subject)
        except BaseException:
            await self._release_pending(origin)
            raise
        async with self._cond:
            self._pending[origin] -= 1
            self.running[inst.id] = inst
            self._cond.notify_all()
        logger.info("launched %s origin=%s owner=%s (display=:%d cdp=%d ip=%s) [%d/%d]",
                    inst.id, origin, owner, inst.display, inst.cdp_port, inst.proxy_ip,
                    len(self.running), self._settings.load().max_instances)
        return inst

    async def _do_launch(self, req, origin: Origin, owner: str | None,
                         subject: str | None = None) -> Instance:
        from cloakbrowser import launch_persistent_context_async

        settings = self._settings.load()

        # Resolve the binary before anything else, and refuse anything but Pro.
        # Guarding only the *empty* key is not enough: an invalid one does not
        # raise, it silently resolves the free browser — which is a different,
        # older binary that the Step 0 fonts gate never covered. Cheap to do
        # here: every launch path calls ensure_binary anyway, so this hits the
        # same cache a moment earlier and simply looks at what came back.
        await asyncio.to_thread(
            resolve_pro_binary,
            settings.cloakbrowser_license_key,
            settings.cloakbrowser_version,
        )
        # No proxy fields is an intentional, supported direct mode. Any partial
        # configuration still raises here: a typo or half-filled form must never
        # be reinterpreted as permission to bypass the proxy.
        parts = ProxyParts.optional_from_settings(settings)

        profile = self.profiles.get_or_create(
            req.profile,
            default_country=parts.country if parts else settings.proxy_country,
            default_region=parts.region if parts else settings.proxy_region,
            country=req.country,
            region=req.region,
        )
        proxy_url: str | None = None
        proxy_ip = tz = locale = None
        launch_geoip = False
        if parts is not None:
            proxy_url = build_proxy_url(
                profile.session_token, parts, country=profile.country, region=profile.region
            )
            logger.info("launch profile=%s proxy=%s", profile.name, masked(proxy_url))

            # Measure a configured proxy before spending a display and a pool
            # slot on it. A proxy that cannot route is broken, not optional: fail
            # fast and never retry direct. This branch is the only place the
            # proxy probe/geolocator runs.
            probe = await geo.probe(proxy_url, geo=req.geoip)
            proxy_ip, tz, locale = probe.exit_ip, probe.timezone, probe.locale
            launch_geoip = req.geoip

            if req.geoip and not probe.geo_resolved:
                # geoip=True asks for geographic coherence and we cannot deliver it.
                #
                # Launching anyway is not "degraded but working": a browser reporting
                # the container's UTC from behind a residential exit in California is
                # itself an anti-bot tell. It would trade a visible failure for an
                # invisible one — silent blocks, with nothing in the logs to explain
                # them. So refuse, and never substitute a plausible default.
                raise GeoUnresolved(
                    f"The proxy routes (exit IP {proxy_ip}), but its location could not be "
                    f"resolved. Launching now would give the browser a timezone that "
                    f"contradicts its exit IP, which is exactly what listing sites look for. "
                    f"This usually means the GeoLite2 database could not be downloaded — "
                    f"retry in a moment. To launch anyway and accept an unknown timezone, "
                    f"pass geoip=false."
                )
        else:
            # Direct mode deliberately does not call our probe or ask the package
            # to geolocate the server's address. That keeps all reported geo
            # fields honest nulls and avoids turning a recommended proxy into a
            # hidden network precondition of direct launch.
            logger.info("launch profile=%s connection=direct", profile.name)

        display = await self.displays.allocate()
        try:
            cdp_port = self._alloc_cdp_port()
            vnc_port = await self.displays.start(display, req.width, req.height)
            udd = Path(profile.user_data_dir)
            for lk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                (udd / lk).unlink(missing_ok=True)
            args = [
                "--disable-infobars", "--test-type", "--use-angle=swiftshader",
                f"--fingerprint={profile.fingerprint_seed}",
                f"--remote-debugging-port={cdp_port}",
                f"--fingerprint-screen-width={req.width}",
                f"--fingerprint-screen-height={req.height}",
            ]
            # license_key/browser_version come from settings, never from the env:
            # ensure_binary() resolves the version and downloads on demand into
            # CLOAKBROWSER_CACHE_DIR (the volume). We write no install logic.
            try:
                context = await launch_persistent_context_async(
                    user_data_dir=profile.user_data_dir, headless=not req.headed,
                    proxy=proxy_url, args=args, timezone=tz, locale=locale,
                    humanize=req.humanize, human_preset=req.human_preset, geoip=launch_geoip,
                    viewport=None, env={**os.environ, "DISPLAY": f":{display}"},
                    license_key=settings.cloakbrowser_license_key,
                    browser_version=settings.cloakbrowser_version or None)
            except Exception as exc:
                diagnosis = _diagnose_pin(exc, settings.cloakbrowser_version)
                if diagnosis:
                    raise PinUnavailable(diagnosis) from exc
                raise
        except BaseException:
            await self.displays.stop(display)
            raise

        now_wall, now_mono = time.time(), time.monotonic()
        inst = Instance(
            id=uuid.uuid4().hex[:12], profile=profile.name, origin=origin, owner=owner,
            subject=subject,
            context=context, display=display, cdp_port=cdp_port, vnc_port=vnc_port,
            proxy_ip=proxy_ip, timezone=tz, locale=locale,
            headed=req.headed, geoip=launch_geoip, humanize=req.humanize,
            seed=profile.fingerprint_seed, ttl_min=req.ttl_min or _HARD_TTL_MIN,
            created_wall=now_wall, created_mono=now_mono, last_used_mono=now_mono)
        context.on("close", lambda: asyncio.ensure_future(self._on_closed(inst.id)))
        return inst

    async def _on_closed(self, iid: str) -> None:
        async with self._cond:
            inst = self.running.pop(iid, None)
            self._cond.notify_all()
        if inst:
            await self.displays.stop(inst.display)
            logger.info("instance %s closed, freed :%d", iid, inst.display)

    async def stop(self, iid: str) -> bool:
        async with self._cond:
            inst = self.running.pop(iid, None)
            self._cond.notify_all()
        if not inst:
            return False
        try:
            await inst.context.close()
        except Exception as exc:
            logger.warning("close ctx %s: %s", iid, exc)
        await self.displays.stop(inst.display)
        return True

    def get(self, iid: str) -> Instance | None:
        return self.running.get(iid)

    async def reap(self) -> None:
        idle_limit = _IDLE_TTL_MIN * 60
        to_stop: list[str] = []
        for iid, inst in list(self.running.items()):
            hard = inst.age_sec() > inst.ttl_min * 60
            # idle-reap only interactive; task instances manage their own close
            idle = inst.origin == "interactive" and inst.idle_sec() > idle_limit
            if hard or idle:
                to_stop.append(iid)
        for iid in to_stop:
            logger.info("reaping %s", iid)
            await self.stop(iid)

    async def cleanup_all(self) -> None:
        for iid in list(self.running.keys()):
            await self.stop(iid)
        await self.displays.cleanup_all()
