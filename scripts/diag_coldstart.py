"""Split a deployed browser cold start into browser, proxy, and client phases.

This is an invasive *diagnostic*, not a health check. Run it only in an
exclusive live window: phases C and E stop every ``agent-browser`` daemon in the
container so the next invocation is genuinely cold. That interrupts any other
agent-browser client using this deployment.

The browsers themselves are isolated from the application pool. They use
throwaway profiles under ``/tmp`` and fixed CDP ports 9501/9502, outside the
pool's 9222-9321 range. Cleanup closes the contexts created here and, if needed,
terminates only Chromium parents carrying those exact diagnostic port flags. It
never kills application-pool Chromium processes.

Railway usage, from a shell in the deployed container::

    python scripts/diag_coldstart.py --exclusive-live-window

The deployed volume must already contain ``settings.json`` and ``.dek``. The
script reads them through ``SettingsService``, exactly like the application. It
does not seed settings from environment variables and will not create a second
settings file. A working Pro licence and a complete proxy must already be saved
in the UI. ``APP_SECRET`` is irrelevant to this diagnostic.

Phases:

* A: direct browser, first navigation via raw CDP
* B: proxied browser, first navigation via raw CDP
* C: first agent-browser navigation on the browser-warm proxied instance
* D: warm agent-browser command on the same instance
* E: cold agent-browser navigation with its verbose daemon-spawn log

If A or B is slow, the delay is in Chromium/network/proxy. If C is slow while B
is not, the delay is agent-browser daemon startup. D is the steady-state floor.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# ``python scripts/...`` puts /app/scripts, not /app, first on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CONFIG, Config  # noqa: E402
from app.services.license import resolve_pro_binary  # noqa: E402
from app.services.proxy import (  # noqa: E402
    ProxyNotConfigured,
    ProxyParts,
    build_proxy_url,
    new_session_token,
)
from app.services.settings import SettingsService  # noqa: E402

BENIGN_URL = "https://example.com"
DIRECT_CDP_PORT = 9501
PROXY_CDP_PORT = 9502
APP_POOL_CDP_RANGE = range(9222, 9322)
AGENT_BROWSER_PATTERNS = (
    "agent-browser-darwin",
    "agent-browser-linux",
    "node_modules/agent-browser",
)


class DiagnosticPreflightError(RuntimeError):
    """The live deployment is not ready for an interpretable diagnostic."""


@dataclass(frozen=True)
class DiagnosticConfig:
    """Validated inputs loaded from the deployment's authoritative store."""

    license_key: str = field(repr=False)
    browser_version: str
    proxy_url: str = field(repr=False)
    settings_path: Path
    cache_dir: Path
    resolved_pro_binary: str


