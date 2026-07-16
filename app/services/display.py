"""One X display per instance, backed by Xvfb.

Headed Chromium is the whole point — headless is a fingerprint — so every
instance needs a display of its own, and the profile's screen geometry has to
match the fingerprint args it launches with.

This is the seam where live inspection lands later: KasmVNC's Xvnc is a drop-in
replacement for Xvfb that additionally serves the framebuffer over a websocket.
Until then Xvfb keeps the pool headed without pulling in a VNC stack.
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
    process: subprocess.Popen | None = None


class DisplayManager:
    BASE_DISPLAY = 100

    def __init__(self) -> None:
        self._allocated: dict[int, Display] = {}
        self._lock = asyncio.Lock()

    async def allocate(self) -> int:
        async with self._lock:
            number = self.BASE_DISPLAY
            while number in self._allocated:
                number += 1
            self._allocated[number] = Display(number=number)
            return number

    async def start(self, number: int, width: int = 1440, height: int = 900) -> subprocess.Popen:
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            raise RuntimeError(
                "Xvfb is not installed, so a headed browser has nowhere to draw. "
                "It ships in the container image; run there rather than on the host."
            )
        cmd = [xvfb, f":{number}", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"]
        log_path = f"/tmp/xvfb-{number}.log"
        logger.info("starting Xvfb :%d (%dx%d)", number, width, height)
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        await asyncio.sleep(0.6)
        if proc.poll() is not None:
            try:
                err = open(log_path).read()
            except OSError:
                err = ""
            raise RuntimeError(f"Xvfb failed to start on :{number}: {err}")
        async with self._lock:
            if number in self._allocated:
                self._allocated[number].process = proc
        return proc

    async def stop(self, number: int) -> None:
        async with self._lock:
            display = self._allocated.pop(number, None)
        if display and display.process:
            logger.info("stopping Xvfb :%d", number)
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
