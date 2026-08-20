from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.web import create_web_app


def _client(config_file: Path, *, require_auth: bool = False) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    settings.web.require_auth = require_auth
    return TestClient(create_web_app(settings))


def test_core_api_rejects_non_loopback_host(config_file: Path) -> None:
    client = _client(config_file)
    response = client.get("/api/simple-status", headers={"host": "example.invalid"})
    assert response.status_code == 400
    assert response.json()["detail"] == "loopback host required"


def test_core_api_rejects_untrusted_origin(config_file: Path) -> None:
    client = _client(config_file)
    response = client.get("/api/simple-status", headers={"origin": "https://example.invalid"})
    assert response.status_code == 403
    assert response.json()["detail"] == "cross-origin request rejected"


def test_auth_gate_and_csrf_protect_core_settings(config_file: Path) -> None:
    client = _client(config_file, require_auth=True)
    assert client.get("/api/simple-status").status_code == 401

    csrf = client.get("/").cookies["oa_csrf"]
    token = client.app.state.bootstrap_token
    assert client.post("/api/auth/login", json={"token": token}, headers={"x-csrf-token": csrf}).status_code == 204
    assert client.get("/api/simple-status").status_code == 200
    assert client.patch("/api/settings", json={}).status_code == 403

    assert client.patch("/api/settings", json={}, headers={"x-csrf-token": csrf}).status_code != 403


def test_unknown_api_path_is_json_and_spa_path_is_html(config_file: Path) -> None:
    client = _client(config_file)
    api_response = client.get("/api/not-a-route")
    assert api_response.status_code == 404
    assert api_response.headers["content-type"].startswith("application/json")

    spa_response = client.get("/some-client-route")
    assert spa_response.status_code == 200
    assert spa_response.headers["content-type"].startswith("text/html")
