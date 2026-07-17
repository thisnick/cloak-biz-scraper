"""One X display per instance, backed by KasmVNC's Xvnc.

Headed Chromium is the whole point — headless is a fingerprint — so every
instance needs a display of its own, and the profile's screen geometry has to
match the fingerprint args it launches with.

Xvnc is Xvfb's drop-in replacement: the browser draws into it exactly the same
way, and it additionally serves the framebuffer over a websocket, which is what
makes live inspection possible at all. This module used to run Xvfb and said
this was where VNC would land; this is it landing.

**Xvfb is still the fallback, and the fallback is honest about itself.** If Xvnc
is not on the PATH the pool keeps working without live view — the instance comes
up with `vnc_port=None`, and everything downstream omits `vnc_url` rather than
minting a URL that resolves to nothing. A browser you cannot watch is a
degraded product; a browser advertising a viewer that does not exist is a
support ticket.

**The websocket listens on 127.0.0.1 only.** Xvnc runs with `-SecurityTypes
None` — no VNC password — because the authentication that matters is ours, at
the proxy in routes/vnc.py, where the signed token is checked before a socket is
accepted. That is only safe while nothing outside the container can reach the
raw port: `-interface 127.0.0.1` is what keeps the unauthenticated framebuffer
unreachable, and `-rfbport -1` refuses the plain TCP VNC port entirely, so the
websocket we proxy is the only way in.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("cloakbiz.display")


@dataclass
class Display:
    number: int
    ws_port: int | None = None
    process: subprocess.Popen | None = None


class DisplayManager:
    BASE_DISPLAY = 100
    BASE_WS_PORT = 6100

    def __init__(self) -> None:
        self._allocated: dict[int, Display] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def ws_port_for(cls, number: int) -> int:
        return cls.BASE_WS_PORT + (number - cls.BASE_DISPLAY)

    async def allocate(self) -> int:
        async with self._lock:
            number = self.BASE_DISPLAY
            while number in self._allocated:
                number += 1
            self._allocated[number] = Display(number=number)
            return number

    async def start(self, number: int, width: int = 1440, height: int = 900) -> int | None:
        """Start the display. Returns its websocket port, or None under Xvfb."""
        ws_port = self.ws_port_for(number)
        xvnc = shutil.which("Xvnc")
        if xvnc:
            cmd = [
                xvnc, f":{number}",
                "-websocketPort", str(ws_port),
                # No plain VNC port at all: the websocket is the only door, and
                # it is the one our proxy guards.
                "-rfbport", "-1",
                "-geometry", f"{width}x{height}",
                "-depth", "24",
                "-SecurityTypes", "None",
                "-DisableBasicAuth",
                "-interface", "127.0.0.1",
                "-AlwaysShared",
                "-httpd", "/usr/share/kasmvnc/www",
            ]
        else:
            ws_port = None
            xvfb = shutil.which("Xvfb")
            if not xvfb:
                raise RuntimeError(
                    "Neither Xvnc nor Xvfb is installed, so a headed browser has nowhere "
                    "to draw. Both ship in the container image; run there rather than on "
                    "the host."
                )
            logger.warning(
                "Xvnc not found; falling back to Xvfb on :%d. The pool works, but these "
                "browsers cannot be watched — no vnc_url will be offered for them.", number
            )
            cmd = [xvfb, f":{number}", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"]

        log_path = f"/tmp/display-{number}.log"
        logger.info("starting %s :%d (%dx%d) ws_port=%s", cmd[0].rsplit("/", 1)[-1],
                    number, width, height, ws_port)
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        await asyncio.sleep(0.6)
        if proc.poll() is not None:
            try:
                err = open(log_path).read()
            except OSError:
                err = ""
            raise RuntimeError(f"display :{number} failed to start: {err}")
        async with self._lock:
            if number in self._allocated:
                self._allocated[number].process = proc
                self._allocated[number].ws_port = ws_port
        return ws_port

    async def stop(self, number: int) -> None:
        async with self._lock:
            display = self._allocated.pop(number, None)
        if display and display.process:
            logger.info("stopping display :%d", number)
            display.process.terminate()
            try:
                await asyncio.to_thread(display.process.wait, 5)
            except subprocess.TimeoutExpired:
                display.process.kill()

    def is_alive(self, number: int) -> bool:
        display = self._allocated.get(number)
        return bool(display and display.process and display.process.poll() is None)

    async def cleanup_all(self) -> None:
        async with self._lock:
            numbers = list(self._allocated.keys())
        for number in numbers:
            await self.stop(number)
