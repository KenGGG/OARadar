from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.writer import atomic_write_bytes
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch, ExclusionPolicy, OAItem
from oa_knowledge.ops.exclusion_cleanup import cleanup_excluded_archives


def test_cleanup_deletes_only_matched_archive_and_keeps_title(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    matched_rel = "raw/done/2026/07/出差申请_1"
    kept_rel = "raw/done/2026/07/重要制度_2"
    matched_file = atomic_write_bytes(b"matched", settings.data_root, f"{matched_rel}/body.html")
    atomic_write_bytes(b"manifest", settings.data_root, f"{matched_rel}/manifest.json")
    kept_file = atomic_write_bytes(b"keep", settings.data_root, f"{kept_rel}/body.html")
    with Session(engine) as session:
        session.add(ExclusionPolicy(name="trip", pattern="出差申请", action="metadata_only", scope="title"))
        batch = CollectionBatch(
            batch_key="cleanup", plan_hash="6" * 64, source_channel="done",
            planned_limit=2, discovered_count=2, archived_count=2, status="paused",
        )
        session.add(batch)
        session.flush()
        matched = OAItem(oa_item_key="done:1", source_channel="done", title="出差申请表", pipeline_status="files_verified", archive_relpath=matched_rel)
        kept = OAItem(oa_item_key="done:2", source_channel="done", title="重要制度", pipeline_status="files_verified", archive_relpath=kept_rel)
        session.add_all((matched, kept))
        session.flush()
        session.add_all((
            ArchivedFile(oa_item_id=matched.id, original_name="body.html", local_relpath=f"{matched_rel}/body.html", attachment_key="m", file_role="body_snapshot", source_container_key="m", depth=1, size_bytes=7, download_status="verified"),
            ArchivedFile(oa_item_id=kept.id, original_name="body.html", local_relpath=f"{kept_rel}/body.html", attachment_key="k", file_role="body_snapshot", source_container_key="k", depth=1, size_bytes=4, download_status="verified"),
            BatchItem(batch_id=batch.id, oa_item_key="done:1", workitem_id_text="1", title=matched.title, ordinal=1, archive_status="archived", oa_item_id=matched.id),
            BatchItem(batch_id=batch.id, oa_item_key="done:2", workitem_id_text="2", title=kept.title, ordinal=2, archive_status="archived", oa_item_id=kept.id),
        ))
        session.commit()

    result = cleanup_excluded_archives(settings)
    assert result.matched_items == 1
    assert result.deleted_files == 2
    assert not matched_file.exists()
    assert kept_file.exists()
    assert (settings.data_root / result.report_relpath).is_file()
    with Session(engine) as session:
        matched = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:1"))
        kept = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:2"))
        batch = session.scalar(select(CollectionBatch).where(CollectionBatch.batch_key == "cleanup"))
        assert matched is not None and matched.title == "出差申请表"
        assert matched.archive_relpath is None and matched.pipeline_status == "metadata_only"
        assert kept is not None and kept.archive_relpath == kept_rel
        assert batch is not None and batch.archived_count == 1 and batch.skipped_count == 1
