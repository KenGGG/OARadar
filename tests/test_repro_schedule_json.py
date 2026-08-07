from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.web import create_web_app


def _client(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"app": {"data_root": str(tmp_path / "data")}}), encoding="utf-8")
    settings = load_settings(cfg)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    return TestClient(create_web_app(settings))


def test_schedule_status_is_json(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.get("/api/schedule/status")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/json")
    assert "overall_status" in r.json()


def test_unmatched_api_route_returns_json_404_not_html(tmp_path: Path) -> None:
    # A stale/old backend missing a route must surface as a clear 404, never the
    # SPA fallback HTML (which the console would misreport as "非 JSON 内容").
    client = _client(tmp_path)
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.json()["detail"] == "API route not found"
