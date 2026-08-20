from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.web import create_web_app


def _client(config_file: Path) -> TestClient:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings))


def test_v2_business_routes_are_available(config_file: Path) -> None:
    client = _client(config_file)
    for path in (
        "/api/simple-status", "/api/pending-notifications", "/api/done-archives",
        "/api/markdown-outputs", "/api/settings", "/api/schedule/status",
    ):
        assert client.get(path).status_code == 200
