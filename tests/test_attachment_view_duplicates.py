from dataclasses import replace
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy import select
from oa_knowledge.collector.detail import DirectAttachment, DetailCapture, deduplicate_attachment_views
from oa_knowledge.detail_archive import archive_collaboration_detail
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, BatchItem, CollectionBatch


def attachment(key, role='direct_attachment', content=b'%PDF-1.4\nsynthetic\n%%EOF', name='synthetic.pdf'):
    return DirectAttachment(key, name, None, len(content) if content else None, file_role=role, content=content, download_status='downloaded' if content else 'download_failed')


def test_widget_panel_keep_real_identity_and_body_role():
    merged=deduplicate_attachment_views([attachment('cap4-widget:synthetic','official_body'),attachment('-9001')])
    assert len(merged)==1 and merged[0].attachment_key=='-9001' and merged[0].file_role=='official_body'
    assert len(deduplicate_attachment_views(merged))==1
    spaced=deduplicate_attachment_views([attachment('cap4-widget:spaced',name='synthetic document.pdf'),attachment('-9001',name='synthetic\u00a0document.pdf')])
    assert len(spaced)==1
    truncated=deduplicate_attachment_views([attachment('cap4-widget:full','official_body',name='synthetic complete document.pdf'),attachment('-9001',name='synthetic...pdf')])
    assert len(truncated)==1 and truncated[0].filename=='synthetic complete document.pdf'


def test_different_real_ids_names_contents_and_failures_are_preserved():
    assert len(deduplicate_attachment_views([attachment('-9001'),attachment('-9002')]))==2
    assert len(deduplicate_attachment_views([attachment('cap4-widget:synthetic'),attachment('-9001',name='other.pdf')]))==2
    assert len(deduplicate_attachment_views([attachment('cap4-widget:synthetic'),attachment('-9001',content=b'other')]))==2
    assert len(deduplicate_attachment_views([attachment('cap4-widget:synthetic',content=None),attachment('-9001')]))==2
    assert len(deduplicate_attachment_views([attachment('cap4-widget:first'),attachment('cap4-widget:second')]))==2


def test_display_size_badge_does_not_create_a_second_attachment():
    merged=deduplicate_attachment_views([
        attachment('-9001', name='synthetic doc....pdf'),
        attachment('cap4-widget:badge', 'official_body', name='synthetic document.pdf (1M)'),
    ])
    assert len(merged)==1
    assert merged[0].attachment_key=='-9001'
    assert merged[0].filename=='synthetic document.pdf'
    assert merged[0].file_role=='official_body'


def test_exact_full_name_beats_ambiguous_truncated_name():
    merged=deduplicate_attachment_views([
        attachment('-9001',name='synthetic complete document.pdf'),
        attachment('-9002',name='synthetic....pdf'),
        attachment('cap4-widget:badge','official_body',name='synthetic complete document.pdf (4M)'),
    ])
    assert len(merged)==2
    assert {a.attachment_key for a in merged}=={'-9001','-9002'}
    assert next(a for a in merged if a.attachment_key=='-9001').file_role=='official_body'
    ambiguous=deduplicate_attachment_views([
        attachment('-9001'),attachment('-9002'),attachment('cap4-widget:unclear')])
    assert len(ambiguous)==3


def test_archive_repeated_capture_writes_one_physical_file(tmp_path):
    engine=create_db_engine(tmp_path/'oa.db');upgrade_database(tmp_path/'oa.db')
    with Session(engine) as session:
        batch=CollectionBatch(batch_key='synthetic-duplicates',source_channel='done',window_field='completed_at',planned_limit=1,status='running',plan_hash='a'*64)
        session.add(batch);session.flush()
        item=BatchItem(batch_id=batch.id,oa_item_key='done:-9001',workitem_id_text='-9001',title='synthetic duplicates',ordinal=1,created_at=datetime(2026,9,1))
        session.add(item);session.flush()
        capture=DetailCapture('https://oa.invalid/detail','collaboration',(),(),(attachment('cap4-widget:synthetic','official_body'),attachment('-9001')))
        for _ in range(2):
            manifest=archive_collaboration_detail(session,item,capture,tmp_path);session.commit()
            assert len(manifest.containers[0].files)==1
        panel_only=replace(capture,attachments=(attachment('-9001'),))
        archive_collaboration_detail(session,item,panel_only,tmp_path);session.commit()
        assert len(list(session.scalars(select(ArchivedFile))))==1
        assert session.scalar(select(ArchivedFile)).file_role=='official_body'
        assert len(list((tmp_path/'originals').rglob('*.pdf')))==1
        renamed=replace(capture,attachments=(attachment('-9001',name='synthetic complete name.pdf'),))
        archive_collaboration_detail(session,item,renamed,tmp_path);session.commit()
        assert len(list(session.scalars(select(ArchivedFile))))==1
        assert len(list((tmp_path/'originals').rglob('*.pdf')))==1
    engine.dispose()


def test_separate_widget_and_canonical_captures_reuse_identity_and_path(tmp_path):
    engine=create_db_engine(tmp_path/'oa.db');upgrade_database(tmp_path/'oa.db')
    with Session(engine) as session:
        batch=CollectionBatch(batch_key='synthetic-cross-run',source_channel='done',window_field='completed_at',planned_limit=1,status='running',plan_hash='b'*64)
        session.add(batch);session.flush()
        item=BatchItem(batch_id=batch.id,oa_item_key='done:-9002',workitem_id_text='-9002',title='synthetic cross run',ordinal=1,created_at=datetime(2026,9,1))
        session.add(item);session.flush()
        def capture(att):
            return DetailCapture('https://oa.invalid/detail','collaboration',(),(),(att,))
        archive_collaboration_detail(session,item,capture(attachment('cap4-widget:one','official_body',name='synthetic document.pdf (1M)')),tmp_path);session.commit()
        original_path=session.scalar(select(ArchivedFile)).local_relpath
        archive_collaboration_detail(session,item,capture(attachment('-9002',name='synthetic doc....pdf')),tmp_path);session.commit()
        archive_collaboration_detail(session,item,capture(attachment('cap4-widget:two','official_body',name='synthetic document.pdf (1MB)')),tmp_path);session.commit()
        files=list(session.scalars(select(ArchivedFile)))
        assert len(files)==1 and files[0].attachment_key=='-9002'
        assert files[0].local_relpath==original_path
        assert files[0].file_role=='official_body'
        assert len(list((tmp_path/'originals').rglob('*.pdf')))==1
        # Equal bytes and names with distinct real IDs are not aliases.
        archive_collaboration_detail(session,item,capture(attachment('-9003',name='synthetic document.pdf')),tmp_path);session.commit()
        assert len(list(session.scalars(select(ArchivedFile))))==2
    engine.dispose()
