"""A retried item is counted once; business gaps and evidence failures differ."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAManifestItem, OAItem
from oa_knowledge.web.worker import OperationWorker


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


def test_hash_mismatch_marks_only_that_item_failed_and_preserves_original(config_file, tmp_path):
    spec = importlib.util.spec_from_file_location('local_batch', Path(__file__).parents[1] / 'scripts/local_done_markdown.py')
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    original = settings.data_root / 'originals' / 'synthetic.pdf'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'synthetic original')
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        key = 'done:synthetic-hash-mismatch'
        item = OAItem(oa_item_key=key, source_channel='done', title='synthetic', archive_relpath='originals')
        session.add_all((item, OAManifestItem(oa_item_key=key, title='synthetic', list_page=1, processing_status='downloaded')))
        session.flush()
        session.add(ArchivedFile(oa_item_id=item.id, original_name='synthetic.pdf',
            local_relpath='originals/synthetic.pdf', sha256='0' * 64,
            attachment_key='synthetic', file_role='direct_attachment', source_container_key='synthetic',
            download_status='verified'))
        session.commit()
    worker = OperationWorker(settings, config_file)
    try:
        result = batch.process(worker, key, tmp_path)
    finally:
        worker.close()
    assert result['status'] == 'failed'
    assert result['reason'] == 'original_hash_mismatch'
    assert original.read_bytes() == b'synthetic original'
