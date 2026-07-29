from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.manifest import ContainerManifest, ItemManifest
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ReviewEntry
from oa_knowledge.review import enqueue_depth_review


def test_depth_limit_creates_review(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    engine = create_db_engine(db)
    manifest = ItemManifest(
        oa_item_key="synthetic",
        workitem_id_text="1",
        title="synthetic",
        captured_at=datetime.now(timezone.utc),
        containers=[ContainerManifest(container_key="c10", page_family="govdoc", depth=10, has_unvisited_children=True)],
    )
    with Session(engine) as session:
        enqueue_depth_review(session, manifest)
        session.commit()
        entry = session.scalar(select(ReviewEntry))
        assert entry is not None
        assert entry.kind == "depth_limit_reached"
        assert entry.depth == 10
