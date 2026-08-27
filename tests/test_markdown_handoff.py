"""Tests for the Done-archive → Markdown handoff (plan-0805-02 §4)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, MarkdownTask, OAItem
from oa_knowledge.markdown_export.paths import markdown_path_for_source
from oa_knowledge.markdown_queue import (
    SCHEMA_VERSION,
    audit_handoff,
    enqueue_missing_markdown_tasks,
)
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


def _engine(config_file):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    return settings, create_db_engine(settings.database_path)


def _make_item(session: Session, oa_item_key: str) -> OAItem:
    item = OAItem(oa_item_key=oa_item_key, source_channel="done", title="合成事项")
    session.add(item)
    session.flush()
    return item


_ATTACHMENT_SEQ = 0


def _make_verified_file(session: Session, oa_item_id: int, *, role: str, relpath: str, status: str = "verified") -> ArchivedFile:
    global _ATTACHMENT_SEQ
    _ATTACHMENT_SEQ += 1
    f = ArchivedFile(
        oa_item_id=oa_item_id, attachment_key=f"k-{_ATTACHMENT_SEQ}", file_role=role,
        original_name=Path(relpath).name if relpath else "unknown", local_relpath=relpath,
        source_container_key="c", download_status=status,
    )
    session.add(f)
    session.flush()
    return f


def test_enqueue_missing_markdown_tasks_picks_up_verified(config_file) -> None:
    settings, engine = _engine(config_file)
    with Session(engine) as session:
        item = _make_item(session, "done:1")
        session.add(item); session.flush()
        f = _make_verified_file(session, item.id, role=MARKDOWN_SOURCE_ROLES[0],
                                relpath="originals/2026/07/OA-1/attachments/报告.pdf")
        session.commit()
        fid = f.id

    queued = enqueue_missing_markdown_tasks(engine)
    assert queued == 1
    with Session(engine) as session:
        task = session.scalar(select(MarkdownTask).where(MarkdownTask.source_file_id == fid))
        assert task is not None
        assert task.schema_version == SCHEMA_VERSION
        assert task.status == "queued"


def test_enqueue_missing_markdown_tasks_skips_non_source_and_unverified(config_file) -> None:
    settings, engine = _engine(config_file)
    with Session(engine) as session:
        item = _make_item(session, "done:2")
        session.add(item); session.flush()
        # role not in MARKDOWN_SOURCE_ROLES -> skipped
        _make_verified_file(session, item.id, role="thumbnail", relpath="originals/unknown/x/t.png")
        # not verified -> skipped
        _make_verified_file(session, item.id, role=MARKDOWN_SOURCE_ROLES[0],
                            relpath=None, status="download_failed")
        # no local_relpath -> skipped
        _make_verified_file(session, item.id, role=MARKDOWN_SOURCE_ROLES[0], relpath=None)
        session.commit()

    assert enqueue_missing_markdown_tasks(engine) == 0


def test_enqueue_missing_markdown_tasks_idempotent_with_export(config_file) -> None:
    settings, engine = _engine(config_file)
    with Session(engine) as session:
        item = _make_item(session, "done:3")
        session.add(item); session.flush()
        f = _make_verified_file(session, item.id, role=MARKDOWN_SOURCE_ROLES[0],
                                relpath="originals/2026/07/OA-3/报告.pdf")
        session.flush()
        # Already successfully converted -> must not be re-queued.
        session.add(MarkdownExport(
            source_file_id=f.id, source_relpath=f.local_relpath, markdown_relpath="workspace/raw/sources/oa/done/2026/07/OA-3/报告.pdf.md",
            schema_version=SCHEMA_VERSION, status="success", source_sha256="0" * 64, parse_engine="direct-text", parse_engine_version="1", parse_config_hash="cfg",
        ))
        session.commit()
        fid = f.id

    assert enqueue_missing_markdown_tasks(engine) == 0
    with Session(engine) as session:
        assert session.scalar(select(MarkdownTask).where(MarkdownTask.source_file_id == fid)) is None


def test_audit_handoff_reports_counts(config_file) -> None:
    settings, engine = _engine(config_file)
    # Write a real verified source file so the on-disk existence check passes.
    src = settings.data_root / "originals/2026/07/OA-4/报告.pdf"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"%PDF-1.4 synthetic")
    with Session(engine) as session:
        item = _make_item(session, "done:4")
        session.add(item); session.flush()
        f = _make_verified_file(session, item.id, role=MARKDOWN_SOURCE_ROLES[0],
                                relpath="originals/2026/07/OA-4/报告.pdf")
        session.flush()
        session.add(MarkdownExport(
            source_file_id=f.id, source_relpath=f.local_relpath, markdown_relpath="workspace/raw/sources/oa/done/2026/07/OA-4/报告.pdf.md",
            schema_version=SCHEMA_VERSION, status="success", source_sha256="0" * 64, parse_engine="direct-text", parse_engine_version="1", parse_config_hash="cfg",
        ))
        # A pending task for the same file.
        session.add(MarkdownTask(source_file_id=f.id, schema_version=SCHEMA_VERSION, status="queued"))
        session.flush()
        # An orphan export (no backing ArchivedFile) whose source is also missing.
        session.add(MarkdownExport(
            source_file_id=None, source_relpath="originals/unknown/missing/报告.pdf",
            markdown_relpath="workspace/raw/sources/oa/done/missing/报告.pdf.md",
            schema_version=SCHEMA_VERSION, status="success", source_sha256="0" * 64, parse_engine="direct-text", parse_engine_version="1", parse_config_hash="cfg",
        ))
        session.commit()

    report = audit_handoff(engine, settings)
    assert report["verified_source_files"] == 1
    assert report["markdown_success"] == 2
    assert report["markdown_tasks"] == 1
    assert report["pending"] == 1
    assert report["failed"] == 0
    # one orphan export (source_file_id 999999 absent)
    assert report["orphan_exports"] == 1
    # the orphan export's source_relpath does not exist on disk
    assert report["missing_paths"] >= 1


def test_markdown_output_path_mirrors_originals_under_markdown_root(config_file) -> None:
    # The Markdown mirror is rooted at the distinct Markdown tree, never under originals.
    settings = load_settings(config_file)
    raw = settings.archive_root
    source = settings.data_root / "originals/2026/07/OA-1/attachments/报告.pdf"
    target = markdown_path_for_source(source, raw, settings.markdown_root)
    rel = target.relative_to(settings.markdown_root).as_posix()
    assert "originals" not in rel
    assert rel == "2026/07/OA-1/attachments/报告.pdf.md"
