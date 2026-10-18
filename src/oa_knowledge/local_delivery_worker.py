"""Fresh-code local attachment delivery, isolated from legacy OA consumers."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import threading
from sqlalchemy import select, text, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from oa_knowledge.config import load_settings
from oa_knowledge.db.models import PipelineTask
from oa_knowledge.platform_lock import lock_exclusive
from oa_knowledge.web.worker import OperationWorker
from oa_knowledge.web.local_conversion import process_running
from oa_knowledge.archive.writer import atomic_write_bytes
import json


def run(config):
    settings = load_settings(config)
    with (settings.runtime_root/'local-delivery.lock').open('a+') as lock:
        try:lock_exclusive(lock.fileno())
        except BlockingIOError:return
        worker = OperationWorker(settings, config_path=config)
        atomic_write_bytes(json.dumps({'pid':os.getpid()}).encode(),settings.runtime_root,'local-delivery-worker.json')
        try:
            while True:
                try:
                    with Session(worker.engine) as db:
                        db.execute(text('BEGIN IMMEDIATE'))
                        # Recover only this consumer's tasks, never steal live work.
                        for row in db.scalars(select(PipelineTask).where(PipelineTask.status=='running',PipelineTask.payload_json.like('%"local_delivery": true%'))):
                            try:alive=process_running(int((row.lease_owner or '').removeprefix('worker-')))
                            except ValueError:alive=None
                            if alive is False:row.status='local_queued'
                        task = db.scalar(select(PipelineTask).where(PipelineTask.status=='local_queued',
                            or_(PipelineTask.next_retry_at.is_(None),PipelineTask.next_retry_at<=datetime.now(timezone.utc))).order_by(PipelineTask.id))
                        if task is not None:
                            task.status='running';task.lease_owner=worker.owner
                            task.lease_expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)
                            task.started_at=task.started_at or datetime.now(timezone.utc);task.attempts+=1
                            db.commit();db.refresh(task);db.expunge(task)
                        else:db.commit()
                    if task is not None:
                        worker.settings=load_settings(config)
                        worker._execute_pipeline_task(task)
                    else:threading.Event().wait(2)
                except OperationalError:threading.Event().wait(2)
        finally:worker.engine.dispose()


if __name__=='__main__':run(Path(sys.argv[1]).resolve())
