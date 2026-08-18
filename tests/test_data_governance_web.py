"""数据治理 API 的异步、安全和隐私边界测试。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import CleanupItem, CleanupRun, OperationJob, Run
from oa_knowledge.web.app import create_web_app
from oa_knowledge.web.worker import OperationWorker


def _client(config_file):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    return settings, TestClient(create_web_app(settings, config_file))


def test_data_governance_plan_requires_csrf_and_only_queues_job(config_file) -> None:
    settings, client = _client(config_file)
    assert client.post(
        "/api/data-governance/plans",
        json={"categories": ["browser_cache"]},
    ).status_code == 403

    csrf = client.get("/").cookies["oa_csrf"]
    response = client.post(
        "/api/data-governance/plans",
        headers={"x-csrf-token": csrf},
        json={"categories": ["browser_cache", "runtime_reports"]},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CleanupRun)) == 0
        job = session.get(OperationJob, response.json()["job_id"])
        assert job.job_type == "data_governance"
        assert "browser_cache" in job.parameters_json


def test_data_governance_status_is_aggregate_and_actions_are_queued(config_file) -> None:
    settings, client = _client(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        run = CleanupRun(
            status="planned", rules_version="data-v1", categories_json='["runtime_reports"]',
            candidate_count=2, candidate_bytes=123,
        )
        session.add(run)
        session.flush()
        session.add(CleanupItem(
            cleanup_run_id=run.id, relative_path="runtime/reports/synthetic.json",
            category="runtime_reports", size_bytes=123, reason_code="rebuildable_runtime_report",
        ))
        session.commit()
        run_id = run.id

    payload = client.get("/api/data-governance").json()
    assert payload["runs"][0] == {
        "id": run_id,
        "status": "planned",
        "rules_version": "data-v1",
        "categories": ["runtime_reports"],
        "candidate_count": 2,
        "candidate_bytes": 123,
        "quarantined_count": 0,
        "quarantined_bytes": 0,
        "restored_count": 0,
        "restored_bytes": 0,
        "purged_count": 0,
        "purged_bytes": 0,
    }
    assert "relative_path" not in repr(payload)
    assert payload["storage"]["category_summary"]["runtime_reports"] == {
        "count": 1, "bytes": 123,
    }
    assert payload["storage"]["tiers"][0]["retention"] == "永久保留"
    assert payload["storage"]["quarantine"]["recoverable"] is True

    csrf = client.cookies["oa_csrf"]
    action = client.post(
        f"/api/data-governance/runs/{run_id}/quarantine",
        headers={"x-csrf-token": csrf},
        json={},
    )
    assert action.status_code == 202
    assert action.json()["status"] == "queued"


def test_data_governance_purge_requires_confirmation(config_file) -> None:
    settings, client = _client(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        run = CleanupRun(status="quarantined", rules_version="data-v1", categories_json="[]")
        session.add(run)
        session.commit()
        run_id = run.id
    csrf = client.get("/").cookies["oa_csrf"]

    response = client.post(
        f"/api/data-governance/runs/{run_id}/purge",
        headers={"x-csrf-token": csrf},
        json={"confirmation": "wrong"},
    )
    assert response.status_code == 400


def test_data_governance_worker_executes_queued_plan(config_file) -> None:
    settings, client = _client(config_file)
    report = settings.data_root / "runtime/reports/synthetic.json"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"synthetic")
    csrf = client.get("/").cookies["oa_csrf"]
    queued = client.post(
        "/api/data-governance/plans",
        headers={"x-csrf-token": csrf},
        json={"categories": ["runtime_reports"]},
    ).json()

    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = session.get(OperationJob, queued["job_id"])
        run = session.scalar(select(CleanupRun))
        assert job.status == "completed"
        assert run.status == "planned"
        assert run.candidate_count == 1


def test_data_governance_exposes_only_latest_integrity_counts(config_file) -> None:
    settings, client = _client(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(Run(
            run_key="integrity-reconciliation:synthetic",
            stage="integrity_reconciliation",
            status="completed",
            summary_json=json.dumps({
                "total": 2655,
                "issue_counts": {"hash_mismatch": 6, "manifest_file_mismatch": 2649},
                "reason_counts": {"content_changed": 6, "manifest_schema_drift": 2143, "review_required": 506},
            }),
        ))
        session.commit()

    payload = client.get("/api/data-governance").json()

    assert payload["integrity"]["total"] == 2655
    assert payload["integrity"]["reason_counts"]["review_required"] == 506
    assert "relative_path" not in repr(payload["integrity"])


def test_data_governance_exposes_privacy_safe_archive_migration_progress(config_file) -> None:
    settings, client = _client(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OperationJob(
            job_key="archive-migration-synthetic",
            job_type="verified_archive_migration",
            status="running",
            idempotency_key="archive-migration-synthetic-v1",
            progress_current=25,
            progress_total=100,
            parameters_json=json.dumps({
                "audit_run_id": 7,
                "processed": 25,
                "migrated": 24,
                "failed": 1,
                "review_required": 3,
                "failed_item_ids": [123],
            }),
        ))
        session.commit()

    migration = client.get("/api/data-governance").json()["archive_migration"]

    assert migration == {
        "status": "running",
        "progress_current": 25,
        "progress_total": 100,
        "migrated": 24,
        "failed": 1,
        "review_required": 3,
    }
    assert "failed_item_ids" not in repr(migration)


def test_integrity_audit_endpoint_only_queues_durable_job(config_file) -> None:
    settings, client = _client(config_file)
    assert client.post("/api/data-governance/integrity-audits").status_code == 403
    csrf = client.get("/").cookies["oa_csrf"]

    response = client.post(
        "/api/data-governance/integrity-audits",
        headers={"x-csrf-token": csrf},
    )

    assert response.status_code == 202
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = session.get(OperationJob, response.json()["job_id"])
        assert job.job_type == "data_governance"
        assert json.loads(job.parameters_json) == {"action": "integrity_audit"}


def test_worker_persists_privacy_safe_integrity_audit_summary(config_file) -> None:
    settings, client = _client(config_file)
    csrf = client.get("/").cookies["oa_csrf"]
    queued = client.post(
        "/api/data-governance/integrity-audits",
        headers={"x-csrf-token": csrf},
    ).json()
    worker = OperationWorker(settings, config_path=config_file)
    try:
        assert worker.run_once() is True
    finally:
        worker.close()

    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = session.get(OperationJob, queued["job_id"])
        run = session.scalar(select(Run).where(Run.stage == "integrity_reconciliation"))
        summary = json.loads(run.summary_json)
        assert job.status == "completed"
        assert summary == {"issue_counts": {}, "reason_counts": {}, "total": 0}
        assert "relative_path" not in run.summary_json
