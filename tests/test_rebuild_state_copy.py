"""Synthetic tests for the runtime-state copy prepared for cutover."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    MarkdownExport,
    Notification,
    OAItem,
    ParseArtifact,
    ParseJob,
    PipelineRun,
    RebuildOutput,
)
from oa_knowledge.rebuild.state_copy import (
    apply_rebuilt_ledger,
    backup_live_database,
    validate_database_copy,
)


def _integrity_check(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA integrity_check").fetchone()[0]


def _seed_rebuild_copy(path: Path) -> int:
    upgrade_database(path)
    engine = create_db_engine(path)
    try:
        with Session(engine) as session:
            item = OAItem(
                oa_item_key="synthetic-copy-item", source_channel="done", title="Synthetic copy",
            )
            source = ArchivedFile(
                oa_item_id=1, original_name="source.bin", local_relpath="retired/source.bin",
                attachment_key="synthetic-source", file_role="direct_attachment",
                source_container_key="synthetic-container", download_status="verified",
            )
            run = PipelineRun(run_key="synthetic-copy-run", pipeline_type="data_rebuild", status="completed")
            other_run = PipelineRun(run_key="synthetic-other-run", pipeline_type="data_rebuild", status="completed")
            session.add_all([item, run, other_run])
            session.flush()
            source.oa_item_id = item.id
            session.add(source)
            session.flush()
            parse_job = ParseJob(
                file_id=source.id, engine="synthetic", engine_version="1", config_hash="j" * 64,
                status="success", output_relpath="retired/parse/source",
            )
            session.add(parse_job)
            session.flush()
            session.add(ParseArtifact(
                parse_job_id=parse_job.id, engine="synthetic", engine_version="1",
                output_relpath="retired/parse/source", source_sha256="a" * 64,
                config_hash="j" * 64, lifecycle_status="valid",
            ))
            session.add_all([
                MarkdownExport(
                    source_file_id=source.id, oa_item_id=item.id, document_kind="attachment",
                    source_sha256="a" * 64, source_relpath="retired/source.bin",
                    markdown_relpath="retired/source.bin.md", assets_relpath="retired/assets",
                    parse_engine="synthetic", parse_engine_version="1", parse_config_hash="b" * 64,
                    schema_version="synthetic-v1", status="success",
                ),
                MarkdownExport(
                    oa_item_id=item.id, document_kind="item_index", source_sha256="c" * 64,
                    source_relpath="archive/placeholder", markdown_relpath="retired/_index.md",
                    parse_engine="synthetic", parse_engine_version="1", parse_config_hash="d" * 64,
                    schema_version="synthetic-index-v1", status="success",
                ),
                Notification(
                    idempotency_key="synthetic-dedupe", channel="local", status="queued",
                    payload_hash="e" * 64, attempts=2,
                ),
                RebuildOutput(
                    run_id=run.id, oa_item_id=item.id, source_file_id=source.id, kind="original",
                    target_relpath="archive/current/source.bin", sha256="a" * 64, status="success",
                ),
                RebuildOutput(
                    run_id=run.id, oa_item_id=item.id, source_file_id=source.id, kind="attachment_markdown",
                    target_relpath="markdown/current/source.bin.md", sha256="f" * 64, status="success",
                ),
                RebuildOutput(
                    run_id=run.id, oa_item_id=item.id, kind="item_index",
                    target_relpath="markdown/current/_index.md", sha256="g" * 64, status="success",
                ),
                RebuildOutput(
                    run_id=run.id, oa_item_id=item.id, source_file_id=source.id, kind="parse",
                    target_relpath="parse/current/source", sha256="h" * 64, status="success",
                ),
                RebuildOutput(
                    run_id=other_run.id, oa_item_id=item.id, source_file_id=source.id, kind="original",
                    target_relpath="archive/other/source.bin", sha256="i" * 64, status="success",
                ),
            ])
            session.commit()
            return run.id
    finally:
        engine.dispose()


def test_backup_is_consistent_while_source_connection_is_open(tmp_path: Path) -> None:
    """Removing the backup API or copying raw WAL bytes must fail this test."""
    source = tmp_path / "live.db"
    target = tmp_path / "copy.db"
    upgrade_database(source)

    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO notifications VALUES (1, ?, ?, ?, ?, ?)", (
            "synthetic-dedupe", "local", "queued", "a" * 64, 0,
        ))
        connection.commit()
        backup_live_database(source, target)

    assert _integrity_check(target) == "ok"
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT idempotency_key FROM notifications").fetchone()[0] == "synthetic-dedupe"


def test_apply_ledger_updates_only_the_copy_and_preserves_notification_dedupe(tmp_path: Path) -> None:
    """Changing the source, using failed output, or retaining retired paths must fail this test."""
    source = tmp_path / "live.db"
    copied = tmp_path / "copy.db"
    run_id = _seed_rebuild_copy(source)
    backup_live_database(source, copied)

    result = apply_rebuilt_ledger(copied, run_id)

    assert result == {"files": 1, "markdown_exports": 2, "parse_artifacts": 1}
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT local_relpath FROM files").fetchone()[0] == "retired/source.bin"
        assert connection.execute("SELECT idempotency_key, attempts FROM notifications").fetchone() == ("synthetic-dedupe", 2)
    with sqlite3.connect(copied) as connection:
        assert connection.execute("SELECT local_relpath FROM files").fetchone()[0] == "archive/current/source.bin"
        assert connection.execute("SELECT output_relpath FROM parse_artifacts").fetchone()[0] == "parse/current/source"
        exports = connection.execute(
            "SELECT source_relpath, markdown_relpath, assets_relpath FROM markdown_exports ORDER BY id"
        ).fetchall()
        assert exports == [
            ("archive/current/source.bin", "markdown/current/source.bin.md", "markdown/current/assets/1"),
            ("archive/placeholder", "markdown/current/_index.md", None),
        ]
        assert connection.execute("SELECT idempotency_key, attempts FROM notifications").fetchone() == ("synthetic-dedupe", 2)
    assert all(check.ok for check in validate_database_copy(copied))


def test_apply_ledger_rejects_unmapped_retired_active_path(tmp_path: Path) -> None:
    """Dropping the post-remap path gate would leave a cutover copy pointing at retired data."""
    source = tmp_path / "live.db"
    copied = tmp_path / "copy.db"
    run_id = _seed_rebuild_copy(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO files (oa_item_id, original_name, local_relpath, attachment_key, file_role, source_container_key, depth, download_status, download_attempts) "
            "VALUES (1, 'unmapped.bin', 'retired/unmapped.bin', 'unmapped', 'direct_attachment', 'synthetic-container', 1, 'verified', 0)"
        )
    backup_live_database(source, copied)

    with pytest.raises(RuntimeError, match="retired filesystem paths"):
        apply_rebuilt_ledger(copied, run_id)

    assert not next(check for check in validate_database_copy(copied) if check.code == "ACTIVE_RUNTIME_PATHS").ok


def test_backup_does_not_replace_target_when_alembic_revision_is_invalid(tmp_path: Path) -> None:
    """Removing schema verification before promotion would overwrite the last known-good copy."""
    source = tmp_path / "live.db"
    target = tmp_path / "copy.db"
    upgrade_database(source)
    target.write_text("prior-copy", encoding="utf-8")
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'synthetic-old-revision'")

    with pytest.raises(RuntimeError, match="validation failed"):
        backup_live_database(source, target)

    assert target.read_text(encoding="utf-8") == "prior-copy"
