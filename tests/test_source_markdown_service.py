"""有效 ParseArtifact 到唯一 Source Markdown 的发布测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, ContentObject, MarkdownExport, OAItem, ParseArtifact, ParseJob
from oa_knowledge.source_markdown.service import publish_active_artifact


def _seed(tmp_path: Path):
    settings = Settings(app={"data_root": tmp_path / "data"})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    original_relpath = "originals/done/2026/08/OA-SYNTHETIC/report.docx"
    original = settings.data_root / original_relpath
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original-binary-content")
    artifact_body = "# 已解析正文\n\n这是唯一的来源内容。"
    artifact_path = settings.parse_work_root / "parse/item-1/document.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(artifact_body, encoding="utf-8")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic", source_channel="done", title="合成事项",
            archive_relpath="originals/done/2026/08/OA-SYNTHETIC",
        )
        session.add(item); session.flush()
        content = ContentObject(
            sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
            size_bytes=original.stat().st_size,
        )
        session.add(content); session.flush()
        source = ArchivedFile(
            oa_item_id=item.id, attachment_key="source-1", file_role="direct_attachment",
            source_container_key="root", original_name="report.docx",
            local_relpath=original_relpath, download_status="verified",
            sha256=content.sha256, content_object_id=content.id,
        )
        session.add(source); session.flush()
        job = ParseJob(
            file_id=source.id, engine="synthetic", engine_version="1",
            config_hash="c" * 64, status="completed",
        )
        session.add(job); session.flush()
        artifact = ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="synthetic",
            engine_version="1", output_relpath="work/parse/item-1/document.md",
            source_sha256=content.sha256,
            product_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            config_hash="c" * 64, lifecycle_status="valid", quality_score=0.95,
        )
        session.add(artifact); session.flush()
        content.active_parse_artifact_id = artifact.id
        session.commit()
        source_id = source.id
    return settings, engine, source_id, artifact_path


def test_publish_uses_active_artifact_and_never_reparses_original(monkeypatch, tmp_path: Path) -> None:
    settings, engine, source_id, artifact_path = _seed(tmp_path)
    monkeypatch.setattr(
        "oa_knowledge.markdown_export.service.parse_file",
        lambda *_args, **_kwargs: pytest.fail("source publication must not call parse_file"),
    )

    with Session(engine) as session:
        record = publish_active_artifact(session, settings, source_id)
        session.commit()
        destination = settings.workspace_root / record.markdown_relpath
        content = destination.read_text(encoding="utf-8")

    assert "这是唯一的来源内容。" in content
    assert "original-binary-content" not in content
    assert "parse_artifact_id:" in content
    assert "source_relpath: originals/done/" in content
    assert not artifact_path.parent.exists()


def test_same_artifact_is_idempotent_and_new_product_atomically_replaces(tmp_path: Path) -> None:
    settings, engine, source_id, artifact_path = _seed(tmp_path)
    with Session(engine) as session:
        first = publish_active_artifact(session, settings, source_id)
        session.commit()
        destination = settings.workspace_root / first.markdown_relpath
        first_id, first_attempts = first.id, first.attempts

        again = publish_active_artifact(session, settings, source_id)
        session.commit()
        assert again.id == first_id and again.attempts == first_attempts

        old = session.get(ParseArtifact, again.parse_artifact_id)
        old.lifecycle_status = "superseded"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("# 新解析正文\n\n替换内容", encoding="utf-8")
        replacement = ParseArtifact(
            parse_job_id=old.parse_job_id, content_object_id=old.content_object_id,
            engine="synthetic", engine_version="2", output_relpath="work/parse/item-1/document.md",
            source_sha256=old.source_sha256,
            product_sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            config_hash="d" * 64, lifecycle_status="valid", quality_score=1.0,
        )
        session.add(replacement); session.flush()
        content_object = session.get(ContentObject, old.content_object_id)
        content_object.active_parse_artifact_id = replacement.id
        updated = publish_active_artifact(session, settings, source_id)
        session.commit()
        updated_id, updated_attempts = updated.id, updated.attempts

    assert updated_id == first_id and updated_attempts == first_attempts + 1
    assert "替换内容" in destination.read_text(encoding="utf-8")
    assert "唯一的来源内容" not in destination.read_text(encoding="utf-8")


def test_missing_valid_artifact_fails_without_creating_export(tmp_path: Path) -> None:
    settings, engine, source_id, _artifact_path = _seed(tmp_path)
    with Session(engine) as session:
        for artifact in session.query(ParseArtifact):
            artifact.lifecycle_status = "rejected"
        session.commit()

        with pytest.raises(FileNotFoundError, match="valid parse artifact"):
            publish_active_artifact(session, settings, source_id)
        assert session.query(MarkdownExport).count() == 0
