from __future__ import annotations
from datetime import datetime, timedelta, timezone
import os
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, MarkdownQueueControl, MarkdownTask, MarkdownTaskEvent, OAItem
from oa_knowledge.markdown_export.render import SCHEMA_VERSION
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES

# Kept for backward compatibility with callers importing the old names.
ATTACHMENT_ROLES = MARKDOWN_SOURCE_ROLES
PDF_CAMPAIGN_ROLES = MARKDOWN_SOURCE_ROLES

def enqueue_file(session: Session, source_file_id: int) -> bool:
    done = session.scalar(select(MarkdownExport.id).where(MarkdownExport.source_file_id == source_file_id, MarkdownExport.schema_version == SCHEMA_VERSION, MarkdownExport.status.in_(("success", "unsupported"))))
    exists = session.scalar(select(MarkdownTask.id).where(MarkdownTask.source_file_id == source_file_id, MarkdownTask.schema_version == SCHEMA_VERSION))
    if done or exists: return False
    session.add(MarkdownTask(source_file_id=source_file_id, schema_version=SCHEMA_VERSION, status="queued"))
    session.flush(); return True

def enqueue_verified_for_oa(session: Session, oa_item_key: str) -> int:
    ids = session.scalars(select(ArchivedFile.id).join(OAItem).where(OAItem.oa_item_key == oa_item_key, ArchivedFile.file_role.in_(ATTACHMENT_ROLES), ArchivedFile.download_status == "verified", ArchivedFile.local_relpath.is_not(None))).all()
    return sum(enqueue_file(session, file_id) for file_id in ids)

def exclude_non_attachment_tasks(settings: Settings) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s:
            count=s.execute(update(MarkdownTask).where(MarkdownTask.status=="queued", MarkdownTask.source_file_id.in_(select(ArchivedFile.id).where(ArchivedFile.file_role.not_in(ATTACHMENT_ROLES)))).values(status="excluded", lease_owner=None, lease_expires_at=None)).rowcount
            s.add(MarkdownTaskEvent(event_type="non_attachments_excluded", message=f"已从附件队列排除 {count} 个非附件快照")); s.commit(); return {"excluded":count}
    finally: engine.dispose()

def _control(session: Session) -> MarkdownQueueControl:
    row = session.get(MarkdownQueueControl, 1)
    if not row: row = MarkdownQueueControl(id=1, paused=False); session.add(row); session.flush()
    return row

def start_pdf_mineru_campaign(settings: Settings) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s:
            files=s.scalars(select(ArchivedFile).where(ArchivedFile.file_role.in_(PDF_CAMPAIGN_ROLES),ArchivedFile.download_status=="verified",ArchivedFile.local_relpath.is_not(None))).all()
            pdfs=[f for f in files if f.local_relpath and (settings.data_root/f.local_relpath).suffix.lower()==".pdf"]
            queued=skipped=0
            for f in pdfs:
                done=s.scalar(select(MarkdownExport.id).where(MarkdownExport.source_file_id==f.id,MarkdownExport.schema_version==SCHEMA_VERSION,MarkdownExport.status=="success",MarkdownExport.parse_engine=="mineru"))
                if done: skipped+=1; continue
                task=s.scalar(select(MarkdownTask).where(MarkdownTask.source_file_id==f.id,MarkdownTask.schema_version==SCHEMA_VERSION))
                if not task: task=MarkdownTask(source_file_id=f.id,schema_version=SCHEMA_VERSION,status="queued");s.add(task)
                if task.status!="running": task.status="queued";task.last_error_code=None;task.lease_owner=None;task.lease_expires_at=None
                task.requested_engine="mineru";task.campaign="pdf_mineru";queued+=1
            _control(s).pdf_mineru_paused=False
            s.add(MarkdownTaskEvent(event_type="pdf_mineru_started",message=f"PDF MinerU 重转已启动：目标 {queued}，跳过 {skipped}"));s.commit()
            return {"total":len(pdfs),"queued":queued,"skipped":skipped}
    finally: engine.dispose()

def set_pdf_mineru_paused(settings: Settings, paused: bool) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s: _control(s).pdf_mineru_paused=paused;s.add(MarkdownTaskEvent(event_type="pdf_mineru_paused" if paused else "pdf_mineru_resumed",message="PDF MinerU 重转已暂停" if paused else "PDF MinerU 重转已继续"));s.commit()
    finally: engine.dispose()
    return {"paused":paused}

