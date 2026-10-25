import json
from datetime import datetime,timezone,timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OperationJob,OAManifestItem,OAItem,ArchivedFile,PipelineTask
from oa_knowledge.daily_done import DailyDoneRunner,next_daily_run,daily_done_status

NOW=datetime(2026,10,16,1,tzinfo=timezone.utc)


@pytest.fixture
def runner(config_file):
    settings=load_settings(config_file);settings.daily_done.enabled=True
    upgrade_database(settings.database_path)
    result=DailyDoneRunner(settings)
    yield result
    result.close()


def job(runner,kind,status='queued',params=None,**extra):
    with Session(runner.engine)as db:
        value=OperationJob(job_key=uuid4().hex,idempotency_key=uuid4().hex,job_type=kind,status=status,parameters_json=json.dumps(params or {}),**extra)
        db.add(value);db.commit();return value.id


def test_daily_due_uses_local_time_not_utc():
    assert next_daily_run(NOW,'02:00','Asia/Shanghai').isoformat()=='2026-10-17T02:00:00+08:00'
    before=NOW.replace(hour=17)-timedelta(days=1)
    assert next_daily_run(before,'02:00','Asia/Shanghai').isoformat()=='2026-10-16T02:00:00+08:00'


def test_machine_enable_switch_does_not_require_changing_legacy_yaml(config_file):
    before=config_file.read_bytes();settings=load_settings(config_file)
    settings.runtime_root.mkdir(parents=True,exist_ok=True)
    path=settings.runtime_root/'daily-done-settings.json'
    path.write_text(json.dumps({'enabled':True}),encoding='utf8')
    assert load_settings(config_file).daily_done.enabled
    assert config_file.read_bytes()==before
    path.write_text(json.dumps({'enabled':False}),encoding='utf8')
    assert not load_settings(config_file).daily_done.enabled


def test_inventory_must_complete_before_download_and_runs_once_a_day(runner,monkeypatch):
    calls=[]
    def start(settings,kind):
        calls.append(kind);return {'id':job(runner,kind)}
    monkeypatch.setattr('oa_knowledge.daily_done.start_collection',start)
    state=runner.tick(now=NOW)
    assert calls==['done_inventory']
    runner.tick(now=NOW);assert calls==['done_inventory']
    with Session(runner.engine)as db:
        row=db.get(OperationJob,state['inventory_job_id']);row.status='completed';db.commit()
    state=runner.tick(now=NOW);assert calls==['done_inventory','done_download']
    with Session(runner.engine)as db:
        db.get(OperationJob,state['download_job_id']).status='completed';db.commit()
    assert runner.tick(now=NOW)['status']=='delivery_queued'
    runner.tick(now=NOW);assert len(calls)==2


@pytest.mark.parametrize('download_status,final_status',[('completed','delivery_queued'),('failed','partial')])
def test_successful_new_items_are_handed_off_without_requeueing_old_backlog_or_exclusions(runner,download_status,final_status):
    with Session(runner.engine)as db:
        for key,status in [('old','downloaded'),('new','no_attachment'),('excluded','skipped')]:
            db.add(OAManifestItem(oa_item_key='done:'+key,title='synthetic '+key,list_page=1,processing_status=status))
            db.add(OAItem(oa_item_key='done:'+key,title='synthetic '+key,source_channel='done'))
        db.commit()
    identity=job(runner,'done_download',download_status,{'source_keys':['done:old','done:new','done:excluded']})
    runner.state=dict(run_date=NOW.date().isoformat(),phase='download',download_job_id=identity,inventory_job_id=123,
                      baseline_keys=['done:old'],baseline_file_id=0,baseline_item_id=999)
    state=runner.tick(now=NOW)
    assert state['status']==final_status
    with Session(runner.engine)as db:
        tasks=list(db.scalars(select(PipelineTask)))
        assert len(tasks)==1 and tasks[0].logical_item_key=='done:new'
        assert tasks[0].stage=='attachment_inventory' and tasks[0].queue_name=='markdown_delivery'
    runner.tick(now=NOW)
    with Session(runner.engine)as db:assert db.scalar(select(func.count()).select_from(PipelineTask))==1