@dataclass(frozen=True)
class CommandResult:
    elapsed: float
    stdout: str
    stderr: str


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_diagnostic_config(
    config: Config = CONFIG,
    *,
    validate_pro: Callable[[str, str], str] = resolve_pro_binary,
    token_factory: Callable[[], str] = new_session_token,
) -> DiagnosticConfig:
    """Read and validate the existing deployment settings without seeding them.

    ``SettingsService.load()`` creates a first-boot store when none exists. A
    diagnostic must never do that: a missing mount should fail loudly rather
    than produce an empty ``settings.json`` that looks like user configuration.
    Checking both files before constructing the service also avoids creating a
    new ``.dek`` in the wrong directory.
    """
    if not config.settings_path.is_file():
        raise DiagnosticPreflightError(
            f"No existing settings store at {config.settings_path}. Run this inside the "
            "deployed application container with its /data volume mounted; the diagnostic "
            "will not create or seed a replacement settings file."
        )
    if not config.dek_path.is_file():
        raise DiagnosticPreflightError(
            f"The settings key {config.dek_path} is missing. Refusing to read or replace "
            f"{config.settings_path}; verify that the correct Railway volume is mounted."
        )

    try:
        settings = SettingsService(config.settings_path, config.dek_path).load()
    # Turn volume/decryption failures into operator guidance.
    except Exception as exc:  # noqa: BLE001
        raise DiagnosticPreflightError(
            f"Could not decrypt the existing settings at {config.settings_path}: "
            f"{type(exc).__name__}: {exc}. Verify the mounted volume before retrying."
        ) from exc

    if not settings.cloakbrowser_license_key:
        raise DiagnosticPreflightError(
            "No CloakBrowser Pro licence is saved in Settings. This diagnostic compares "
            "the previously observed Pro deployment and refuses to substitute the public build."
        )

    try:
        parts = ProxyParts.from_settings(settings)
    except ProxyNotConfigured as exc:
        if settings.proxy_present():
            detail = str(exc)
        else:
            detail = "no proxy connection fields are saved"
        raise DiagnosticPreflightError(
            f"A complete proxy is required for phases B-E ({detail}). Save and test the "
            "proxy in Settings before opening the exclusive diagnostic window."
        ) from exc
    if settings.proxy_last_check_ok is not True:
        status = (
            f"the last saved test failed ({settings.proxy_last_check_summary})"
            if settings.proxy_last_check_ok is False
            else "no successful proxy test is recorded"
        )
        raise DiagnosticPreflightError(
            f"The configured proxy is not known to be available: {status}. Use Test proxy "
            "in Settings and require a successful result before starting this comparison."
        )

    # Validate/download before any daemon is stopped. If licensing or network is
    # unavailable, ordinary client sessions remain untouched. Point validation
    # at the deployment's existing volume cache first: a Railway SSH process
    # does not inherit runtime mutations made in uvicorn's separate process.
    os.environ["CLOAKBROWSER_CACHE_DIR"] = str(config.binary_cache_dir)
    try:
        resolved = validate_pro(
            settings.cloakbrowser_license_key,
            settings.cloakbrowser_version,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the licence module's actionable text
        raise DiagnosticPreflightError(
            f"The saved CloakBrowser licence could not resolve a Pro binary: {exc}"
        ) from exc

    return DiagnosticConfig(
        license_key=settings.cloakbrowser_license_key,
        browser_version=settings.cloakbrowser_version,
        proxy_url=build_proxy_url(token_factory(), parts),
        settings_path=config.settings_path,
        cache_dir=config.binary_cache_dir,
        resolved_pro_binary=str(resolved),
    )


def browser_launch_kwargs(
    diagnostic: DiagnosticConfig,
    *,
    cdp_port: int,
    user_data_dir: Path,
    proxy_url: str | None,
) -> dict:
    """Build the exact isolated launch arguments used by phases A and B."""
    if cdp_port in APP_POOL_CDP_RANGE:
        raise ValueError(f"diagnostic CDP port {cdp_port} overlaps the application pool")
    return {
        "user_data_dir": str(user_data_dir),
        "headless": True,
        "proxy": proxy_url,
        "args": [
            "--disable-infobars",
            "--test-type",
            "--use-angle=swiftshader",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            "--fingerprint=1",
        ],
        # The diagnostic isolates connection/client startup, not geolocation or
        # humanization. These must be identical in the direct and proxy arms.
        "geoip": False,
        "humanize": False,
        "viewport": None,
        "license_key": diagnostic.license_key,
        "browser_version": diagnostic.browser_version or None,
    }


def agent_browser_command(cdp_port: int, *args: str, verbose: bool = False) -> list[str]:
    """Build an agent-browser attach command without ever launching its Chrome."""
    command = ["agent-browser"]
    if verbose:
        command.append("--verbose")
    command.extend(["--cdp", str(cdp_port), *args])
    return command


def _require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise DiagnosticPreflightError(
                f"CDP port {port} is already in use. Do not kill the owner blindly: close an "
                "earlier diagnostic cleanly or choose a new exclusive window."
            ) from exc


def preflight_runtime() -> None:
    missing = [name for name in ("agent-browser", "pkill") if shutil.which(name) is None]
    if missing:
        raise DiagnosticPreflightError(
            f"Required container tools are missing: {', '.join(missing)}. Rebuild from the "
            "project Dockerfile before running the cold-start diagnostic."
        )
    for port in (DIRECT_CDP_PORT, PROXY_CDP_PORT):
        _require_free_port(port)


async def launch_browser(
    diagnostic: DiagnosticConfig,
    *,
    cdp_port: int,
    user_data_dir: Path,
    proxy_url: str | None,
):
    from cloakbrowser import launch_persistent_context_async

    return await launch_persistent_context_async(
        **browser_launch_kwargs(
            diagnostic,
            cdp_port=cdp_port,
            user_data_dir=user_data_dir,
            proxy_url=proxy_url,
        )
    )


async def bare_cdp_navigate(cdp_port: int, url: str) -> float:
    """Measure Page.navigate -> Page.loadEventFired over a fresh raw CDP socket."""
    import httpx
    import websockets

    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{cdp_port}/json", timeout=10)
        response.raise_for_status()
        targets = response.json()
    try:
        page = next(target for target in targets if target.get("type") == "page")
    except StopIteration as exc:
        raise RuntimeError(f"CDP port {cdp_port} reported no page target") from exc

    async with websockets.connect(
        page["webSocketDebuggerUrl"], max_size=None, ping_interval=None
    ) as websocket:
        await websocket.send(json.dumps({"id": 1, "method": "Page.enable"}))
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"Page.enable failed: {message['error']}")
                break
        started = time.monotonic()
        await websocket.send(
            json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": url}})
        )
        navigation_accepted = False
        while True:
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=120))
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError(f"Page.navigate failed: {message['error']}")
                if message.get("result", {}).get("errorText"):
                    raise RuntimeError(f"Page.navigate failed: {message['result']['errorText']}")
                navigation_accepted = True
            if navigation_accepted and message.get("method") == "Page.loadEventFired":
                return time.monotonic() - started


