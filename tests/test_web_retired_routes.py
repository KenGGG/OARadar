from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.web import create_web_app


@pytest.fixture
def client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings))


@pytest.mark.parametrize("path", [
    "/api/audits/online",
    "/api/governance/inventory",
    "/api/reviews",
    "/api/batches",
    "/api/backfill/status",
    "/api/maintenance",
    "/api/lifecycle/knowledge",
    "/api/lifecycle/processing-center",
])
def test_retired_v2_routes_return_json_404(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "API route not found"


def test_retired_v2_routes_are_not_registered(client: TestClient) -> None:
    retired_prefixes = (
        "/api/audits",
        "/api/governance",
        "/api/reviews",
        "/api/batches",
        "/api/backfill",
        "/api/lifecycle/knowledge",
        "/api/lifecycle/processing",
        "/api/maintenance",
    )
    registered_paths = [getattr(route, "path", "") for route in client.app.routes]

    assert not [
        path for path in registered_paths if path.startswith(retired_prefixes)
    ]


def test_title_exclusion_policy_routes_are_available_from_system_settings(client: TestClient) -> None:
    """Removing the live policy route would make title exclusions uneditable."""
    csrf = client.get("/").cookies.get("oa_csrf")
    response = client.post(
        "/api/policies/bulk",
        json={"text": "合成排除关键词", "action": "skip", "scope": "title"},
        headers={"x-csrf-token": csrf or ""},
    )
    assert response.status_code == 200
    policies = client.get("/api/policies")
    assert policies.status_code == 200
    assert any(policy["pattern"] == "合成排除关键词" and policy["action"] == "skip" for policy in policies.json())


def test_title_exclusion_policy_can_be_edited_by_id(client: TestClient) -> None:
    """A legacy title rule must remain visible and become editable in settings."""
    csrf = client.get("/").cookies.get("oa_csrf")
    created = client.post(
        "/api/policies/bulk",
        json={"text": "旧的合成标题规则", "action": "metadata_only", "scope": "title"},
        headers={"x-csrf-token": csrf or ""},
    )
    assert created.status_code == 200
    policy_id = created.json()["policies"][0]["id"]

    response = client.put(
        f"/api/policies/{policy_id}",
        json={"pattern": "更新后的合成标题规则"},
        headers={"x-csrf-token": csrf or ""},
    )

    assert response.status_code == 200
    assert response.json()["pattern"] == "更新后的合成标题规则"
    assert response.json()["action"] == "skip"
    policies = client.get("/api/policies").json()
    assert any(policy["id"] == policy_id and policy["pattern"] == "更新后的合成标题规则" for policy in policies)
