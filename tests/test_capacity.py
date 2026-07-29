from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem
from oa_knowledge.ops.capacity import capacity_report


def test_capacity_projection_uses_verified_bytes_and_safety(tmp_path: Path) -> None:
    db = tmp_path / "state" / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    with Session(engine) as session:
        item = OAItem(oa_item_key="one", source_channel="done", title="one")
        session.add(item); session.flush()
        session.add(ArchivedFile(
            oa_item_id=item.id, original_name="one.pdf", attachment_key="one",
            file_role="direct_attachment", source_container_key="one", depth=1,
            size_bytes=1000, download_status="verified",
        ))
        session.commit()
    report = capacity_report(db, tmp_path, 500, 1.5)
    assert report.average_bytes_per_item == 1000
    assert report.projected_incremental_bytes == 499000
    assert report.required_bytes_with_safety == 748500
    assert report.allowed
