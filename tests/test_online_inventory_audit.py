import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.collector.detail import DirectAttachment, DetailCapture
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, OAManifestItem, OnlineAuditItem, OnlineAuditRun, OperationJob, PipelineTask
from oa_knowledge.online_audit import AuditObservation, audit_view, execute_audit, observe_inventory_capture, resume_audit, start_audit


def test_inventory_reads_ui_entries_without_file_bodies():
    capture = DetailCapture('https://synthetic.invalid/detail','collaboration',(),(),(
        DirectAttachment('-1','synthetic.pdf',None,None),
        DirectAttachment('cap4-widget:test','synthetic.pdf (1M)',None,None),
    ))
    observation = observe_inventory_capture(capture)
    assert observation.inventory_only and observation.recognized_attachments == 1
    assert observation.attachment_evidence[0][1] == '-1'
    assert observation.online_content_sha256 is None


def test_inventory_does_not_hide_extra_downloads_by_equal_hash(config_file):
    settings=load_settings(config_file);upgrade_database(settings.database_path)
    engine=create_db_engine(settings.database_path)
    payload=b'%PDF synthetic';digest=hashlib.sha256(payload).hexdigest()
    with Session(engine) as db:
        db.add_all([OAManifestItem(oa_item_key='done:1',workitem_id_text='1',title='synthetic',list_page=1),
                    OAManifestItem(oa_item_key='done:2',workitem_id_text='2',title='excluded',list_page=1,
                                   processing_status='skipped',matched_exclusion_keyword='manual:test')])
        item=OAItem(oa_item_key='done:1',workitem_id_text='1',title='synthetic',source_channel='done')
        db.add(item);db.flush()
        for key in ['-1','-2']:
            rel='originals/synthetic/'+key+'.pdf';path=settings.data_root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
            db.add(ArchivedFile(oa_item_id=item.id,attachment_key=key,original_name='synthetic.pdf',file_role='direct_attachment',source_container_key='collaboration:1',depth=1,download_status='verified',sha256=digest,size_bytes=len(payload),local_relpath=rel))
        db.commit()
    run=start_audit(settings,inventory_only=True)['run_id']
    def inspect(item):
        return AuditObservation(1,attachment_evidence=(('direct_attachment','-1',None,None),),inventory_only=True)
    execute_audit(settings,run,inspect_item=inspect,max_items=1)
    execute_audit(settings,run,inspect_item=inspect)
    with Session(engine) as db:
        rows={i.oa_item_key:i for i in db.scalars(select(OnlineAuditItem).where(OnlineAuditItem.run_id==run))}
        assert rows['done:1'].downloaded_attachments==2
        assert rows['done:1'].status=='historical_retained'
        assert rows['done:1'].comparison_reason.startswith('inventory_only:')
        assert rows['done:2'].comparison_reason=='inventory_only:excluded_from_download'
        assert not list(db.scalars(select(PipelineTask)))
    engine.dispose()


def test_resume_failed_audit_keeps_completed_results(config_file):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as db:
        db.add(OAManifestItem(oa_item_key='done:resume', workitem_id_text='resume', title='synthetic', list_page=1))
        db.commit()
    run_id = start_audit(settings, inventory_only=True)['run_id']
    with Session(engine) as db:
        run = db.get(OnlineAuditRun, run_id)
        run.status = 'failed'
        db.get(OperationJob, run.job_id).status = 'failed'
        row = db.scalar(select(OnlineAuditItem))
        row.status = 'matched'
        db.commit()
    assert resume_audit(settings, run_id)['run_id'] == run_id
    with Session(engine) as db:
        assert db.scalar(select(OnlineAuditItem)).status == 'matched'
        assert db.get(OnlineAuditRun, run_id).status == 'queued'
    engine.dispose()


def test_inventory_depth_limit_never_matches(config_file):
    settings=load_settings(config_file);upgrade_database(settings.database_path);engine=create_db_engine(settings.database_path)
    with Session(engine) as db:
        db.add(OAManifestItem(oa_item_key='done:depth',workitem_id_text='depth',title='synthetic',list_page=1));db.commit()
    run=start_audit(settings,inventory_only=True)['run_id']
    execute_audit(settings,run,inspect_item=lambda _:AuditObservation(0,depth_limit_reached=True,attachment_evidence=(),inventory_only=True))
    with Session(engine) as db:
        assert db.scalar(select(OnlineAuditItem)).status=='depth_limit_reached'
    engine.dispose()


def test_overview_review_filter_excludes_policy_skips_and_links_archive(config_file):
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as db:
        for key in ('review', 'excluded', 'access', 'missing', 'history'):
            db.add(OAManifestItem(oa_item_key=f'done:{key}', workitem_id_text=key, title='synthetic', list_page=1))
        archive = OAItem(oa_item_key='done:review', workitem_id_text='review', title='synthetic', source_channel='done')
        db.add(archive)
        db.commit()
        archive_id = archive.id
    run = start_audit(settings, inventory_only=True)['run_id']
    with Session(engine) as db:
        rows = {row.workitem_id_text: row for row in db.scalars(select(OnlineAuditItem))}
        rows['review'].status = 'content_unverified'
        rows['excluded'].status = 'content_unverified'
        rows['excluded'].comparison_reason = 'inventory_only:excluded_from_download'
        rows['access'].status = 'access_failed'
        rows['missing'].status = 'missing_download'
        rows['history'].status = 'historical_retained'
        db.commit()
    result = audit_view(settings, run, item_filter='review', item_page_size=10)
    assert result['item_pagination']['total'] == 1
    assert result['items'][0]['archive_item_id'] == archive_id
    assert result['excluded_items'] == 1
    assert result['status_counts']['content_unverified'] == 2
    for category in ('access_failed', 'missing_download', 'historical_retained'):
        result = audit_view(settings, run, item_filter=category, item_page_size=10)
        assert result['item_pagination']['total'] == 1
        assert result['items'][0]['status'] == category
    engine.dispose()
