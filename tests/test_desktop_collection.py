import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from oa_knowledge.config import load_settings
from oa_knowledge.desktop_collection import desktop_owner, reserve_desktop


def test_reservation_requires_live_recent_desktop(config_file, monkeypatch):
    import oa_knowledge.desktop_collection as module
    settings = load_settings(config_file)
    settings.runtime_root.mkdir(parents=True)
    monkeypatch.setattr(module, 'process_running', lambda pid: True)
    path = settings.runtime_root / 'desktop-collection.json'
    path.write_text(json.dumps({'pid': 123, 'heartbeat_at': datetime.now(timezone.utc).isoformat()}))
    job = SimpleNamespace(lease_owner=None, lease_expires_at=None)
    reserve_desktop(settings, job)
    if os.name == 'nt':
        assert job.lease_owner == 'worker-123'
        assert job.lease_expires_at > datetime.now(timezone.utc)
    path.write_text(json.dumps({'pid': 123, 'heartbeat_at': (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}))
    assert desktop_owner(settings) is None


def test_disguised_html_is_identified_by_bytes(tmp_path):
    from oa_knowledge.parsers.format_router import detect_format
    source = tmp_path / 'notice.docx'
    source.write_bytes(b'<!DOCTYPE html><html><body>Synthetic notice</body></html>')
    assert detect_format(source).actual_file_type == 'html'


def test_zero_pixel_crop_keeps_one_pixel_sample():
    import ast
    from pathlib import Path
    source = Path('scripts/mineru-local.py').read_text(encoding='utf8')
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'safe_warp')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<synthetic>', 'exec'), namespace)
    warp = namespace['safe_warp'](lambda image, matrix, size, **kwargs: size)
    assert warp(None, None, (0, 15)) == (1, 15)
    assert warp(None, None, (25, 15)) == (25, 15)


def test_session_cache_is_origin_bound_and_uses_encryption(config_file, monkeypatch):
    import oa_knowledge.collector.session_cache as cache
    if os.name != 'nt':return
    settings = load_settings(config_file)
    settings.browser.base_url = 'https://synthetic.invalid'
    context = SimpleNamespace(cookies=lambda origins: [{'name':'synthetic', 'value':'fixture', 'domain':'synthetic.invalid', 'path':'/'}])
    # Reversible synthetic seam proves no plaintext is written by save_session.
    monkeypatch.setattr(cache, '_crypt', lambda data, decrypt=False: data[::-1])
    cache.save_session(settings, context)
    content = (settings.browser_profile_path/'oa-session.dpapi').read_bytes()
    assert b'fixture' not in content
    restored = []
    context.add_cookies = restored.extend
    cache.restore_session(settings, context)
    assert len(restored) == 1
    settings.browser.base_url = 'https://other.invalid'
    restored.clear()
    cache.restore_session(settings, context)
    assert restored == []


def test_daily_delivery_is_not_consumed_by_legacy_worker(config_file):
    from oa_knowledge.db.engine import create_db_engine
    from oa_knowledge.db.migrate import upgrade_database
    from oa_knowledge.db.models import PipelineTask
    from oa_knowledge.production_pipeline import ProductionQueue
    from sqlalchemy.orm import Session
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    queue = ProductionQueue(engine)
    identity = queue.enqueue('markdown_delivery','synthetic-item','attachment_inventory','synthetic-daily',payload={'local_delivery':True})
    assert queue.claim('worker-legacy',queue_names=('markdown_delivery',)) is None
    with Session(engine) as db:
        task = db.get(PipelineTask,identity)
        assert task.status == 'local_queued'
        task.status='running';task.lease_owner='worker-local';db.commit()
    queue.advance(identity,'worker-local','parse')
    with Session(engine) as db:
        task = db.get(PipelineTask,identity)
        assert (task.status,task.stage)==('local_queued','parse')
    engine.dispose()
