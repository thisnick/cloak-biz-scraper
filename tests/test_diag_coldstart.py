"""Cold-start harness preflight and command construction — no live processes."""
from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from app.config import Config
from app.services.settings import SettingsService
from scripts import diag_coldstart as diag_script
from scripts.diag_coldstart import (
    DIRECT_CDP_PORT,
    PROXY_CDP_PORT,
    AGENT_BROWSER_PATTERNS,
    DiagnosticPreflightError,
    agent_browser_command,
    browser_launch_kwargs,
    diagnostic_browser_pids,
    load_diagnostic_config,
    parse_args,
    stop_agent_browser_daemons,
)


@pytest.fixture(autouse=True)
def restore_binary_cache_env(monkeypatch):
    """The harness deliberately sets this process-level package input."""
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", "test-original-cache")


def configured_volume(tmp_path: Path, **changes) -> Config:
    config = Config(data_dir=tmp_path, port=8000)
    values = {
        "cloakbrowser_license_key": "saved-pro-key",
        "cloakbrowser_version": "148.0.7778.215.5",
        "proxy_user": "proxy-user",
        "proxy_password": "proxy-secret",
        "proxy_host": "proxy.example",
        "proxy_port": "1000",
        "proxy_country": "US",
        "proxy_region": "california",
        "proxy_last_check_ok": True,
        "proxy_last_check_summary": "203.0.113.10 — California, US",
        **changes,
    }
    SettingsService(config.settings_path, config.dek_path).update(**values)
    return config


def test_loads_authoritative_volume_settings_and_builds_one_sticky_proxy(tmp_path):
    config = configured_volume(tmp_path)
    validation_calls = []
    resolved_pro = "/data/.cloakbrowser/chromium-148.0.7778.215.5-pro/chrome"

    def validate(key: str, pin: str) -> str:
        validation_calls.append((key, pin))
        return resolved_pro

    diagnostic = load_diagnostic_config(
        config,
        resolve_binary=validate,
        token_factory=lambda: "FIXEDTOK",
    )

    assert validation_calls == [("saved-pro-key", "148.0.7778.215.5")]
    assert diagnostic.settings_path == tmp_path / "settings.json"
    assert diagnostic.cache_dir == tmp_path / ".cloakbrowser"
    assert diagnostic.resolved_pro_binary == resolved_pro
    assert os.environ["CLOAKBROWSER_CACHE_DIR"] == str(diagnostic.cache_dir)
    assert diagnostic.proxy_url == (
        "http://proxy-user:proxy-secret_country-US_region-california_"
        "session-FIXEDTOK_lifetime-2@proxy.example:1000"
    )
    assert not (tmp_path / "s.json").exists()
    assert not diagnostic.cache_dir.exists(), "loading settings must remain read-only"


def test_missing_settings_fails_without_creating_a_store_or_dek(tmp_path):
    config = Config(data_dir=tmp_path, port=8000)

    with pytest.raises(DiagnosticPreflightError, match="No existing settings store"):
        load_diagnostic_config(config, resolve_binary=lambda _key, _pin: "unused")

    assert list(tmp_path.iterdir()) == []


def test_missing_dek_does_not_replace_or_touch_existing_ciphertext(tmp_path):
    config = Config(data_dir=tmp_path, port=8000)
    ciphertext = b"existing encrypted settings"
    config.settings_path.write_bytes(ciphertext)

    with pytest.raises(DiagnosticPreflightError, match="settings key"):
        load_diagnostic_config(config, resolve_binary=lambda _key, _pin: "unused")

    assert config.settings_path.read_bytes() == ciphertext
    assert not config.dek_path.exists()


def test_missing_pro_key_fails_before_validation_or_daemon_work(tmp_path):
    config = configured_volume(tmp_path, cloakbrowser_license_key="")
    called = False

    def must_not_validate(_key: str, _pin: str) -> str:
        nonlocal called
        called = True
        return "unused"

    with pytest.raises(DiagnosticPreflightError, match="No CloakBrowser Pro licence"):
        load_diagnostic_config(config, resolve_binary=must_not_validate)

    assert not called


