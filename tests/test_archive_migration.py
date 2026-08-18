"""Tests for the legacy archive-path unification migration."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive_reconciliation import migrate_archive_paths
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem


def _seed(engine, data_root):
    with Session(engine) as session:
        done = OAItem(oa_item_key="done:x", source_channel="done", title="已办事项", workitem_id_text="x", initiated_at=None, archive_relpath="raw/done/unknown/已办事项_x")
        session.add(done)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=done.id, original_name="a.pdf", attachment_key="k", file_role="direct_attachment",
            source_container_key="c", depth=1, local_relpath="raw/done/unknown/已办事项_x/a.pdf", download_status="verified",
        ))
        session.add(OAManifestItem(
            oa_item_key="done:x", workitem_id_text="x", title="已办事项", list_page=1,
            processing_status="downloaded", archive_relpath="raw/done/unknown/已办事项_x",
        ))
        pending = OAItem(oa_item_key="pending:1", source_channel="pending", title="待办", archive_relpath="raw/pending/1/5")
        session.add(pending)
        session.flush()
        session.add(ArchivedFile(
            oa_item_id=pending.id, original_name="b.pdf", attachment_key="k2", file_role="direct_attachment",
            source_container_key="c2", depth=1, local_relpath="raw/pending/1/5/b.pdf", download_status="verified",
        ))
        session.commit()
        done_id, pending_id = done.id, pending.id

    # On-disk fixtures
    (data_root / "raw/done/unknown/已办事项_x").mkdir(parents=True, exist_ok=True)
    (data_root / "raw/done/unknown/已办事项_x/a.pdf").write_text("x")
    (data_root / "raw/pending/1/5").mkdir(parents=True, exist_ok=True)
    (data_root / "raw/pending/1/5/b.pdf").write_text("y")
    return done_id, pending_id


def test_migrate_archive_paths_dry_run_does_not_move(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    _seed(engine, settings.data_root)

    with Session(engine) as session:
        counts = migrate_archive_paths(session, settings, dry_run=True)
    assert counts["migrated"] == 2
    # Nothing was moved on disk.
    assert (settings.data_root / "raw/done/unknown/已办事项_x/a.pdf").is_file()
    assert not (settings.data_root / "archive").exists()


def test_migrate_archive_paths_moves_and_rewrites_db(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    done_id, pending_id = _seed(engine, settings.data_root)

    with Session(engine) as session:
        counts = migrate_archive_paths(session, settings, dry_run=False)
    assert counts["migrated"] == 2

    # Directories moved under the unified prefix.
    assert (settings.data_root / "archive/raw/oa/done/unknown/已办事项_x/a.pdf").is_file()
    assert (settings.data_root / "archive/raw/oa/pending/1/5/b.pdf").is_file()
    # Source leaf directories are gone (empty parent dirs may remain).
    assert not (settings.data_root / "raw/done/unknown/已办事项_x").exists()
    assert not (settings.data_root / "raw/pending/1/5").exists()

    with Session(engine) as session:
        done = session.get(OAItem, done_id)
        assert done.archive_relpath == "archive/raw/oa/done/unknown/已办事项_x"
        f = session.scalar(select(ArchivedFile).where(ArchivedFile.oa_item_id == done_id))
        assert f.local_relpath == "archive/raw/oa/done/unknown/已办事项_x/a.pdf"
        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:x"))
        assert manifest.archive_relpath == "archive/raw/oa/done/unknown/已办事项_x"
        pending = session.get(OAItem, pending_id)
        assert pending.archive_relpath == "archive/raw/oa/pending/1/5"
        pf = session.scalar(select(ArchivedFile).where(ArchivedFile.oa_item_id == pending_id))
        assert pf.local_relpath == "archive/raw/oa/pending/1/5/b.pdf"


def test_migrate_archive_paths_preserves_every_source_byte(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    _seed(engine, settings.data_root)
    source = settings.data_root / "raw/done/unknown/已办事项_x"
    # Historical manifests can contain the old relative prefix. A path migration
    # must not rewrite that evidence in place; derived manifests are rebuilt later.
    manifest = source / "manifest.json"
    manifest.write_text('{"source":"raw/done/unknown/已办事项_x/a.pdf"}', encoding="utf-8")
    before = {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*") if path.is_file()
    }

    with Session(engine) as session:
        counts = migrate_archive_paths(session, settings, dry_run=False)

    target = settings.data_root / "archive/raw/oa/done/unknown/已办事项_x"
    after = {
        path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in target.rglob("*") if path.is_file()
    }
    assert counts["failed"] == 0
    assert after == before
    assert "raw/done/unknown" in (target / "manifest.json").read_text(encoding="utf-8")


def test_migrate_archive_paths_rejects_symlinked_archive(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    _seed(engine, settings.data_root)
    source = settings.data_root / "raw/done/unknown/已办事项_x"
    outside = settings.data_root / "outside.txt"
    outside.write_text("unique evidence", encoding="utf-8")
    try:
        (source / "escape-link").symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with Session(engine) as session:
        counts = migrate_archive_paths(session, settings, dry_run=False)

    assert counts["failed"] == 1
    assert source.is_dir()
    assert outside.read_text(encoding="utf-8") == "unique evidence"


def test_migrate_archive_paths_is_idempotent(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    _seed(engine, settings.data_root)

    with Session(engine) as session:
        migrate_archive_paths(session, settings, dry_run=False)
    with Session(engine) as session:
        counts = migrate_archive_paths(session, settings, dry_run=False)
    assert counts["already_correct"] == 2
    assert counts["migrated"] == 0