@pytest.mark.parametrize('phase', ['inventory', 'download'])
def test_auth_failure_retries_saved_login_daily_and_keeps_checkpoint(runner,monkeypatch,phase):
    identity=job(runner,'done_inventory','auth_required',heartbeat_at=NOW)
    runner.state=dict(run_date=NOW.date().isoformat(),phase=phase,**{phase+'_job_id':identity})
    called=[];monkeypatch.setattr('oa_knowledge.daily_done.control_collection',lambda *args:called.append(args))
    assert runner.tick(now=NOW)['status']==('syncing' if phase=='inventory' else 'downloading')
    assert called==[(runner.settings,identity,'resume')]
    assert runner.tick(now=NOW+timedelta(minutes=1))['status']=='auth_required'
    assert len(called)==1
    # The retry marker survives restarting the coordinator.
    runner.state=json.loads(runner.path.read_text(encoding='utf8'))
    runner.tick(now=NOW+timedelta(minutes=2));assert len(called)==1
    runner.tick(now=NOW+timedelta(days=1));assert len(called)==2
    job(runner,'done_login','completed',finished_at=NOW+timedelta(minutes=1))
    runner.tick(now=NOW+timedelta(days=1,minutes=2));assert len(called)==3
    assert runner.state[phase+'_job_id']==identity


def test_new_attachment_on_existing_item_is_handed_off(runner):
    with Session(runner.engine)as db:
        item=OAItem(oa_item_key='done:existing',title='synthetic existing',source_channel='done');db.add(item);db.flush()
        db.add(OAManifestItem(oa_item_key=item.oa_item_key,title=item.title,list_page=1,processing_status='downloaded'))
        for identity in (10,11):
            db.add(ArchivedFile(id=identity,oa_item_id=item.id,attachment_key='synthetic-'+str(identity),source_container_key='root',
                original_name='synthetic-'+str(identity)+'.pdf',
                file_role='direct_attachment',download_status='verified',sha256=str(identity)[0]*64,local_relpath='originals/synthetic-'+str(identity)+'.pdf'))
        db.commit()
    identity=job(runner,'done_download','completed',{'source_keys':['done:existing']})
    runner.state=dict(run_date=NOW.date().isoformat(),phase='download',download_job_id=identity,inventory_job_id=123,
                      baseline_keys=['done:existing'],baseline_file_id=10,baseline_item_id=999)
    runner.tick(now=NOW)
    with Session(runner.engine)as db:assert db.scalar(select(func.count()).select_from(PipelineTask))==1


def test_paused_and_failed_inventory_never_start_download(runner,monkeypatch):
    identity=job(runner,'done_inventory','paused')
    runner.state=dict(run_date=NOW.date().isoformat(),phase='inventory',inventory_job_id=identity)
    monkeypatch.setattr('oa_knowledge.daily_done.start_collection',lambda *args:pytest.fail('must not download'))
    assert runner.tick(now=NOW)['status']=='paused'
    with Session(runner.engine)as db:db.get(OperationJob,identity).status='failed';db.commit()
    assert runner.tick(now=NOW)['status']=='failed'


def test_status_uses_controller_liveness_and_hides_private_baseline(runner,monkeypatch):
    runner.state={'baseline_keys':['done:synthetic-private'],'status':'auth_required'}
    runner._save(datetime.now(timezone.utc))
    monkeypatch.setattr('oa_knowledge.daily_done.process_running',lambda pid:True)
    state=daily_done_status(runner.settings)
    assert state['active'] and state['status']=='auth_required'
    assert 'baseline_keys'not in state
    monkeypatch.setattr('oa_knowledge.daily_done.process_running',lambda pid:False)
    assert not daily_done_status(runner.settings)['active']