def test_missing_proxy_fails_actionably_before_pro_validation(tmp_path):
    config = configured_volume(
        tmp_path,
        proxy_user="",
        proxy_password="",
        proxy_host="",
        proxy_port="",
    )
    called = False

    def must_not_validate(_key: str, _pin: str) -> str:
        nonlocal called
        called = True
        return "unused"

    with pytest.raises(DiagnosticPreflightError, match="complete proxy.*no proxy"):
        load_diagnostic_config(config, resolve_binary=must_not_validate)

    assert not called


def test_invalid_or_unavailable_pro_resolution_keeps_original_guidance(tmp_path):
    config = configured_volume(tmp_path)

    def fail(_key: str, _pin: str) -> str:
        raise RuntimeError("licensing server unavailable and no cached validation")

    with pytest.raises(
        DiagnosticPreflightError,
        match="could not resolve a Pro binary.*licensing server unavailable",
    ):
        load_diagnostic_config(config, resolve_binary=fail)


def test_whitespace_key_cannot_select_the_public_build(tmp_path):
    config = configured_volume(tmp_path, cloakbrowser_license_key="  \t  ")
    calls = []

    with pytest.raises(DiagnosticPreflightError, match="No CloakBrowser Pro licence"):
        load_diagnostic_config(
            config,
            resolve_binary=lambda key, pin: calls.append((key, pin)) or "unused",
        )

    assert calls == [], "blank public mode must be rejected before binary resolution"


def test_resolved_public_artifact_is_rejected_without_fallback(tmp_path):
    config = configured_volume(tmp_path)
    public = "/data/.cloakbrowser/chromium-146.0.7680.177.3/chrome"

    with pytest.raises(
        DiagnosticPreflightError,
        match="resolved the public/non-Pro build.*Refusing public fallback",
    ):
        load_diagnostic_config(
            config,
            resolve_binary=lambda _key, _pin: public,
        )


def test_untested_or_failed_proxy_is_not_accepted_as_available(tmp_path):
    untested = configured_volume(tmp_path / "untested", proxy_last_check_ok=None)
    with pytest.raises(DiagnosticPreflightError, match="no successful proxy test"):
        load_diagnostic_config(untested, resolve_binary=lambda _key, _pin: "unused")

    failed = configured_volume(
        tmp_path / "failed",
        proxy_last_check_ok=False,
        proxy_last_check_summary="407 rejected",
    )
    with pytest.raises(DiagnosticPreflightError, match="last saved test failed.*407"):
        load_diagnostic_config(failed, resolve_binary=lambda _key, _pin: "unused")


def test_launch_arguments_keep_direct_and_proxy_arms_identical_except_proxy(tmp_path):
    diagnostic = load_diagnostic_config(
        configured_volume(tmp_path),
        resolve_binary=lambda _key, _pin: "/cache/chromium-pro/chrome",
        token_factory=lambda: "TOK",
    )
    direct = browser_launch_kwargs(
        diagnostic,
        cdp_port=DIRECT_CDP_PORT,
        user_data_dir=tmp_path / "direct",
        proxy_url=None,
    )
    proxied = browser_launch_kwargs(
        diagnostic,
        cdp_port=PROXY_CDP_PORT,
        user_data_dir=tmp_path / "proxy",
        proxy_url=diagnostic.proxy_url,
    )

    assert direct["proxy"] is None
    assert proxied["proxy"] == diagnostic.proxy_url
    assert direct["geoip"] is proxied["geoip"] is False
    assert direct["humanize"] is proxied["humanize"] is False
    assert direct["license_key"] == proxied["license_key"] == "saved-pro-key"
    assert direct["browser_version"] == proxied["browser_version"]
    assert "--remote-debugging-port=9501" in direct["args"]
    assert "--remote-debugging-port=9502" in proxied["args"]
    assert str(direct["user_data_dir"]).startswith(str(tmp_path))
    assert str(proxied["user_data_dir"]).startswith(str(tmp_path))