def stop_agent_browser_daemons(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """Stop all agent-browser daemons; safe only in an exclusive live window."""
    for pattern in AGENT_BROWSER_PATTERNS:
        result = run(["pkill", "-f", pattern], capture_output=True, check=False)
        # pkill uses 1 for "no matching process", which is the desired state.
        if result.returncode not in (0, 1):
            raise RuntimeError(
                f"pkill could not stop agent-browser pattern {pattern!r} "
                f"(exit {result.returncode}); the next phase would not be cold"
            )
    pause(1)


def run_agent_browser(command: Sequence[str], *, timeout: int = 120) -> CommandResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{' '.join(command)} exceeded {timeout}s. stdout={exc.stdout!r} stderr={exc.stderr!r}"
        ) from exc
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} exited {result.returncode}. "
            f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )
    return CommandResult(elapsed, result.stdout.strip(), result.stderr.strip())


def diagnostic_browser_pids(ports: Sequence[int], proc_root: Path = Path("/proc")) -> set[int]:
    """Find only Chromium parents carrying one of our exact CDP-port flags."""
    if not proc_root.is_dir():
        return set()
    flags = {f"--remote-debugging-port={port}".encode() for port in ports}
    found: set[int] = set()
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            arguments = set((process / "cmdline").read_bytes().split(b"\0"))
        except OSError:
            continue
        if arguments & flags:
            found.add(int(process.name))
    return found


def stop_diagnostic_browsers(ports: Sequence[int]) -> None:
    """Terminate leftovers on our isolated ports, never pool-port browsers."""
    pids = diagnostic_browser_pids(ports)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(0.5)
    for pid in diagnostic_browser_pids(ports):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def close_context(context, label: str, cdp_port: int) -> None:
    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=10)
        except Exception as exc:  # noqa: BLE001 - cleanup must continue to exact-port fallback
            log(f"WARNING: could not close the {label} diagnostic context cleanly: {exc}")
    # Preflight proved these ports were empty before this run. Exact-port process
    # cleanup therefore targets only browsers this harness started, and cannot
    # overlap the application's 9222-9321 pool.
    stop_diagnostic_browsers((cdp_port,))


