import json
from datetime import datetime,timezone

from oa_knowledge.config import Settings
from oa_knowledge.web.local_conversion import read_local_conversion
from oa_knowledge.web.simple_status import local_delivery_progress


def seed(tmp_path,monkeypatch,stage='markdown_delivery',alive=True):
    settings=Settings(app={'data_root':tmp_path/'data'})
    root=settings.runtime_root;root.mkdir(parents=True)
    (root/'local-conversion.json').write_text(json.dumps({'progress_relpath':'synthetic/progress.json'}),encoding='utf8')
    progress=root/'synthetic/progress.json';progress.parent.mkdir()
    progress.write_text(json.dumps({'stage':stage,'processed':3,'total':10,'pid':12345,'updated_at':datetime.now(timezone.utc).isoformat()}),encoding='utf8')
    monkeypatch.setattr('oa_knowledge.web.local_conversion.process_running',lambda pid:alive)
    return settings,progress


def test_registered_live_progress_takes_precedence_and_is_read_only(tmp_path,monkeypatch):
    settings,path=seed(tmp_path,monkeypatch)
    before=path.read_bytes()
    result=local_delivery_progress(settings)
    assert result['live'] and result['status']=='running'
    assert result['processed']==3 and result['scope_done_items']==10
    assert 'complete_new_or_updated'not in result
    assert path.read_bytes()==before


def test_stopped_worker_is_not_reported_running_from_recent_snapshot(tmp_path,monkeypatch):
    settings,_=seed(tmp_path,monkeypatch,alive=False)
    assert read_local_conversion(settings)['status']=='stopped'


def test_finished_traversal_does_not_claim_all_attachments_succeeded(tmp_path,monkeypatch):
    settings,_=seed(tmp_path,monkeypatch,stage='local_conversion_finished',alive=False)
    result=read_local_conversion(settings)
    assert result['status']=='finished' and 'complete_new_or_updated'not in result


def test_rejects_paths_outside_runtime_and_invalid_counts(tmp_path,monkeypatch):
    settings,path=seed(tmp_path,monkeypatch)
    payload=json.loads(path.read_text());payload['processed']=11
    path.write_text(json.dumps(payload))
    assert not read_local_conversion(settings)['available']
    (settings.runtime_root/'local-conversion.json').write_text(json.dumps({'progress_relpath':'../outside.json'}))
    assert not read_local_conversion(settings)['available']
