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


def test_worker_handoff_recovers_resource_leases_from_stopped_owner(config_file):
    from oa_knowledge.config import load_settings
    from oa_knowledge.db.migrate import upgrade_database
    from oa_knowledge.resources import ResourceCoordinator
    from oa_knowledge.web.worker import OperationWorker

    helper = module()
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    worker = OperationWorker(settings, config_file)
    resources = ResourceCoordinator(worker.engine)
    stale = resources.acquire(
        'browser_profile', 'worker-999999:archive', ttl_seconds=3600,
        uses_local_gpu=False,
    )
    assert stale is not None

    helper.recover_worker_handoff(worker)

    replacement = resources.acquire(
        'browser_profile', worker.owner, ttl_seconds=60,
        uses_local_gpu=False,
    )
    assert replacement is not None
    resources.release(replacement, worker.owner)
    worker.engine.dispose()


def test_daily_converges_after_scan_before_queue_draining(monkeypatch, tmp_path):
    helper = module()
    calls = []

    class Planner:
        def __init__(self, engine, settings):
            calls.append('planner_init')

        def plan(self, *, apply):
            from oa_knowledge.done_convergence import DoneConvergenceReport
            assert apply is True
            calls.append('converge')
            return DoneConvergenceReport(1, 0, 1, 0, 0, 0, 0)

    worker = type('Worker', (), {'engine': object()})()
    monkeypatch.setattr(helper, 'run_nightly_scan', lambda *a, **kw: calls.append('scan') or {'status': 'ok'})
    monkeypatch.setattr(helper, 'DoneConvergencePlanner', Planner)
    monkeypatch.setattr(helper, 'drain', lambda *a, **kw: calls.append(f'drain:{a[3]}') or 0)

    summary = helper.run_daily(worker, object(), tmp_path, tmp_path)

    assert calls == ['scan', 'planner_init', 'converge', 'drain:realtime_done', 'drain:markdown_delivery']
    assert summary['convergence']['download_created'] == 1


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
