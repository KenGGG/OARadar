from __future__ import annotations
from datetime import datetime, timezone
import os, time
from sqlalchemy.orm import Session
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import MarkdownTask, MarkdownTaskEvent
from oa_knowledge.markdown_export.service import convert_file_id
from oa_knowledge.markdown_queue import claim

class MarkdownWorker:
    def __init__(self, settings: Settings): self.settings=settings; self.engine=create_db_engine(settings.database_path); self.owner=f"markdown-worker-{os.getpid()}"
    def close(self): self.engine.dispose()
    def run_once(self):
        with Session(self.engine) as s: task_id=claim(s,self.owner)
        if not task_id: return False
        started=time.monotonic()
        try:
          with Session(self.engine) as s:
            row=s.get(MarkdownTask,task_id)
            try:
                result=convert_file_id(s,self.settings,row.source_file_id,engine=row.requested_engine,force=bool(row.requested_engine))
                row.status="succeeded" if result.success or result.unsupported else "failed"
                row.last_error_code=None if row.status=="succeeded" else "CONVERSION_FAILED"
            except Exception as exc:
                row.status="failed"; row.last_error_code=type(exc).__name__.upper()
            row.elapsed_seconds=round(time.monotonic()-started,3); row.finished_at=datetime.now(timezone.utc); row.lease_owner=None; row.lease_expires_at=None
            s.add(MarkdownTaskEvent(task_id=row.id,event_type="conversion_completed" if row.status=="succeeded" else "conversion_failed",level="info" if row.status=="succeeded" else "error",message="MD 化完成" if row.status=="succeeded" else "MD 化失败")); s.commit()
        except Exception as exc:
            with Session(self.engine) as s:
                row=s.get(MarkdownTask,task_id)
                if row:
                    row.status="failed"; row.last_error_code=type(exc).__name__.upper(); row.finished_at=datetime.now(timezone.utc); row.lease_owner=None; row.lease_expires_at=None
                    s.add(MarkdownTaskEvent(task_id=row.id,event_type="conversion_transaction_failed",level="error",message="MD 化事务失败")); s.commit()
        return True
