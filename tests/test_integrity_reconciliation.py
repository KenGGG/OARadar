import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ContentObject, OAItem
from oa_knowledge.integrity_reconciliation import (
    classify_hash_evidence,
    classify_integrity_issues,
    classify_manifest_evidence,
    normalize_historical_manifest_path,
)


def test_hash_evidence_distinguishes_stale_ledger_from_changed_content() -> None:
    assert classify_hash_evidence(
        recorded_sha="a" * 64,
        actual_sha="b" * 64,
        content_object_sha="b" * 64,
        manifest_sha=None,
    ) == "stale_recorded_hash"
    assert classify_hash_evidence(
        recorded_sha="a" * 64,
        actual_sha="b" * 64,
        content_object_sha="a" * 64,
        manifest_sha="a" * 64,
    ) == "content_changed"


def test_hash_evidence_keeps_ambiguous_mismatch_for_review() -> None:
    assert classify_hash_evidence(
        recorded_sha="a" * 64,
        actual_sha="b" * 64,
        content_object_sha="c" * 64,
        manifest_sha=None,
    ) == "review_required"


def test_manifest_evidence_recognizes_later_database_expansion() -> None:
    assert classify_manifest_evidence(
        manifest_paths={"raw/done/a.pdf"},
        database_paths={"raw/done/a.pdf", "raw/done/body.html"},
        missing_paths=set(),
    ) == "manifest_schema_drift"


def test_manifest_evidence_never_hides_a_missing_original() -> None:
    assert classify_manifest_evidence(
        manifest_paths={"raw/done/a.pdf"},
        database_paths={"raw/done/a.pdf", "raw/done/body.html"},
        missing_paths={"raw/done/a.pdf"},
    ) == "real_missing_source"
    assert classify_manifest_evidence(
        manifest_paths={"raw/done/legacy.pdf"},
        database_paths={"raw/done/current.pdf"},
        missing_paths=set(),
    ) == "review_required"


def test_historical_manifest_path_maps_only_with_matching_byte_evidence() -> None:
    current = ArchivedFile(
        oa_item_id=1,
        original_name="source.bin",
        attachment_key="same-key",
        file_role="official_body",
        source_container_key="root",
        local_relpath="originals/2026/08/current/source.bin",
        sha256="a" * 64,
        download_status="verified",
    )
    assert normalize_historical_manifest_path(
        "raw/done/legacy/source.bin",
        attachment_key="same-key",
        file_role="official_body",
        sha256="a" * 64,
        database_files=[current],
    ) == "originals/2026/08/current/source.bin"
    assert normalize_historical_manifest_path(
        "raw/done/legacy/source.bin",
        attachment_key="same-key",
        file_role="official_body",
        sha256="b" * 64,
        database_files=[current],
    ) == "raw/done/legacy/source.bin"


def test_integrity_summary_contains_reasons_but_not_confidential_paths(tmp_path: Path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    relative_path = "originals/2026/08/synthetic/source.bin"
    source = settings.data_root / relative_path
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic-current-content")
    actual_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="synthetic-integrity",
            source_channel="done",
            title="合成完整性事项",
        )
        content = ContentObject(sha256=actual_sha, size_bytes=source.stat().st_size)
        session.add_all((item, content))
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id,
            original_name="source.bin",
            attachment_key="synthetic-body",
            file_role="official_body",
            source_container_key="root",
            local_relpath=relative_path,
            size_bytes=source.stat().st_size,
            sha256="a" * 64,
            content_object_id=content.id,
            download_status="verified",
        ))
        session.commit()

    summary = classify_integrity_issues(settings, engine)

    assert summary.issue_counts == {"hash_mismatch": 1}
    assert summary.reason_counts == {"stale_recorded_hash": 1}
    assert relative_path not in repr(summary)
    engine.dispose()


def test_integrity_audit_accepts_byte_proven_legacy_manifest_after_move(
    tmp_path: Path, monkeypatch,
) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    current_rel = "originals/2026/08/current/source.bin"
    source = settings.data_root / current_rel
    source.parent.mkdir(parents=True)
    source.write_bytes(b"same immutable bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_rel = "originals/2026/08/current/manifest.json"
    (settings.data_root / manifest_rel).write_text(json.dumps({
        "oa_item_key": "done:manifest-moved",
        "workitem_id_text": "manifest-moved",
        "title": "合成事项",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "containers": [{
            "container_key": "root", "parent_container_key": None,
            "page_family": "synthetic", "depth": 1,
            "direct_file_count": 1, "child_container_count": 0,
            "has_unvisited_children": False,
            "files": [{
                "attachment_key": "same-key", "original_name": "source.bin",
                "local_relpath": "raw/done/legacy/source.bin",
                "file_role": "official_body", "source_container_key": "root",
                "size_bytes": source.stat().st_size, "sha256": digest,
                "download_status": "verified",
            }],
        }],
    }), encoding="utf-8")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:manifest-moved", source_channel="done",
            title="合成事项", archive_relpath="originals/2026/08/current",
        )
        batch = CollectionBatch(
            batch_key="synthetic-manifest", source_channel="done", planned_limit=1,
            plan_hash="f" * 64,
        )
        session.add_all((item, batch)); session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="source.bin", attachment_key="same-key",
            file_role="official_body", source_container_key="root",
            local_relpath=current_rel, size_bytes=source.stat().st_size,
            sha256=digest, download_status="verified",
        ))
        batch_item = BatchItem(
            batch_id=batch.id, oa_item_key=item.oa_item_key,
            workitem_id_text="manifest-moved", title="合成事项", ordinal=1,
            oa_item_id=item.id, archive_manifest_relpath=manifest_rel,
        )
        session.add(batch_item); session.commit(); batch_item_id = batch_item.id
    monkeypatch.setattr(
        "oa_knowledge.integrity_reconciliation.audit_database",
        lambda _settings: [SimpleNamespace(code="manifest_file_mismatch", record_id=batch_item_id)],
    )

    summary = classify_integrity_issues(settings, engine)

    assert summary.total == 0
    assert (settings.data_root / manifest_rel).read_text(encoding="utf-8").find("raw/done/legacy") > 0
