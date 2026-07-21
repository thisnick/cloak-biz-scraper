"""server_info — a status snapshot that must never carry a secret."""
from __future__ import annotations

import pytest
from conftest import isolate_auth, mint_access
from fastapi.testclient import TestClient

from app.main import app
from app.services.settings import Settings
from app.services.views import server_info

SECRET = "test-secret-value-long-enough"

LICENSE = "LICENSE-SECRET-abc123"
PROXY_PW = "PROXY-PASSWORD-xyz789"
NOTION_TOK = "NOTION-TOKEN-qwerty"
PRO_PATH = "/data/.cloakbrowser/chromium-148.0.7778.215.2-pro/chrome"
PUBLIC_PATH = "/data/.cloakbrowser/chromium-146.0.7680.177.3/chrome"


def _settings() -> Settings:
    return Settings(
        cloakbrowser_license_key=LICENSE,
        cloakbrowser_version="148.0.7778.215.2",
        proxy_user="u", proxy_password=PROXY_PW, proxy_host="h", proxy_port="1000",
        proxy_country="US", proxy_region="california",
        notion_api_token=NOTION_TOK, notion_db_id="db-1",
        max_instances=4, interactive_reserve=1,
    )


class _FakeInstances:
    def __init__(self, in_use=2, binary_path=None):
        self._in_use = in_use
        self._binary_path = binary_path

    def counts(self):
        return {"task": 1, "interactive": 1, "total": self._in_use,
                "max": 4, "task_budget": 3, "reserve": 1}

    def binary_path_for(self, settings):
        return self._binary_path


class TestNoSecretLeaks:
    """The crux: no proxy password, licence key, or Notion token — in any field,
    at any depth — ever appears in the serialized snapshot."""

    def test_the_serialized_snapshot_contains_no_secret_value(self):
        info = server_info(_settings(), _FakeInstances(binary_path=PRO_PATH))
        blob = info.model_dump_json()
        for secret in (LICENSE, PROXY_PW, NOTION_TOK):
            assert secret not in blob, f"a secret leaked into server_info: {secret!r}"

    def test_status_is_reported_without_the_secret_that_produced_it(self):
        info = server_info(_settings(), _FakeInstances(binary_path=PRO_PATH))
        # Pro is TRUE because the resolved path says so, not because a secret is
        # merely present; the secrets themselves remain absent (checked above).
        assert info.proxy.configured is True
        assert info.browser.pro is True
        assert info.browser.build == "pro"
        assert info.notion.connected is True


class TestSnapshotContent:
    def test_proxy_status_and_location(self):
        info = server_info(_settings(), _FakeInstances())
        assert info.proxy.status == "untested"  # configured, never checked
        assert info.proxy.country == "US" and info.proxy.region == "california"

    def test_pool_counts_come_from_the_manager(self):
        info = server_info(_settings(), _FakeInstances(in_use=3))
        assert info.pool.max == 4 and info.pool.reserved == 1 and info.pool.in_use == 3

    def test_an_unconfigured_server_reads_as_such(self):
        info = server_info(Settings(), _FakeInstances(in_use=0))
        assert info.proxy.configured is False and info.proxy.status == "direct"
        assert info.proxy.country is None and info.proxy.region is None
        assert info.browser.pro is False
        assert info.browser.build == "public"
        assert info.notion.connected is False

    def test_a_saved_key_is_not_called_pro_before_its_artifact_resolves(self):
        info = server_info(_settings(), _FakeInstances(binary_path=None))
        assert info.browser.pro is None
        assert info.browser.build == "pro-unverified"

    def test_a_resolved_public_path_is_labelled_public(self):
        info = server_info(Settings(), _FakeInstances(binary_path=PUBLIC_PATH))
        assert info.browser.pro is False
        assert info.browser.build == "public"
        assert info.browser.version == "146.0.7680.177.3"

    def test_partial_proxy_is_not_misreported_as_direct_or_located(self):
        info = server_info(Settings(proxy_user="u"), _FakeInstances(in_use=0))
        assert info.proxy.configured is False and info.proxy.status == "incomplete"
        assert info.proxy.country is None and info.proxy.region is None


class TestRestEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APP_SECRET", SECRET)
        monkeypatch.delenv("APP_SECRET_RESET", raising=False)
        with TestClient(app, base_url="https://testserver") as c:
            isolate_auth(app, tmp_path)
            yield c

    def test_authed_get_returns_the_snapshot(self, client):
        r = client.get("/api/server-info", headers={"Authorization": f"Bearer {mint_access(app)}"})
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"proxy", "browser", "pool", "notion"}
        assert "windows_fonts" in body["browser"]

    def test_no_token_is_refused(self, client):
        assert client.get("/api/server-info").status_code == 401
