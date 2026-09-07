import json
from datetime import datetime, timezone

from oa_knowledge.config import Settings
from oa_knowledge.web.simple_status import local_delivery_progress


def test_batch_snapshot_is_read_only_and_does_not_count_review_as_success(tmp_path):
    settings = Settings(app={'data_root': tmp_path})
    assert not local_delivery_progress(settings)['available']
    root = tmp_path / 'runs' / 'local-markdown-synthetic'
    root.mkdir(parents=True)
    counts = dict(scope_done_items=10, processed=7, excluded=1,
                  complete_new_or_updated=2, complete_reused=1, partial=1,
                  final_needs_review=1, failed_or_missing=1, awaiting_evidence=0,
                  not_processed=3, attachment_markdown=9, item_indexes=4,
                  updated_at=datetime.now(timezone.utc).isoformat())
    path = root / 'summary.json'
    path.write_text(json.dumps(counts))
    before = path.read_bytes()
    result = local_delivery_progress(settings)
    assert result['available'] and result['processed'] == 7
    assert result['complete_new_or_updated'] + result['complete_reused'] == 3
    assert result['stage'] == 'unknown' and not result['stale']
    assert path.read_bytes() == before
    counts['processed'] = 10
    path.write_text(json.dumps(counts))
    assert not local_delivery_progress(settings)['available']
    path.write_text('{')
    assert not local_delivery_progress(settings)['available']