async def run_diagnostic(diagnostic: DiagnosticConfig, url: str) -> None:
    direct_context = None
    proxy_context = None
    daemon_was_stopped = False

    # Explicitly point downloads at the application's existing volume cache.
    # This is the only binary-cache variable the package needs; licence and pin
    # remain arguments loaded from Settings rather than env fallbacks.
    diagnostic.cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CLOAKBROWSER_CACHE_DIR"] = str(diagnostic.cache_dir)

    log("=== cold-start split diagnostic — throwaway instances, ports 9501/9502 ===")
    log(f"settings: {diagnostic.settings_path}; validated Pro: {diagnostic.resolved_pro_binary}")
    log("WARNING: agent-browser daemon stops below interrupt every attached client")

    with tempfile.TemporaryDirectory(prefix="cloakbiz-diag-direct-") as direct_dir, \
            tempfile.TemporaryDirectory(prefix="cloakbiz-diag-proxy-") as proxy_dir:
        try:
            direct_context = await launch_browser(
                diagnostic,
                cdp_port=DIRECT_CDP_PORT,
                user_data_dir=Path(direct_dir),
                proxy_url=None,
            )
            direct_elapsed = await bare_cdp_navigate(DIRECT_CDP_PORT, url)
            log(f"A  bare CDP navigate, NO proxy     : {direct_elapsed:6.2f}s")
            await close_context(direct_context, "direct", DIRECT_CDP_PORT)
            direct_context = None

            try:
                proxy_context = await launch_browser(
                    diagnostic,
                    cdp_port=PROXY_CDP_PORT,
                    user_data_dir=Path(proxy_dir),
                    proxy_url=diagnostic.proxy_url,
                )
                proxy_elapsed = await bare_cdp_navigate(PROXY_CDP_PORT, url)
            except Exception as exc:
                raise RuntimeError(
                    "Phase B could not launch or navigate through the saved proxy. Run "
                    f"Test proxy in Settings immediately before retrying. Underlying error: {exc}"
                ) from exc
            log(
                f"B  bare CDP navigate, WITH proxy   : {proxy_elapsed:6.2f}s   "
                f"(B-A = {proxy_elapsed - direct_elapsed:+.2f}s)"
            )

            stop_agent_browser_daemons()
            daemon_was_stopped = True
            result = run_agent_browser(
                agent_browser_command(PROXY_CDP_PORT, "navigate", url)
            )
            log(
                f"C  agent-browser navigate (cold ab): {result.elapsed:6.2f}s   "
                "(browser warm -> daemon cold-start)"
            )

            result = run_agent_browser(
                agent_browser_command(PROXY_CDP_PORT, "get", "url")
            )
            log(
                f"D  agent-browser get url (warm)    : {result.elapsed:6.2f}s   "
                "(steady state)"
            )

            stop_agent_browser_daemons()
            result = run_agent_browser(
                agent_browser_command(
                    PROXY_CDP_PORT, "navigate", url, verbose=True
                )
            )
            log(f"E  agent-browser --verbose (cold)  : {result.elapsed:6.2f}s")
            log("E  --verbose output (last 40 lines of daemon-spawn detail):")
            verbose_output = result.stderr or result.stdout
            for line in verbose_output.splitlines()[-40:]:
                print(f"     {line}", flush=True)
        finally:
            await close_context(direct_context, "direct", DIRECT_CDP_PORT)
            await close_context(proxy_context, "proxy", PROXY_CDP_PORT)
            if daemon_was_stopped:
                stop_agent_browser_daemons()

    log("=== done; throwaway instances closed ===")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Localize deployed browser cold-start time without using the app pool.",
        epilog=(
            "Requires the existing /data settings volume, a validated Pro licence, and a "
            "successfully tested proxy. WARNING: this stops all agent-browser daemons in "
            "the container during phases C and E."
        ),
    )
    parser.add_argument("--url", default=BENIGN_URL, help="benign URL used for every phase")
    parser.add_argument(
        "--exclusive-live-window",
        action="store_true",
        help="acknowledge that this run stops all agent-browser daemons in the container",
    )
    args = parser.parse_args(argv)
    if not args.exclusive_live_window:
        parser.error(
            "refusing to stop shared agent-browser daemons without --exclusive-live-window; "
            "coordinate a window with no connector/browser clients first"
        )
    return args


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        preflight_runtime()
        diagnostic = load_diagnostic_config()
        await run_diagnostic(diagnostic, args.url)
    except DiagnosticPreflightError as exc:
        print(f"PRE-FLIGHT FAILED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; owned diagnostic contexts were asked to close.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - command-line harness needs a concise final failure
        print(f"DIAGNOSTIC FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
