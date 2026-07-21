"""/healthz — Railway's prober hits this unauthenticated, so watch what it says."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_is_ok_with_nothing_configured():
    # The deploy must come up before the user has filled in a single setting;
    # otherwise Railway marks it unhealthy and they never reach the form.
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["configured"] is False
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


def test_healthz_does_not_call_a_partial_proxy_complete():
    with TestClient(app) as client:
        app.state.settings.update(
            cloakbrowser_license_key="cb_present",
            proxy_user="started", proxy_password="", proxy_host="", proxy_port="",
        )
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["configured"] is False
