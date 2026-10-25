"""Daily read-only Done inventory, screened downloads, and local delivery handoff."""
from __future__ import annotations

import json,os,hashlib
import subprocess,sys
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select,func
from sqlalchemy.orm import Session

from oa_knowledge.archive.writer import atomic_write_bytes
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OperationJob,OAManifestItem,OAItem,ArchivedFile,PipelineTask
from oa_knowledge.done_collection import start_collection,control_collection
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.web.local_conversion import process_running


def next_daily_run(now,at,timezone_name):
    local=now.astimezone(ZoneInfo(timezone_name));hour,minute=map(int,at.split(':'))
    due=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
    return due if due>local else due+timedelta(days=1)


def daily_done_status(settings):
    path=settings.runtime_root/'daily-done.json'
    result={'enabled':settings.daily_done.enabled,'active':False,'status':'not_run','next_run_at':None,'last_scan_at':None}
    if not path.exists():return result
    try:
        raw=json.loads(path.read_text(encoding='utf8'))
        beat=datetime.fromisoformat(raw['heartbeat_at'])
        active=(datetime.now(timezone.utc)-beat).total_seconds()<max(120,settings.daily_done.poll_seconds*3)
        alive=process_running(int(raw['pid']))
        result.update(active=active and alive is not False,status=raw.get('status','unknown'),
                      last_scan_at=raw.get('inventory_completed_at'),
                      last_error=raw.get('last_error'),inventory_job_id=raw.get('inventory_job_id'),
                      download_job_id=raw.get('download_job_id'),local_tasks_enqueued=raw.get('local_tasks_enqueued',0),
                      heartbeat_at=raw['heartbeat_at'])
        if result['active']and result['enabled']:result['next_run_at']=raw.get('next_run_at')
    except (OSError,ValueError,KeyError,TypeError):result['status']='unknown'
    return result