def retry_pdf_mineru_failed(settings: Settings) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s:
            count=s.execute(update(MarkdownTask).where(MarkdownTask.campaign=="pdf_mineru",MarkdownTask.status=="failed").values(status="queued",last_error_code=None,lease_owner=None,lease_expires_at=None)).rowcount;s.commit();return {"retried":count}
    finally: engine.dispose()

def pdf_mineru_view(session: Session, settings: Settings) -> dict:
    files=session.scalars(select(ArchivedFile).where(ArchivedFile.file_role.in_(PDF_CAMPAIGN_ROLES),ArchivedFile.download_status=="verified",ArchivedFile.local_relpath.is_not(None))).all()
    pdf_ids=[f.id for f in files if f.local_relpath and (settings.data_root/f.local_relpath).suffix.lower()==".pdf"]
    statuses=dict(session.execute(select(MarkdownTask.status,func.count()).where(MarkdownTask.campaign=="pdf_mineru").group_by(MarkdownTask.status)).all())
    success=session.scalar(select(func.count()).select_from(MarkdownExport).where(MarkdownExport.source_file_id.in_(pdf_ids),MarkdownExport.schema_version==SCHEMA_VERSION,MarkdownExport.status=="success",MarkdownExport.parse_engine=="mineru")) if pdf_ids else 0
    campaign_total=sum(statuses.values()); skipped=max(len(pdf_ids)-campaign_total,0)
    return {"paused":_control(session).pdf_mineru_paused,"total":len(pdf_ids),"queued":statuses.get("queued",0),"running":statuses.get("running",0),"succeeded":success or 0,"failed":statuses.get("failed",0),"skipped":skipped}

def set_paused(settings: Settings, paused: bool) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s: _control(s).paused=paused; s.add(MarkdownTaskEvent(event_type="queue_paused" if paused else "queue_resumed", message="MD 化已暂停" if paused else "MD 化已继续")); s.commit()
    finally: engine.dispose()
    return {"paused": paused}

def pause_queue(settings): return set_paused(settings, True)
def resume_queue(settings): return set_paused(settings, False)

def retry_failed(settings: Settings) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s:
            count=s.execute(update(MarkdownTask).where(MarkdownTask.status=="failed").values(status="queued", last_error_code=None, lease_owner=None, lease_expires_at=None)).rowcount
            s.add(MarkdownTaskEvent(event_type="failed_retried", message=f"已重新排队 {count} 个失败任务")); s.commit(); return {"retried": count}
    finally: engine.dispose()

def queue_view(settings: Settings) -> dict:
    engine=create_db_engine(settings.database_path)
    try:
        with Session(engine) as s:
            statuses=dict(s.execute(select(MarkdownTask.status, func.count()).group_by(MarkdownTask.status)).all())
            events=s.scalars(select(MarkdownTaskEvent).order_by(MarkdownTaskEvent.id.desc()).limit(100)).all()
            return {"paused": _control(s).paused, "discovered": sum(statuses.values())-statuses.get("excluded",0), "queued": statuses.get("queued",0), "running": statuses.get("running",0), "succeeded": statuses.get("succeeded",0), "failed": statuses.get("failed",0), "excluded":statuses.get("excluded",0), "pdf_mineru":pdf_mineru_view(s,settings), "events":[{"id":e.id,"event_type":e.event_type,"level":e.level,"message":e.message,"created_at":e.created_at.isoformat() if e.created_at else None} for e in events]}
    finally: engine.dispose()

def claim(session: Session, owner: str) -> int | None:
    control=_control(session)
    now=datetime.now(timezone.utc)
    expired=session.scalars(select(MarkdownTask).where(MarkdownTask.status=="running", (MarkdownTask.lease_expires_at < now) | (MarkdownTask.lease_owner != owner))).all()
    for row in expired: row.status="queued"; row.lease_owner=None
    task=None
    if not control.pdf_mineru_paused:
        task=session.scalar(select(MarkdownTask).where(MarkdownTask.status=="queued",MarkdownTask.campaign=="pdf_mineru").order_by(MarkdownTask.id).limit(1))
    if task is None and not control.paused:
        task=session.scalar(select(MarkdownTask).where(MarkdownTask.status=="queued",MarkdownTask.campaign!="pdf_mineru").order_by(MarkdownTask.id).limit(1))
    if task:
        task.status="running"; task.lease_owner=owner; task.lease_expires_at=now+timedelta(minutes=10); task.started_at=now; task.attempts+=1
        task_id=task.id; session.commit(); return task_id
    return None
