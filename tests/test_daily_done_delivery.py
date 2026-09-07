import importlib.util
import json
from pathlib import Path
import pytest


def module():
    spec = importlib.util.spec_from_file_location('daily_done_delivery', Path('scripts/daily_done_delivery.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_bulk_scope_remains_frozen_until_first_pass_finishes(tmp_path):
    helper = module()
    root = tmp_path / 'bulk'
    root.mkdir()
    (root / 'scope.json').write_text(json.dumps({'keys': ['done:-1', 'done:2']}))
    summary = root / 'summary.json'
    summary.write_text(json.dumps({'not_processed': 1}))
    before = summary.read_bytes()
    assert helper.protected_keys(root) == {'done:-1', 'done:2'}
    assert summary.read_bytes() == before
    summary.write_text(json.dumps({'not_processed': 0}))
    assert helper.protected_keys(root) == set()
    summary.write_text('{')
    with pytest.raises(ValueError):
        helper.protected_keys(root)


@pytest.mark.parametrize(('outcome', 'task_status'), [('needs_review', 'completed'), ('partial', 'failed')])
def test_daily_delivery_leaves_bulk_and_excluded_tasks_untouched(config_file, tmp_path, monkeypatch, outcome, task_status):
    from sqlalchemy.orm import Session
    from oa_knowledge.config import load_settings
    from oa_knowledge.db.migrate import upgrade_database
    from oa_knowledge.db.models import OAManifestItem, PipelineTask
    from oa_knowledge.web.worker import OperationWorker
    helper = module()
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    worker = OperationWorker(settings, config_file)
    bulk = tmp_path / 'bulk'; bulk.mkdir()
    (bulk / 'scope.json').write_text(json.dumps({'keys': ['done:-1']}))
    (bulk / 'summary.json').write_text(json.dumps({'not_processed': 1}))
    unchanged = (bulk / 'summary.json').read_bytes()
    daily = tmp_path / 'daily'; daily.mkdir()
    with Session(worker.engine) as session:
        for key, status in [('done:-1', 'downloaded'), ('done:new', 'downloaded'), ('done:excluded', 'skipped')]:
            session.add(OAManifestItem(oa_item_key=key, workitem_id_text=key[5:], title='合成事项', list_page=1, processing_status=status))
        session.commit()
    tasks = {key: worker.production_queue.enqueue('markdown_delivery', key, 'attachment_inventory', key)
             for key in ('done:-1', 'done:new', 'done:excluded')}
    def fake_conversion(w, key, root):
        assert key == 'done:new'
        return {'manifest_id': key, 'status': outcome, 'reason': 'synthetic incomplete evidence or attachment'}
    monkeypatch.setattr(helper, 'process', fake_conversion)
    assert helper.drain(worker, daily, bulk, 'markdown_delivery') == 1
    with Session(worker.engine) as session:
        assert session.get(PipelineTask, tasks['done:-1']).status == 'queued'
        assert session.get(PipelineTask, tasks['done:excluded']).status == 'queued'
        assert session.get(PipelineTask, tasks['done:new']).status == task_status
    assert json.loads((daily / 'ledger.jsonl').read_text())['status'] == outcome
    assert (bulk / 'summary.json').read_bytes() == unchanged
    assert not (bulk / 'ledger.jsonl').exists()
    worker.engine.dispose()
