"""Local verification tests for the isolated Done Archive handoff."""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.constants import FileRole
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAManifestItem, OAItem
from oa_knowledge.done_archive import verify_done_archive


def _archive_file(settings, relpath: str, content: bytes) -> tuple[int, str]:
    path = settings.data_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return len(content), hashlib.sha256(content).hexdigest()


def _verified_item(session: Session, settings, *, attachment: bool = True) -> OAItem:
    item = OAItem(
        oa_item_key="done:archive-test", source_channel="done", title="Synthetic Done",
        pipeline_status="files_verified",
    )
    session.add(item)
    session.add(OAManifestItem(
        oa_item_key=item.oa_item_key, title=item.title, list_page=0,
        processing_status="downloaded", discovery_hash="manifest-hash",
    ))
    session.flush()
    relpath = "archive/raw/oa/done/synthetic/item/metadata.json"
    size, digest = _archive_file(settings, relpath, b'{"synthetic":true}')
    session.add(ArchivedFile(
        oa_item_id=item.id, original_name="metadata.json", attachment_key="metadata",
        file_role=str(FileRole.METADATA_SNAPSHOT), source_container_key="root",
        local_relpath=relpath, size_bytes=size, sha256=digest, download_status="verified",
    ))
    if attachment:
        relpath = "archive/raw/oa/done/synthetic/item/attachments/source.pdf"
        size, digest = _archive_file(settings, relpath, b"%PDF-synthetic")
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="source.pdf", attachment_key="source",
            file_role=str(FileRole.DIRECT_ATTACHMENT), source_container_key="root",
            local_relpath=relpath, size_bytes=size, sha256=digest, download_status="verified",
        ))
    session.flush()
    return item


def test_verified_done_archive_gets_stable_markdown_handoff_signature(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _verified_item(session, settings)
        result = verify_done_archive(session, settings, item.oa_item_key)

    assert result.status == "verified"
    assert result.content_signature is not None


def test_done_archive_hash_mismatch_is_not_accepted(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _verified_item(session, settings)
        file = next(row for row in item.files if row.file_role == str(FileRole.DIRECT_ATTACHMENT))
        (settings.data_root / file.local_relpath).write_bytes(b"tampered")
        result = verify_done_archive(session, settings, item.oa_item_key)

    assert result.status == "failed"
    assert result.reason == "ARCHIVE_SIZE_MISMATCH"


def test_done_archive_with_only_evidence_is_explicitly_no_attachment(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _verified_item(session, settings, attachment=False)
        result = verify_done_archive(session, settings, item.oa_item_key)

    assert result.status == "no_attachment"


def test_depth_limit_is_never_verified_as_a_complete_archive(config_file) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = _verified_item(session, settings)
        manifest = session.query(OAManifestItem).filter_by(oa_item_key=item.oa_item_key).one()
        manifest.processing_status = "depth_limit_reached"
        result = verify_done_archive(session, settings, item.oa_item_key)

    assert result.status == "failed"
    assert result.reason == "DEPTH_LIMIT_REACHED"
