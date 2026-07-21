"""Capabilities in URLs are useful to the caller and forbidden in logs."""
from __future__ import annotations

import logging

import httpx

from app.services.log_safety import install_log_sanitizer, redact_log_text


def test_uvicorn_vnc_upgrade_keeps_path_but_redacts_raw_and_encoded_tokens():
    raw = "header.payload.raw-signature"
    encoded = "header%2Epayload%2Eencoded-signature"
    text = (
        f'WebSocket /instances/i1/vnc?t={raw} [accepted] '
        f'WebSocket /instances/i2/vnc?mode=view&t={encoded} [accepted]'
    )

    safe = redact_log_text(text)
    assert "/instances/i1/vnc?REDACTED" in safe
    assert "/instances/i2/vnc?REDACTED" in safe
    for capability in (raw, encoded, "raw-signature", "encoded-signature"):
        assert capability not in safe


def test_signed_download_url_keeps_host_and_path_but_no_query_capability():
    signature = "raw-signature/value="
    encoded_signature = "encoded%2Fsignature%3D"
    jwt = "header.payload.synthetic-jwt"
    url = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/file"
        f"?sp=r&sig={signature}&encoded={encoded_signature}&jwt={jwt}"
    )

    safe = redact_log_text(f'HTTP Request: GET {url} "HTTP/1.1 200 OK"')
    assert "release-assets.githubusercontent.com/github-production-release-asset/1/file" in safe
    assert "?REDACTED" in safe
    for capability in (signature, encoded_signature, jwt, "sig=", "jwt="):
        assert capability not in safe


def test_absolute_proxy_urls_drop_raw_and_url_encoded_userinfo():
    raw_user = "account-name"
    raw_password = "p@ssword"
    encoded_user = "account%40example.invalid"
    encoded_password = "p%40ss%2Fword"
    text = (
        f"one=http://{raw_user}:{raw_password}@proxy.example:1000 "
        f"two=http://{encoded_user}:{encoded_password}@proxy.example:1000"
    )

    safe = redact_log_text(text)
    assert safe.count("http://***@proxy.example:1000") == 2
    for credential in (raw_user, raw_password, encoded_user, encoded_password, "%40", "%2F"):
        assert credential not in safe


def test_record_factory_covers_preconfigured_uvicorn_and_dependency_loggers(caplog):
    install_log_sanitizer()
    token = "synthetic-vnc-token.raw"
    signed = "https://release-assets.example/file?sig=encoded%2Fsig&jwt=synthetic.jwt"

    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.access").info(
            '%s - "WebSocket %s" [accepted]',
            ("127.0.0.1", 1234), f"/instances/i1/vnc?t={token}",
        )
        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"', httpx.URL(signed),
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "/instances/i1/vnc?REDACTED" in rendered
    assert "https://release-assets.example/file?REDACTED" in rendered
    for capability in (token, "encoded%2Fsig", "synthetic.jwt", "sig=", "jwt="):
        assert capability not in rendered
