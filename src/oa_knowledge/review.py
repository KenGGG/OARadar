import json

from sqlalchemy.orm import Session

from oa_knowledge.archive.manifest import ItemManifest
from oa_knowledge.db.models import ReviewEntry


def enqueue_depth_review(session: Session, manifest: ItemManifest, item_id: int | None = None) -> ReviewEntry | None:
    limited = [c for c in manifest.containers if c.depth == 10 and c.has_unvisited_children]
    if not limited:
        return None
    entry = ReviewEntry(
        kind="depth_limit_reached",
        item_id=item_id,
        container_key=limited[0].container_key,
        depth=10,
        details_json=json.dumps({"oa_item_key": manifest.oa_item_key, "containers": [c.container_key for c in limited]}),
    )
    session.add(entry)
    return entry
