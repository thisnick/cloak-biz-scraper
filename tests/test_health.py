"""/healthz — Railway's prober hits this unauthenticated, so watch what it says."""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_is_ok_and_public_direct_is_a_complete_mode():
    # The deploy must come up before the user has filled in a single setting;
    # otherwise Railway marks it unhealthy and they never reach the form.
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["configured"] is True
    assert body["instances"] == 0


def test_healthz_leaks_no_secret():
    with TestClient(app) as client:
        app.state.settings.update(
            cloakbrowser_license_key="cb_leakme",
            proxy_user="u", proxy_password="pw_leakme", proxy_host="h", proxy_port="1000",
        )
        response = client.get("/healthz")
    assert response.json()["configured"] is True
    assert "leakme" not in response.text


def test_healthz_does_not_treat_the_optional_proxy_as_required():
    with TestClient(app) as client:
        app.state.settings.update(
            cloakbrowser_license_key="cb_present",
            proxy_user="", proxy_password="", proxy_host="", proxy_port="",
        )
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["configured"] is True


def test_healthz_does_not_treat_a_licence_as_required():
    with TestClient(app) as client:
        app.state.settings.update(
            cloakbrowser_license_key="",
            proxy_user="", proxy_password="", proxy_host="", proxy_port="",
        )
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["configured"] is True


def test_whitespace_licence_is_public_in_health_and_startup_status(caplog):
    # Seed through the real central model before startup. This exercises both
    # the unauthenticated health payload and main.py's ready log — two surfaces
    # that previously used raw truthiness independently.
    from app.config import CONFIG
    from app.services.settings import SettingsService

    SettingsService(CONFIG.settings_path, CONFIG.dek_path).update(
        cloakbrowser_license_key=" \t\r\n ",
        proxy_user="", proxy_password="", proxy_host="", proxy_port="",
    )
    with caplog.at_level(logging.INFO, logger="cloakbiz.main"):
        with TestClient(app) as client:
            response = client.get("/healthz")
            assert app.state.settings.load().cloakbrowser_license_key == ""

    assert response.status_code == 200 and response.json()["configured"] is True
    ready = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ready:")]
    assert ready and "license=public" in ready[-1]
    assert "pro-key-saved" not in ready[-1]


def test_healthz_does_not_call_a_partial_proxy_complete():
    with TestClient(app) as client:
        app.state.settings.update(
            cloakbrowser_license_key="cb_present",
            proxy_user="started", proxy_password="", proxy_host="", proxy_port="",
        )
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["configured"] is False
