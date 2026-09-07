"""A retried item is counted once; business gaps and evidence failures differ."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path


def test_snapshot_deduplicates_and_separates_review_causes(tmp_path):
    spec = importlib.util.spec_from_file_location('local_batch', Path(__file__).parents[1] / 'scripts/local_done_markdown.py')
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    (tmp_path / 'scope.json').write_text(json.dumps({'keys': list('abcdef'), 'first': ['a']}))
    rows = [
        {'manifest_id': 'a', 'status': 'partial'},
        {'manifest_id': 'a', 'status': 'complete', 'attachments': 2, 'indexes': 1},
        {'manifest_id': 'b', 'status': 'excluded'},
        {'manifest_id': 'c', 'status': 'needs_review', 'reason': {'review_reason': 'issuer_missing', 'evidence_step': {'rejection_code': 'schema_invalid'}}},
        {'manifest_id': 'd', 'status': 'needs_review', 'reason': {'review_reason': 'origin_unresolved', 'evidence_step': {'rejection_code': 'determined_origin_conflict'}}},
        {'manifest_id': 'e', 'status': 'needs_review', 'reason': {'review_reason': 'issuer_missing', 'evidence_step_code': 'no_parseable_content'}},
    ]
    (tmp_path / 'ledger.jsonl').write_text('\n'.join(map(json.dumps, rows)))
    summary = batch.summarize(tmp_path)
    assert summary['processed'] == 5
    assert summary['complete_new_or_updated'] == 1
    assert summary['partial'] == 0
    assert summary['not_processed'] == 1
    assert summary['excluded_scope'] == 'scanned_this_run'
    assert summary['review_missing_fields'] == {'issuer_missing': 2, 'origin_unresolved': 1}
    assert summary['review_evidence_failures'] == {'schema_invalid': 1, 'determined_origin_conflict': 1, 'no_parseable_content': 1}
    assert datetime.fromisoformat(summary['updated_at']).tzinfo is not None
