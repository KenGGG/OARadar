"""Synthetic end-to-end acceptance smoke for the local data rebuild only."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.parsers.router import ParseResult
from oa_knowledge.rebuild.campaign import (
    create_rebuild_run,
    enqueue_archive_copy,
    enqueue_markdown_rebuild,
    execute_archive_copy,
)
from oa_knowledge.rebuild.classification import confirm_classification
from oa_knowledge.rebuild.cutover import CutoverPlan, CutoverSmokeError, execute_cutover
from oa_knowledge.rebuild.inventory import build_inventory
from oa_knowledge.rebuild.state_copy import (
    apply_rebuilt_ledger,
    backup_live_database,
    validate_database_copy,
)
from oa_knowledge.rebuild.validation import validate_rebuild, validation_passed, write_acceptance_evidence
from oa_knowledge.web.worker import OperationWorker


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _seed_file(
    session: Session,
    settings: Settings,
    item: OAItem,
    *,
    key: str,
    original_name: str,
    role: str,
    content: bytes,
) -> ArchivedFile:
    relpath = f"archive/raw/oa/done/synthetic/{key}-{original_name}"
    source = settings.data_root / relpath
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    archived = ArchivedFile(
        oa_item_id=item.id,
        original_name=original_name,
        local_relpath=relpath,
        attachment_key=key,
        file_role=role,
        source_container_key="synthetic-root",
        download_status="verified",
        size_bytes=len(content),
        sha256=_sha256(content),
    )
    session.add(archived)
    session.flush()
    return archived


def _synthetic_parse(
    source: Path, _settings: Settings, *, output_dir: Path | None = None,
) -> ParseResult:
    """Local parser double: preserves the parser-product contract without a service."""
    assert output_dir is not None
    product = output_dir / "result.md"
    product.write_text(f"# parsed\n\n{source.read_text(encoding='utf-8')}\n", encoding="utf-8")
    return ParseResult(
        output_path=product,
        engine="synthetic-local",
        engine_version="1",
        quality_score=1.0,
        text_length=len(product.read_text(encoding="utf-8")),
    )


def test_rebuild_from_synthetic_live_data_to_valid_new_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every rebuild boundary using only temporary synthetic evidence."""
    key_env = "SYNTHETIC_REBUILD_E2E_HMAC_KEY"
    monkeypatch.setenv(key_env, "synthetic-e2e-hmac-key-material-at-least-32-bytes")
    settings = Settings(
        app={"data_root": tmp_path / "live"},
        rebuild={
            "target_root": tmp_path / "rebuilt",
            "acceptance_evidence_key_env": key_env,
        },
    )
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            internal = OAItem(
                oa_item_key="done:synthetic-internal", source_channel="done",
                title="Synthetic internal", document_number="SYN-001",
                document_date=date(2026, 8, 20), classification_state="needs_review",
            )
            external = OAItem(
                oa_item_key="done:synthetic-external", source_channel="done",
                title="Synthetic external", document_date=date(2026, 8, 21),
                classification_state="needs_review",
            )
            review = OAItem(
                oa_item_key="done:synthetic-review", source_channel="done",
                title="Synthetic review", document_date=date(2026, 8, 22),
                classification_state="needs_review",
            )
            session.add_all((internal, external, review))
            session.flush()
            _seed_file(
                session, settings, internal, key="internal-page", original_name="body.html",
                role="body_snapshot", content=b"<main>synthetic page body</main>",
            )
            _seed_file(
                session, settings, external, key="external-attachment", original_name="attachment.txt",
                role="direct_attachment", content=b"synthetic external attachment",
            )
            _seed_file(
                session, settings, review, key="review-attachment", original_name="review.txt",
                role="direct_attachment", content=b"synthetic review attachment",
            )
            session.commit()
            confirm_classification(
                session, internal.id, source_type="internal", internal_category="风险管理",
                external_issuer=None, confirmed_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            confirm_classification(
                session, external.id, source_type="external", internal_category=None,
                external_issuer="Synthetic issuer", confirmed_at=datetime(2026, 8, 22, tzinfo=UTC),
            )
            session.commit()
            run = create_rebuild_run(session, cutoff_at=datetime(2026, 8, 22, tzinfo=UTC))
            rows = build_inventory(session, settings)
            assert {row.status for row in rows} == {"ready"}
            assert enqueue_archive_copy(session, run.id, rows, settings=settings) == 3
            assert execute_archive_copy(session, settings, run.id, rows) == {"copied": 3, "failed": 0}
            assert enqueue_markdown_rebuild(session, run.id, [internal.id, external.id, review.id]) == 2
            run_id, internal_id, external_id, review_id = run.id, internal.id, external.id, review.id

        monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", _synthetic_parse)
        worker = OperationWorker(settings)
        try:
            assert worker.run_until_idle() >= 4
        finally:
            worker.close()

        with Session(engine) as session:
            run = session.get(PipelineRun, run_id)
            assert run is not None and run.status == "completed"
            outputs = list(session.scalars(select(RebuildOutput).where(RebuildOutput.run_id == run_id)))
            assert sum(output.kind == "body_markdown" and output.status == "success" for output in outputs) == 1
            assert sum(
                output.kind == "body_markdown" and output.oa_item_id == external_id
                for output in outputs
            ) == 0
            assert not any(output.oa_item_id == review_id and output.kind != "original" for output in outputs)
            assert sum(output.kind == "item_index" and output.status == "success" for output in outputs) == 2
            write_acceptance_evidence(
                session,
                settings,
                run_id,
                webui_filter_contract=True,
                internal_sample_count=100,
                external_sample_count=100,
                automated_tests_passed=True,
                frontend_check_passed=True,
                build_passed=True,
                synthetic_smoke_passed=True,
            )
            checks = validate_rebuild(session, settings, run_id)
            assert validation_passed(checks), [check.code for check in checks if not check.ok]

        copied_database = settings.rebuild.target_root / "state" / "oa.db"
        backup_live_database(settings.database_path, copied_database)
        applied = apply_rebuilt_ledger(copied_database, run_id)
        assert applied["files"] == 3
        assert all(check.ok for check in validate_database_copy(copied_database))

        live_identity = settings.data_root / "runtime" / "synthetic-tree-identity"
        rebuilt_identity = settings.rebuild.target_root / "runtime" / "synthetic-tree-identity"
        live_identity.parent.mkdir(parents=True, exist_ok=True)
        rebuilt_identity.parent.mkdir(parents=True, exist_ok=True)
        live_identity.write_text("pre-cutover-live", encoding="utf-8")
        rebuilt_identity.write_text("pre-cutover-rebuilt", encoding="utf-8")

        plan = CutoverPlan(
            live_root=settings.data_root,
            rebuilt_root=settings.rebuild.target_root,
            legacy_root=tmp_path / "live_legacy_synthetic",
            units=(
                "oaradar-web.service", "oaradar-worker.service", "oaradar-markdown-worker.service",
                "oaradar-hourly.timer", "oaradar-nightly.timer",
            ),
            validation_ok=True,
            database_backup_ok=True,
            external_backup_ok=True,
            git_clean=True,
            units_discovered=True,
            same_filesystem=True,
            legacy_available=True,
        )
        monkeypatch.setattr("oa_knowledge.rebuild.cutover._control_units", lambda *_args: None)
        monkeypatch.setattr("oa_knowledge.rebuild.cutover._smoke", lambda _plan: False)
        with pytest.raises(CutoverSmokeError):
            execute_cutover(plan, authorized=True)
        assert settings.data_root.is_dir()
        assert settings.rebuild.target_root.is_dir()
        assert not plan.legacy_root.exists()
        assert live_identity.read_text(encoding="utf-8") == "pre-cutover-live"
        assert rebuilt_identity.read_text(encoding="utf-8") == "pre-cutover-rebuilt"
    finally:
        engine.dispose()