def ensure_operation_worker(settings,config_path=None):
    """Restart a missing queue consumer; its exclusive lock prevents duplicates."""
    try:
        raw=json.loads((settings.runtime_root/'operation-worker.json').read_text(encoding='utf8'))
        if process_running(int(raw['pid']))is not False:return
    except (OSError,ValueError,KeyError,TypeError):pass
    command=[sys.executable,'-X','utf8','-m','oa_knowledge.cli','worker']
    if config_path:command+=['--config',str(config_path.resolve())]
    settings.runtime_root.mkdir(parents=True,exist_ok=True)
    with(settings.runtime_root/'daily-worker.stdout.log').open('ab')as out,(settings.runtime_root/'daily-worker.stderr.log').open('ab')as err:
        subprocess.Popen(command,stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt'else 0)


def ensure_desktop_collection(settings,config_path):
    if os.name != 'nt' or config_path is None:return
    from oa_knowledge.desktop_collection import desktop_owner
    if desktop_owner(settings):return
    with(settings.runtime_root/'desktop-collection.stdout.log').open('ab')as out,(settings.runtime_root/'desktop-collection.stderr.log').open('ab')as err:
        subprocess.Popen([sys.executable,'-X','utf8','-m','oa_knowledge.desktop_collection',str(config_path.resolve())],stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW)
    from threading import Event
    for _ in range(20):
        if desktop_owner(settings):return
        Event().wait(0.25)
    raise RuntimeError('desktop_collection_not_ready')


def ensure_local_delivery(settings,config_path):
    if config_path is None:return
    try:
        state=json.loads((settings.runtime_root/'local-delivery-worker.json').read_text(encoding='utf8'))
        if process_running(int(state['pid'])) is not False:return
    except (OSError,ValueError,KeyError,TypeError):pass
    with(settings.runtime_root/'local-delivery.stdout.log').open('ab')as out,(settings.runtime_root/'local-delivery.stderr.log').open('ab')as err:
        subprocess.Popen([sys.executable,'-X','utf8','-m','oa_knowledge.local_delivery_worker',str(config_path.resolve())],stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt'else 0)


class DailyDoneRunner:
    def __init__(self,settings):
        self.settings=settings;self.engine=create_db_engine(settings.database_path)
        self.path=settings.runtime_root/'daily-done.json'
        try:self.state=json.loads(self.path.read_text(encoding='utf8'))
        except (OSError,ValueError):self.state={}

    def close(self):self.engine.dispose()

    def _save(self,now):
        self.state.update(heartbeat_at=now.isoformat(),pid=os.getpid(),
            next_run_at=next_daily_run(now,self.settings.daily_done.at_local_time,self.settings.app.timezone).isoformat())
        atomic_write_bytes(json.dumps(self.state,ensure_ascii=False,indent=2).encode('utf8'),self.settings.runtime_root,self.path.name)
        return dict(self.state)

    def _job(self,identity):
        with Session(self.engine)as db:
            job=db.get(OperationJob,identity)
            if not job:return None
            return dict(id=job.id,status=job.status,params=json.loads(job.parameters_json or '{}'),
                        finished_at=job.finished_at,auth_since=job.heartbeat_at or job.started_at or job.created_at)

    def _begin_inventory(self):
        try:return start_collection(self.settings,'done_inventory')['id']
        except ValueError:
            with Session(self.engine)as db:
                previous=db.scalar(select(OperationJob).where(OperationJob.job_type=='done_inventory').order_by(OperationJob.id.desc()))
                if previous and previous.status in {'paused','auth_required'}:return previous.id
            raise

    def _enqueue_delivery(self,job):
        source_keys=job['params'].get('source_keys',[])
        before=set(self.state.get('baseline_keys',[]));max_file=self.state.get('baseline_file_id',0)
        queued=0;queue=ProductionQueue(self.engine)
        with Session(self.engine)as db:
            candidates=set(source_keys)-before
            candidates.update(db.scalars(select(OAItem.oa_item_key).join(ArchivedFile).where(ArchivedFile.id>max_file,ArchivedFile.download_status=='verified')))
            candidates.update(db.scalars(select(OAItem.oa_item_key).where(OAItem.id>self.state.get('baseline_item_id',0))))
            candidates.intersection_update(source_keys)
            rows=list(db.scalars(select(OAManifestItem).where(OAManifestItem.oa_item_key.in_(candidates))))
            for row in rows:
                if row.processing_status not in {'downloaded','no_attachment'}or row.matched_exclusion_keyword:continue
                item=db.scalar(select(OAItem).where(OAItem.oa_item_key==row.oa_item_key))
                if item is None:continue
                files=list(db.scalars(select(ArchivedFile).where(ArchivedFile.oa_item_id==item.id,
                    ArchivedFile.download_status=='verified',ArchivedFile.id>max_file)))
                if row.oa_item_key in before and item.id<=self.state.get('baseline_item_id',0)and not files:continue
                version=hashlib.sha256(json.dumps([(str(f.id),f.sha256)for f in files]).encode()).hexdigest()
                identity=f'daily-done:{row.oa_item_key}:{version}'
                if db.scalar(select(PipelineTask.id).where(PipelineTask.idempotency_key==identity)) is not None:continue
                queue.enqueue('markdown_delivery',row.oa_item_key,'attachment_inventory',identity,
                    payload={'daily_inventory_job_id':self.state['inventory_job_id'],'local_delivery':True})
                queued+=1
        return queued

    def tick(self,*,now=None,force=False):
        now=now or datetime.now(timezone.utc)
        if not self.settings.daily_done.enabled:
            self.state['status']='disabled';return self._save(now)
        local=now.astimezone(ZoneInfo(self.settings.app.timezone));date=local.date().isoformat()
        phase=self.state.get('phase')
        if phase in {'inventory','download'}:
            field='inventory_job_id'if phase=='inventory'else'download_job_id'
            job=self._job(self.state.get(field))
            if not job:
                self.state.update(status='failed',last_error='后台任务记录缺失');return self._save(now)
            if job['status']=='auth_required':
                # A successful interactive login may resume the same checkpoint.
                with Session(self.engine)as db:
                    login=db.scalar(select(OperationJob).where(OperationJob.job_type=='done_login',OperationJob.status=='completed').order_by(OperationJob.id.desc()))
                    after=bool(login and login.finished_at and job['auth_since']and login.finished_at>job['auth_since'])
                # Resume through the normal collection path, which already
                # logs in using locally saved credentials. Never leave a
                # transient login failure blocking every future daily run.
                retry_due = self.state.get('auth_retry_date') != date
                if after or retry_due:
                    try:
                        control_collection(self.settings,job['id'],'resume')
                    except ValueError:
                        self.state.update(status='waiting',last_error='等待当前 OA 操作结束后自动恢复登录')
                        return self._save(now)
                    self.state['auth_retry_date'] = date
                    self.state.update(status='syncing'if phase=='inventory'else'downloading',last_error=None)
                else:
                    self.state.update(status='auth_required',last_error='使用本机保存的凭据自动登录未成功；同步断点保留，将于次日自动重试，也可在本机登录后继续')
                    return self._save(now)
            elif job['status']=='paused':
                self.state.update(status='paused',last_error='同步任务已暂停，等待继续');return self._save(now)
            elif job['status']in {'queued','running'}:
                if phase=='download':
                    added=self._enqueue_delivery(job)
                    self.state['local_tasks_enqueued']=self.state.get('local_tasks_enqueued',0)+added
                self.state.update(status='syncing'if phase=='inventory'else'downloading',last_error=None)
                return self._save(now)
            elif phase=='inventory':
                if job['status']!='completed':
                    self.state.update(phase='finished',status='failed',last_error='已办清单未完整同步，未开始自动下载')
                    return self._save(now)
                self.state['inventory_completed_at']=now.isoformat()
                self.state['run_date']=date
                download=start_collection(self.settings,'done_download')
                self.state.update(phase='download',download_job_id=download['id'],status='downloading',last_error=None)
            else:
                queued=self._enqueue_delivery(job)
                self.state.update(phase='finished',status='delivery_queued'if job['status']=='completed'else'partial',
                    local_tasks_enqueued=self.state.get('local_tasks_enqueued',0)+queued,last_error=None if job['status']=='completed'else'部分附件下载未成功；已成功的附件已进入转换分类队列')
            return self._save(now)
        due=force or(self.state.get('run_date')!=date and local.strftime('%H:%M')>=self.settings.daily_done.at_local_time)
        if not due:return self._save(now)
        with Session(self.engine)as db:
            keys=list(db.scalars(select(OAManifestItem.oa_item_key)))
            max_file=db.scalar(select(func.max(ArchivedFile.id)))or 0
            max_item=db.scalar(select(func.max(OAItem.id)))or 0
        try:identity=self._begin_inventory()
        except ValueError:
            self.state.update(status='waiting',last_error='等待当前 OA 操作结束后自动开始同步');return self._save(now)
        last=self.state.get('inventory_completed_at')
        self.state=dict(run_date=date,phase='inventory',status='syncing',inventory_job_id=identity,
                        baseline_keys=keys,baseline_file_id=max_file,baseline_item_id=max_item,inventory_completed_at=last)
        return self._save(now)