def test_launch_argument_builder_rejects_application_pool_ports(tmp_path):
    diagnostic = load_diagnostic_config(
        configured_volume(tmp_path),
        resolve_binary=lambda _key, _pin: "/cache/chromium-pro/chrome",
    )
    with pytest.raises(ValueError, match="overlaps the application pool"):
        browser_launch_kwargs(
            diagnostic,
            cdp_port=9222,
            user_data_dir=tmp_path / "bad",
            proxy_url=None,
        )


@pytest.mark.asyncio
async def test_proxy_launch_error_omits_raw_and_encoded_credentials(tmp_path, monkeypatch):
    diagnostic = load_diagnostic_config(
        configured_volume(tmp_path),
        resolve_binary=lambda _key, _pin: "/cache/chromium-pro/chrome",
        token_factory=lambda: "STICKY123",
    )
    credentials = diagnostic.proxy_url.split("://", 1)[1].rsplit("@", 1)[0]
    encoded_url = quote(diagnostic.proxy_url, safe="")
    encoded_credentials = quote(credentials, safe="")
    plus_encoded_url = quote_plus(diagnostic.proxy_url, safe="")
    source_text = " | ".join(
        (diagnostic.proxy_url, encoded_url, encoded_credentials, plus_encoded_url)
    )

    class FakeContext:
        async def close(self):
            return None

    async def fake_launch(_diagnostic, *, proxy_url, **_kwargs):
        if proxy_url is None:
            return FakeContext()
        raise RuntimeError(source_text)

    async def fake_navigate(_port, _url):
        return 0.01

    monkeypatch.setattr(diag_script, "launch_browser", fake_launch)
    monkeypatch.setattr(diag_script, "bare_cdp_navigate", fake_navigate)
    monkeypatch.setattr(diag_script, "stop_diagnostic_browsers", lambda _ports: None)

    with pytest.raises(RuntimeError) as failure_info:
        await diag_script.run_diagnostic(diagnostic, "https://example.com")

    failure = failure_info.value
    rendered = "".join(traceback.format_exception(failure))

    assert "Test proxy in Settings" in rendered
    assert "Underlying error type: RuntimeError" in rendered
    assert failure.__context__ is None, "the secret-bearing source exception must be detached"
    for secret_form in (
        diagnostic.proxy_url,
        encoded_url,
        credentials,
        encoded_credentials,
        plus_encoded_url,
        "proxy-secret",
        "STICKY123",
    ):
        assert secret_form not in rendered


def test_agent_browser_commands_attach_only_to_the_diagnostic_port():
    assert agent_browser_command(PROXY_CDP_PORT, "get", "url") == [
        "agent-browser", "--cdp", "9502", "get", "url"
    ]
    assert agent_browser_command(
        PROXY_CDP_PORT, "navigate", "https://example.com", verbose=True
    ) == [
        "agent-browser", "--verbose", "--cdp", "9502", "navigate",
        "https://example.com",
    ]


def test_daemon_stop_uses_explicit_argument_lists_and_accepts_no_match():
    calls = []
    pauses = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1)

    stop_agent_browser_daemons(run=run, pause=pauses.append)

    assert [call[0] for call in calls] == [
        ["pkill", "-f", pattern] for pattern in AGENT_BROWSER_PATTERNS
    ]
    assert all(call[1] == {"capture_output": True, "check": False} for call in calls)
    assert pauses == [1]


def test_process_cleanup_matcher_never_selects_an_application_pool_browser(tmp_path):
    proc = tmp_path / "proc"
    for pid, arguments in {
        101: ["/cache/chrome", "--remote-debugging-port=9501"],
        102: ["/cache/chrome", "--remote-debugging-port=9502"],
        103: ["/cache/chrome", "--remote-debugging-port=9222"],
        104: ["python", "scripts/diag_coldstart.py"],
    }.items():
        process = proc / str(pid)
        process.mkdir(parents=True)
        (process / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments))

    assert diagnostic_browser_pids((9501, 9502), proc_root=proc) == {101, 102}


def test_exclusive_window_acknowledgement_is_mandatory():
    with pytest.raises(SystemExit) as exc:
        parse_args([])
    assert exc.value.code == 2

    args = parse_args(["--exclusive-live-window"])
    assert args.exclusive_live_window
    assert args.url == "https://example.com"
