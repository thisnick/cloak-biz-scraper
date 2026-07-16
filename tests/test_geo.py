"""Carry-forward #1: measure the proxy, or say you couldn't. Never both."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.services import geo
from app.services.geo import ProxyProbe, ProxyUnreachable, measure_exit_ip, probe

PROXY = "http://user:pw@proxy.example.com:1000"


class TestMeasuresTheExit:
    @respx.mock
    @pytest.mark.asyncio
    async def test_reads_the_ip_back_through_the_tunnel(self):
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        assert await measure_exit_ip(PROXY) == "45.12.3.4"

    @respx.mock
    @pytest.mark.asyncio
    async def test_falls_through_to_the_second_echo(self):
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("down"))
        respx.get("https://checkip.amazonaws.com").mock(
            return_value=httpx.Response(200, text="45.12.3.4\n")
        )
        assert await measure_exit_ip(PROXY) == "45.12.3.4"


class TestFailsFastRatherThanGuessing:
    @respx.mock
    @pytest.mark.asyncio
    async def test_unroutable_proxy_raises(self):
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ProxyUnreachable):
            await measure_exit_ip(PROXY)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_fallback_to_the_gateway_address(self):
        """The subtle half of the bug.

        cloakbrowser's resolver falls back to DNS-resolving the proxy *hostname*
        when the echo fails, which returns the gateway's address — a real IP,
        from a real lookup, that is not the exit and may be a continent from it.
        A proxy whose host resolves perfectly but cannot route must still be an
        error here, never a plausible-looking answer.
        """
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        # localhost resolves instantly, so a hostname fallback would "succeed".
        with pytest.raises(ProxyUnreachable):
            await measure_exit_ip("http://user:pw@localhost:1")

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_proxy_error_page_is_not_an_ip(self):
        # A misconfigured proxy answers 200 with HTML. Parsing that as an exit IP
        # would be the same class of bug: a value that was never measured.
        respx.get("https://api.ipify.org").mock(
            return_value=httpx.Response(200, text="<html>407 auth required</html>")
        )
        respx.get("https://checkip.amazonaws.com").mock(
            return_value=httpx.Response(200, text="not an ip")
        )
        with pytest.raises(ProxyUnreachable):
            await measure_exit_ip(PROXY)

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_error_says_what_it_does_not_know(self):
        respx.get("https://api.ipify.org").mock(side_effect=httpx.ConnectError("refused"))
        respx.get("https://checkip.amazonaws.com").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ProxyUnreachable, match="exit IP is unknown"):
            await measure_exit_ip(PROXY)


class TestUnknownIsNotADefault:
    @respx.mock
    @pytest.mark.asyncio
    async def test_ungeolocatable_ip_reports_none_not_a_fallback(self, monkeypatch):
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        monkeypatch.setattr(geo, "_geolocate", lambda ip: (None, None, None, None))
        measured = await probe(PROXY)
        assert measured.exit_ip == "45.12.3.4"
        assert measured.timezone is None, "an unmeasured timezone must never be substituted"
        assert measured.geo_resolved is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_geo_can_be_skipped_entirely(self):
        respx.get("https://api.ipify.org").mock(return_value=httpx.Response(200, text="45.12.3.4"))
        measured = await probe(PROXY, geo=False)
        assert (measured.exit_ip, measured.timezone) == ("45.12.3.4", None)

    def test_describe_says_unknown_out_loud(self):
        text = ProxyProbe(exit_ip="45.12.3.4").describe()
        assert "unknown" in text
        assert "America/Los_Angeles" not in text

    def test_describe_reports_what_was_measured(self):
        text = ProxyProbe(
            exit_ip="45.12.3.4", timezone="America/Los_Angeles", country="US", city="San Jose"
        ).describe()
        assert "45.12.3.4" in text and "San Jose" in text and "America/Los_Angeles" in text


def test_no_timezone_constant_exists_to_fall_back_to():
    """The regression guard for carry-forward #1.

    Asserted against the source because the bug was not a wrong branch — it was a
    constant sitting there waiting to be used. `tz = tz or _FALLBACK_TZ` reads as
    harmless defensive code, which is how it survived review the first time. The
    durable fix is that there is no such constant to reach for, so this looks for
    the value rather than for any particular way of using it.

    Scoped to string constants in code via the AST: docstrings and comments are
    where we *explain* the bug, and prose about a fabricated timezone is not a
    fabricated timezone.
    """
    import ast

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders: list[str] = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "America/Los_Angeles" in node.value
                and node.value not in docstrings
            ):
                offenders.append(f"{path.relative_to(app_dir).as_posix()}:{node.lineno}")

    assert offenders == [], (
        f"a hardcoded timezone is back at {offenders}; report a measured value or None"
    )


def test_the_package_internals_we_depend_on_still_exist():
    """We reuse cloakbrowser's cached GeoLite2 DB rather than downloading a
    second 70 MB copy, which means depending on two private names. Fail here, in
    CI, rather than in front of a user after a package upgrade moves them.
    """
    from cloakbrowser.geoip import COUNTRY_LOCALE_MAP, _ensure_geoip_db  # noqa: F401

    assert COUNTRY_LOCALE_MAP.get("US") == "en-US"
    assert callable(_ensure_geoip_db)
