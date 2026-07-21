"""The Evomi proxy URL builder, and the promise that credentials never get logged."""
from __future__ import annotations

import pytest

from app.services.proxy import (
    ProxyNotConfigured,
    ProxyParts,
    build_proxy_url,
    masked,
    new_session_token,
)
from app.services.settings import Settings

FULL = Settings(
    proxy_user="user1",
    proxy_password="s3cret",
    proxy_host="core-residential.example.com",
    proxy_port="1000",
    proxy_country="US",
    proxy_region="california",
)


def test_url_carries_geo_and_session_in_the_password_suffix():
    url = build_proxy_url("ABC123XYZ", ProxyParts.from_settings(FULL))
    assert url == (
        "http://user1:s3cret_country-US_region-california_session-ABC123XYZ_lifetime-2"
        "@core-residential.example.com:1000"
    )


def test_per_profile_geo_overrides_the_default():
    url = build_proxy_url(
        "TOK", ProxyParts.from_settings(FULL), country="CA", region="ontario"
    )
    assert "_country-CA_region-ontario_" in url


def test_empty_geo_omits_the_segments_entirely():
    # An empty country/region must NOT emit a bare "_country-_region-": that is
    # not valid Evomi targeting. Each segment appears only when it has a value.
    parts = ProxyParts(
        user="u", password="pw", host="h.evomi.com", port="1000", country="", region=""
    )
    url = build_proxy_url("TOK", parts)
    assert "_country-" not in url and "_region-" not in url
    assert url == "http://u:pw_session-TOK_lifetime-2@h.evomi.com:1000"
    # Region omitted, country kept, when only one is set.
    one = build_proxy_url("TOK", ProxyParts(
        user="u", password="pw", host="h.evomi.com", port="1000", country="US", region=""
    ))
    assert "_country-US_session-" in one and "_region-" not in one


def test_session_tokens_are_unique_and_evomi_shaped():
    tokens = {new_session_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) == 9 and t.isalnum() and t.isupper() for t in tokens)


def test_missing_proxy_names_what_is_missing_rather_than_launching_bare():
    # Some proxy input means the user attempted to configure one. A partial
    # attempt must fail visibly, never become permission to launch direct.
    with pytest.raises(ProxyNotConfigured, match="proxy_password, proxy_host, proxy_port"):
        ProxyParts.from_settings(Settings(proxy_user="only-user"))


def test_optional_proxy_distinguishes_direct_from_incomplete():
    assert ProxyParts.optional_from_settings(Settings()) is None
    with pytest.raises(ProxyNotConfigured):
        ProxyParts.optional_from_settings(Settings(proxy_user="only-user"))
    assert ProxyParts.optional_from_settings(FULL) == ProxyParts.from_settings(FULL)


class TestMasked:
    def test_all_userinfo_is_hidden_and_host_kept(self):
        url = build_proxy_url("TOK", ProxyParts.from_settings(FULL))
        assert masked(url) == "http://***@core-residential.example.com:1000"

    def test_no_fragment_of_the_secret_survives(self):
        url = build_proxy_url("TOK", ProxyParts.from_settings(FULL))
        out = masked(url)
        assert "user1" not in out
        assert "s3cret" not in out
        assert "lifetime" not in out  # the whole suffix goes, not just the password

    def test_garbage_fails_closed(self):
        assert masked("this is not a url") == "***"

    def test_password_containing_an_at_sign_leaks_no_fragment(self):
        # Splitting on the first '@' would mask only "p" and leave "ss_country-US…"
        # in the log line, disguised as a hostname.
        parts = ProxyParts.from_settings(FULL.model_copy(update={"proxy_password": "p@ss"}))
        assert masked(build_proxy_url("TOK", parts)) == (
            "http://***@core-residential.example.com:1000"
        )

    def test_url_encoded_userinfo_is_redacted_as_userinfo_not_treated_as_host(self):
        parts = ProxyParts.from_settings(FULL.model_copy(update={
            "proxy_user": "account%40example.invalid",
            "proxy_password": "raw%2Fencoded%3Fcredential",
        }))
        out = masked(build_proxy_url("TOK", parts))
        assert out == "http://***@core-residential.example.com:1000"
        assert "account" not in out and "%2F" not in out
