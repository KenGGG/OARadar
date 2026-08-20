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
    "/api/policies",
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
