"""Standalone API boundary tests; never contact Telegram or Google."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.api import create_app
from dashboard import operations

ORIGIN = "https://dashboard.example.com"
PASSWORD = "a-strong-test-password-2026"


@pytest.fixture
def client():
    with patch.dict(os.environ, {"HYPERDASH_ALLOWED_ORIGIN": ORIGIN, "HYPERDASH_PASSWORD": PASSWORD}):
        with TestClient(create_app()) as c:
            yield c


def authenticate(client):
    result = client.post("/api/v1/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    return {"Authorization": "Bearer " + result.json()["access_token"], "Origin": ORIGIN}


def test_admin_endpoints_require_login(client):
    assert client.get("/api/v1/dashboard").status_code == 401
    assert client.post("/api/v1/files/reconcile", json={}).status_code == 401
    assert client.post("/api/v1/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/v1/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_cors_and_logout(client):
    headers = authenticate(client)
    res = client.options("/api/v1/dashboard", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "Authorization"})
    assert res.headers["access-control-allow-origin"] == ORIGIN
    assert client.post("/api/v1/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/dashboard", headers=headers).status_code == 401


def test_dashboard_excludes_vault_tokens(client):
    headers = authenticate(client)
    data = {"today": "2026-11-25", "candidates": [], "entries": [], "records": [
        SimpleNamespace(title="Example", website_id="example", active=True, public_token="SECRET_TOKEN", source_channel_id=-123)
    ], "shifts": [], "history": [], "shift_sheet_enabled": True, "shift_sheet_url": "",
    "group_configured": True, "sheet_error": None, "shift_sheet_error": None}
    with patch.object(operations, "service_context", return_value=(None, None, None, None)), patch.object(operations, "snapshot", return_value=data):
        response = client.get("/api/v1/dashboard", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "SECRET_TOKEN" not in response.text and "source_channel_id" not in response.text


def test_file_link_requires_row_version(client):
    headers = authenticate(client)
    payload = {"candidate_id": 1, "sheet": "softwares", "row_number": 2}
    assert client.post("/api/v1/files/link", json=payload, headers=headers).status_code == 422
    with patch.object(operations, "service_context", return_value=(None, None, None, None)), patch.object(operations, "link_candidate", return_value={"synchronized": True}) as fn:
        response = client.post("/api/v1/files/link", json={**payload, "row_version": "a"*64}, headers=headers)
        assert response.status_code == 200
        assert fn.call_args.args[-1] == "a"*64


def test_api_disabled_without_allowed_origin():
    with patch.dict(os.environ, {"HYPERDASH_ALLOWED_ORIGIN": "*", "HYPERDASH_PASSWORD": PASSWORD}):
        with pytest.raises(RuntimeError):
            create_app()


def test_invalid_password_payload_is_not_echoed(client):
    password = "sensitive-" + "x" * 600
    response = client.post("/api/v1/login", json={"password": password})
    assert response.status_code == 422
    assert password not in response.text
    assert response.json()["detail"] == "Invalid request payload"


def test_catalog_add_requires_auth_and_valid_payload(client):
    payload = {"sheet": "softwares", "request_id": "a"*32, "title": "New app"}
    assert client.post("/api/v1/catalog/add", json=payload).status_code == 401
    headers = authenticate(client)
    assert client.post("/api/v1/catalog/add", json={**payload, "sheet": "Requests"}, headers=headers).status_code == 422
    with patch.object(operations, "service_context", return_value=(None, None, None, None)), patch.object(operations, "add_catalog_entry", return_value={"created": True}) as fn:
        response = client.post("/api/v1/catalog/add", json=payload, headers=headers)
        assert response.status_code == 200
        assert fn.call_args.args[3:6] == ("softwares", "a"*32, "New app")


def test_catalog_add_normalizes_vault_source_and_does_not_register():
    from unittest.mock import Mock
    settings = SimpleNamespace(source_chat_id=-10012345, source_chat_username="Hyper_Vault")
    sheets = Mock()
    sheets.append_catalog_only.return_value = SimpleNamespace(file_id="sw_"+"a"*32, telegram_status="pending_review")
    repo = Mock()
    result = operations.add_catalog_entry(settings, repo, sheets, "softwares", "a"*32,
                                          "Test", source="https://t.me/Hyper_Vault/12/34?single")
    assert result["status"] == "pending_review"
    assert sheets.append_catalog_only.call_args.kwargs["source_url"] == "https://t.me/Hyper_Vault/12/34"
    assert not repo.mock_calls
    with pytest.raises(ValueError):
        operations.add_catalog_entry(settings, repo, sheets, "softwares", "b"*32,
                                     "Wrong source", source="https://t.me/OtherVault/12/34")
