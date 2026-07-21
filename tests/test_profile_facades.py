"""REST and MCP profile management are one safe, authenticated contract."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.profiles import ProfileStore
from app.services.settings import SettingsService

from conftest import isolate_auth, mint_access

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Origin": "https://testserver",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", "test-secret-value-long-enough")
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        isolate_auth(app, tmp_path)
        settings = SettingsService(tmp_path / "settings.json", tmp_path / ".dek")
        profiles = ProfileStore(tmp_path / "profiles")
        profiles.ensure_default(default_country="US", default_region="california")
        monkeypatch.setattr(app.state, "settings", settings)
        monkeypatch.setattr(app.state.instances, "profiles", profiles)
        monkeypatch.setattr(app.state.instances, "running", {})
        monkeypatch.setattr(app.state.instances, "_profiles_opening", {})
        c.headers["Authorization"] = f"Bearer {mint_access(app)}"
        yield c


def _mcp(client, name: str, arguments: dict | None = None) -> dict:
    response = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _mcp_value(client, name: str, arguments: dict | None = None):
    result = _mcp(client, name, arguments)
    assert result["isError"] is False, result
    structured = result["structuredContent"]
    # FastMCP wraps bare lists as {"result": [...]}, while a typed BaseModel is
    # emitted as the object itself. Exercise both real serialization shapes.
    return structured.get("result", structured)


def test_list_is_byte_equivalent_across_rest_and_mcp(client):
    rest = client.get("/api/profiles")
    assert rest.status_code == 200
    mcp = _mcp_value(client, "list_profiles")
    assert rest.json() == mcp
    assert json.dumps(rest.json(), sort_keys=True) == json.dumps(mcp, sort_keys=True)


def test_rest_create_update_and_delete_support_names_with_slashes(client):
    created = client.post(
        "/api/profiles",
        json={"name": "team/research", "country": "GB", "region": "london"},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "team/research"

    updated = client.patch(
        "/api/profiles",
        json={"name": "team/research", "new_name": "team/acquisitions", "region": "kent"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "team/acquisitions"
    assert updated.json()["country"] == "GB" and updated.json()["region"] == "kent"

    deleted = client.delete("/api/profiles", params={"name": "team/acquisitions"})
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "name": "team/acquisitions"}


def test_mcp_create_update_and_delete_use_the_same_result_models(client):
    created = _mcp_value(client, "create_profile", {"name": "research"})
    assert created["name"] == "research" and created["in_use"] is False
    updated = _mcp_value(
        client,
        "update_profile",
        {"name": "research", "new_name": "diligence", "country": "CA"},
    )
    assert updated["name"] == "diligence" and updated["country"] == "CA"
    assert _mcp_value(client, "delete_profile", {"name": "diligence"}) == {
        "ok": True,
        "name": "diligence",
    }


def test_explicit_create_collisions_and_missing_names_fail_clearly(client):
    assert client.post("/api/profiles", json={"name": "research"}).status_code == 201
    collision = client.post("/api/profiles", json={"name": "research"})
    assert collision.status_code == 409 and "already exists" in collision.text

    missing = client.patch(
        "/api/profiles", json={"name": "missing", "country": "GB"},
    )
    assert missing.status_code == 404 and "no profile" in missing.text
    mcp_missing = _mcp(client, "delete_profile", {"name": "missing"})
    assert mcp_missing["isError"] is True
    assert "no profile" in json.dumps(mcp_missing)


def test_new_proxy_session_refuses_direct_mode_without_mutation(client):
    profiles = app.state.instances.profiles
    created = profiles.get_or_create(
        "research", default_country="US", default_region="california",
    )
    before = created.session_token

    rest = client.post("/api/profiles/new-proxy-session", json={"name": "research"})
    assert rest.status_code == 409 and "direct mode" in rest.text
    assert profiles.get("research").session_token == before

    mcp = _mcp(client, "new_proxy_session", {"name": "research"})
    assert mcp["isError"] is True and "direct mode" in json.dumps(mcp)
    assert profiles.get("research").session_token == before


def test_both_facades_enforce_in_use_and_default_guards(client):
    profiles = app.state.instances.profiles
    profiles.get_or_create("source", default_country="US", default_region="california")
    app.state.instances._profiles_opening["source"] = 1
    try:
        rest = client.patch(
            "/api/profiles", json={"name": "source", "new_name": "renamed"},
        )
        assert rest.status_code == 409 and "queued, opening, open, or closing" in rest.text
        mcp = _mcp(client, "delete_profile", {"name": "source"})
        assert mcp["isError"] is True
        assert "queued, opening, open, or closing" in json.dumps(mcp)
    finally:
        app.state.instances._profiles_opening.clear()

    rest_default = client.delete("/api/profiles", params={"name": "Default"})
    assert rest_default.status_code == 400 and "cannot be deleted" in rest_default.text
    mcp_default = _mcp(client, "delete_profile", {"name": "Default"})
    assert mcp_default["isError"] is True and "cannot be deleted" in json.dumps(mcp_default)


def test_raw_payloads_and_public_schemas_never_expose_identity_secrets(client):
    profile = app.state.instances.profiles.get_or_create(
        "sentinel", default_country="US", default_region="california",
    )
    profile.session_token = "SESSION_TOKEN_SENTINEL"
    profile.fingerprint_seed = 1_987_654_321
    profile.user_data_dir = "/tmp/USER_DATA_DIR_SENTINEL"

    rest_raw = client.get("/api/profiles").content.decode()
    mcp_raw = json.dumps(_mcp(client, "list_profiles"), sort_keys=True)
    tools = client.post(
        "/mcp",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ).json()["result"]["tools"]
    profile_schemas = json.dumps(
        [
            {"input": tool.get("inputSchema"), "output": tool.get("outputSchema")}
            for tool in tools
            if tool["name"] in {
                "list_profiles", "create_profile", "update_profile",
                "new_proxy_session", "delete_profile",
            }
        ],
        sort_keys=True,
    )
    forbidden = (
        "session_token", "fingerprint_seed", "user_data_dir",
        "SESSION_TOKEN_SENTINEL", "1987654321", "USER_DATA_DIR_SENTINEL",
    )
    for raw in (rest_raw, mcp_raw, profile_schemas):
        for value in forbidden:
            assert value not in raw
