from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.archive_reconciliation import reconcile_item
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem


def test_reconcile_item_moves_raw_tree_and_updates_database(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    old = Path("raw/done/2026/07/Synthetic_42")
    source = settings.data_root / old / "attachments" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF synthetic")
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:42", workitem_id_text="42", source_channel="done", title="Synthetic", initiated_at=datetime(2022, 4, 22), archive_relpath=old.as_posix())
        session.add(item); session.flush()
        file = ArchivedFile(oa_item_id=item.id, original_name="source.pdf", attachment_key="a", file_role="direct_attachment", source_container_key="root", local_relpath=(old / "attachments/source.pdf").as_posix(), download_status="verified")
        session.add(file); session.commit(); item_id=item.id; file_id=file.id

    with Session(engine) as session:
        result = reconcile_item(session, settings, item_id)
        session.commit()

    assert result.status == "migrated"
    target = Path("raw/done/2022/04/Synthetic_42")
    assert (settings.data_root / target / "attachments/source.pdf").is_file()
    assert not (settings.data_root / old).exists()
    with Session(engine) as session:
        assert session.get(OAItem, item_id).archive_relpath == target.as_posix()
        assert session.get(ArchivedFile, file_id).local_relpath == (target / "attachments/source.pdf").as_posix()


def test_reconcile_item_places_missing_date_under_unknown(config_file: Path) -> None:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    old = Path("raw/done/2026/07/Unknown_9")
    (settings.data_root / old).mkdir(parents=True)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        item = OAItem(oa_item_key="done:9", workitem_id_text="9", source_channel="done", title="Unknown", archive_relpath=old.as_posix())
        session.add(item); session.commit(); item_id=item.id
    with Session(engine) as session:
        result = reconcile_item(session, settings, item_id); session.commit()
    assert result.new_relpath == "raw/done/unknown/Unknown_9"
