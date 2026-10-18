import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from oa_knowledge.db.models import ArchiveClassificationPlan, ArchivedFile, OAItem, OAManifestItem, ParseArtifact
from oa_knowledge.source_library import classify_library_item, source_directory
from test_archive_classification import specification
from test_source_markdown_service import _seed


def setup_case(tmp_path, title, body):
    settings, engine, source_id, _ = _seed(tmp_path)
    with Session(engine) as db:
        source=db.get(ArchivedFile,source_id)
        source.original_name='合成附件.pdf'
        item=db.get(OAItem,source.oa_item_id);item.title=title
        row=OAManifestItem(oa_item_key=item.oa_item_key,workitem_id_text='-90001',title=title,
            initiated_at=item.initiated_at,list_page=1,processing_status='downloaded')
        db.add(row)
        raw=json.dumps(specification(),ensure_ascii=False)
        db.add(ArchiveClassificationPlan(version='synthetic',spec_json=raw,source_sha256=hashlib.sha256(raw.encode()).hexdigest(),active=True))
        artifact=db.scalar(select(ParseArtifact))
        path=settings.cache_root/artifact.output_relpath;path.write_text(body,encoding='utf8')
        artifact.product_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        db.commit()
    return settings,engine


def test_body_signature_beats_title_document_code(tmp_path):
    settings,engine=setup_case(tmp_path,'合集〔2026〕1号 文件','正文\n合成监管局\n2026年10月1日')
    with Session(engine) as db:
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert source_directory(result).parts[0]=='03_外部机构来文'


def test_trailing_external_code_beats_reference_attachments(tmp_path):
    settings,engine=setup_case(tmp_path,'【文件传阅】关于印发《合成指引》的通知【合监通〔2026〕16号】(由某人原发)','发文单位：合成集团\n发文单位：合成租赁')
    with Session(engine)as db:
        enable_external_routing(db)
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['source_priority']={'primary_external_document_code':True}
        spec['mappings']['external_organizations'][0]['document_code_pattern']='合监(?:干|通)?'
        plan.spec_json=json.dumps(spec,ensure_ascii=False);db.flush()
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert result['archive_agency']=='合成监管局' and not result['needs_review']


def test_combined_publications_use_first_series_even_with_annex_issuers(tmp_path):
    settings,engine=setup_case(tmp_path,'【文件传阅】合成信息第18期 合成学习选刊第3期(由某人原发)','发文单位：合成集团\n发文单位：合成监管局')
    with Session(engine)as db:
        enable_external_routing(db)
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['source_priority']={'combined_publication':'first_title_series'}
        plan.spec_json=json.dumps(spec,ensure_ascii=False);db.flush()
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='PUBLICATION' and result['archive_series']=='合成信息'
        assert result['related_publication_series']==['合成学习选刊']


def test_group_primary_document_code_resolves_annex_conflict(tmp_path):
    settings,engine=setup_case(tmp_path,'合成通知','合集〔2026〕12号-关于合成事项\n发文单位：合成监管局\n发文单位：合成租赁')
    with Session(engine)as db:
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['source_priority']={'primary_group_document_code':True}
        plan.spec_json=json.dumps(spec);db.flush()
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='GROUP' and result['archive_organization']=='合成集团'


def test_company_board_package_keeps_group_reference_documents_as_references(tmp_path):
    settings,engine=setup_case(tmp_path,'合成租赁第三届董事会会议材料','合集〔2026〕12号-关于参考规定\n发文单位：合成集团\n发文单位：合成监管局')
    with Session(engine)as db:
        enable_external_routing(db)
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['source_priority']={'primary_group_document_code':True};plan.spec_json=json.dumps(spec);db.flush()
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='SELF'


def test_company_board_title_resolves_reference_attachment_conflict(tmp_path):
    settings,engine=setup_case(tmp_path,'合成租赁第三届董事会会议材料','发文单位：合成集团\n发文单位：合成监管局')
    with Session(engine)as db:
        enable_external_routing(db)
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='SELF'


def test_group_minutes_heading_without_dated_signature(tmp_path):
    settings,engine=setup_case(tmp_path,'专题会议纪要','合成集团专题会议纪要\n〔2026〕1号\n合成会议正文')
    with Session(engine)as db:
        enable_external_routing(db)
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='GROUP'


def test_sender_affiliation_and_counterparty_do_not_determine_source(tmp_path):
    settings,engine=setup_case(tmp_path,'关于与合成集团合作事项','客户提到合成集团董事会会议材料。')
    with Session(engine)as db:
        enable_external_routing(db)
        row=db.scalar(select(OAManifestItem));row.sender='合成集团'
        assert classify_library_item(db,settings,row)['archive_category_id']=='UNKNOWN'


def test_learning_title_requires_real_study_text(tmp_path):
    settings,engine=setup_case(tmp_path,'第一议题学习材料','签到表')
    with Session(engine)as db:
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['learning_materials']={'series':'合成学习资料','title_pattern':'第一议题学习材料','body_pattern':'合成理论正文'}
        plan.spec_json=json.dumps(spec);db.flush()
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='UNKNOWN'
        artifact=db.scalar(select(ParseArtifact));path=settings.cache_root/artifact.output_relpath
        path.write_text('合成理论正文'*100,encoding='utf8');artifact.product_sha256=hashlib.sha256(path.read_bytes()).hexdigest();db.flush()
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='PUBLICATION'


def enable_external_routing(db):
    plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
    spec['external_routing']={'enabled':True,'transmission_fallback':True,'agency_labels':{'合成监管局':'合成监管局'}}
    plan.spec_json=json.dumps(spec,ensure_ascii=False);db.flush()


def test_external_directory_contains_region_and_agency(tmp_path):
    settings,engine=setup_case(tmp_path,'合监〔2026〕1号 关于合成通知','合监〔2026〕1号')
    with Session(engine) as db:
        enable_external_routing(db)
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert source_directory(result).parts[1:3]==('其他机构','合成监管局')


def test_transmission_routes_unknown_to_external_with_agency_review(tmp_path):
    settings,engine=setup_case(tmp_path,'【文件传阅】关于合成活动的通知','无机构信息')
    with Session(engine) as db:
        enable_external_routing(db)
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert result['archive_agency']=='机构待确认' and result['needs_review']


def test_transmission_does_not_override_known_group(tmp_path):
    settings,engine=setup_case(tmp_path,'【文件传阅】合集〔2026〕1号 关于合成事项','合集〔2026〕1号')
    with Session(engine) as db:
        enable_external_routing(db)
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='GROUP'


def test_original_sender_routes_unknown_but_preserves_specific_source(tmp_path):
    settings,engine=setup_case(tmp_path,'关于合成活动的通知（由某人原发）','无机构信息')
    with Session(engine)as db:
        enable_external_routing(db)
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['external_routing']['original_sender_fallback']=True
        plan.spec_json=json.dumps(spec);db.flush()
        row=db.scalar(select(OAManifestItem))
        result=classify_library_item(db,settings,row)
        assert result['archive_category_id']=='EXTERNAL' and result['needs_review']
        row.title='合成集团关于合成活动的通知(由某人原发)';db.flush()
        assert classify_library_item(db,settings,row)['archive_category_id']=='GROUP'


def test_external_center_heading_resolves_agency(tmp_path):
    settings,engine=setup_case(tmp_path,'广州市合成网络通报中心关于合成预警的通报(由某人原发)','合成预警正文')
    with Session(engine)as db:
        enable_external_routing(db)
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert result['archive_agency']=='合成网络通报中心'


def test_generic_government_signature_with_annex_resolves_agency(tmp_path):
    settings,engine=setup_case(tmp_path,'关于合成事项的通知','合成正文\n广州市黄埔区合成事务局\n2026 年 10 月 1 日\n附件\n'+'合成表格\n'*30)
    with Session(engine) as db:
        enable_external_routing(db)
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert result['archive_region']=='黄埔区' and result['archive_agency']=='合成事务局'
        assert result['source_organization_verified']


def test_cited_government_department_is_not_issuer(tmp_path):
    settings,engine=setup_case(tmp_path,'合成材料','根据广州市黄埔区合成事务局要求执行。')
    with Session(engine) as db:
        enable_external_routing(db)
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='UNKNOWN'


def test_group_party_office_is_not_government_agency(tmp_path):
    settings,engine=setup_case(tmp_path,'【文件传阅】关于合成事项的通知','中共合成集团委员会\n2026 年 10 月 1 日')
    with Session(engine) as db:
        enable_external_routing(db)
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='GROUP'


def test_company_with_geographical_prefix_is_not_government(tmp_path):
    settings,engine=setup_case(tmp_path,'合成材料','广州开发区合成集团有限公司办公室\n2026 年 10 月 1 日')
    with Session(engine) as db:
        enable_external_routing(db)
        assert classify_library_item(db,settings,db.scalar(select(OAManifestItem)))['archive_category_id']=='UNKNOWN'


def test_signature_before_long_annex_is_recognized(tmp_path):
    settings,engine=setup_case(tmp_path,'合成通知','正文\n合成监管局\n2026 年 10 月 1 日\n附件\n'+'合成表格\n'*80)
    with Session(engine) as db:
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'
        assert result['classification_source']=='body_issuer_label'


def test_directory_uses_original_title_date_and_collision_number(tmp_path):
    from datetime import datetime
    settings,engine=setup_case(tmp_path,'关于合成材料的通知','无明确来源')
    with Session(engine) as db:
        row=db.scalar(select(OAManifestItem));row.initiated_at=datetime(2026,10,14)
        db.flush()
        first=classify_library_item(db,settings,row)
        assert source_directory(first).name=='2026-10-14_关于合成材料的通知'
        twin=OAManifestItem(oa_item_key='done:twin',workitem_id_text='-90002',title=row.title,
            initiated_at=row.initiated_at,list_page=1,processing_status='downloaded')
        db.add(twin);db.flush()
        second=classify_library_item(db,settings,twin)
        assert source_directory(second).name=='2026-10-14_关于合成材料的通知_2'


def test_attachment_only_delivery_needs_no_index(tmp_path):
    from oa_knowledge.source_library import publish_library_index
    from oa_knowledge.source_markdown.service import publish_active_artifact
    from oa_knowledge.web.delivery_facts import delivery_facts
    settings,engine=setup_case(tmp_path,'合监〔2026〕1号 关于合成材料的通知','合监〔2026〕1号\n合成正文')
    with Session(engine) as db:
        plan=db.scalar(select(ArchiveClassificationPlan));spec=json.loads(plan.spec_json)
        spec['delivery']={'attachments_only':True};plan.spec_json=json.dumps(spec)
        db.flush()
        source=db.scalar(select(ArchivedFile));record=publish_active_artifact(db,settings,source.id)
        manifest=db.scalar(select(OAManifestItem));item=db.scalar(select(OAItem))
        directory,remaining=publish_library_index(db,settings,item,manifest,classify_library_item(db,settings,manifest))
        assert remaining==0
        assert directory==(settings.workspace_root/record.markdown_relpath).parent
        assert not list(settings.markdown_root.rglob('_index.md'))
        assert delivery_facts(db,item)['status']=='complete'
        assert delivery_facts(db,item)['index_status']=='not_required'
        db.commit()
    from oa_knowledge.web.console_views import markdown_outputs_list
    page=markdown_outputs_list(settings,category='EXTERNAL')
    assert page['item_total']==1
    assert page['items'][0]['archive_category_id']=='EXTERNAL'
    assert markdown_outputs_list(settings,category='SELF')['item_total']==0


def test_company_project_keeps_external_contracts_in_company_item(tmp_path):
    settings,engine=setup_case(tmp_path,'客户直租项目风险评审报告','发文单位：合成监管局\n合成合同附件')
    with Session(engine) as db:
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='SELF'
        assert len(source_directory(result).parts)==3


def test_formal_body_code_enriches_unknown_oa_heading(tmp_path):
    settings,engine=setup_case(tmp_path,'合成材料','合监〔2026〕1号\n关于合成任职的通知')
    with Session(engine) as db:
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='EXTERNAL'


def test_citation_and_sender_do_not_establish_source(tmp_path):
    settings,engine=setup_case(tmp_path,'合成材料','根据合监〔2026〕1号执行。正文提到合成监管局。')
    with Session(engine) as db:
        row=db.scalar(select(OAManifestItem));row.sender='合成监管局'
        result=classify_library_item(db,settings,row)
        assert result['archive_category_id']=='UNKNOWN'
        assert source_directory(result).parts[0]=='05_待确认'


def test_wrong_product_hash_is_not_source_evidence(tmp_path):
    settings,engine=setup_case(tmp_path,'合成材料','发文单位：合成监管局')
    with Session(engine) as db:
        db.scalar(select(ParseArtifact)).product_sha256='0'*64
        result=classify_library_item(db,settings,db.scalar(select(OAManifestItem)))
        assert result['archive_category_id']=='UNKNOWN'


def test_publication_uses_source_directory_and_types_only_as_tags(tmp_path):
    import yaml
    from oa_knowledge.source_markdown.service import publish_active_artifact
    from oa_knowledge.source_library import publish_library_index
    settings,engine=setup_case(tmp_path,'合监〔2026〕1号 关于合成人员任职的通知','合监〔2026〕1号\n关于合成人员任职的通知')
    with Session(engine) as db:
        source=db.scalar(select(ArchivedFile));source.original_name='关于合成人员任职的通知.pdf'
        original=settings.data_root/source.local_relpath;before=original.read_bytes()
        record=publish_active_artifact(db,settings,source.id)
        assert record.markdown_relpath.startswith('03_外部机构来文/')
        assert '任免通知/' not in record.markdown_relpath
        content=(settings.workspace_root/record.markdown_relpath).read_text(encoding='utf8')
        metadata=yaml.safe_load(content[4:].split('\n---\n',1)[0])
        assert metadata['tags']==['任免通知']
        manifest=db.scalar(select(OAManifestItem));item=db.scalar(select(OAItem))
        decision=classify_library_item(db,settings,manifest)
        index,remaining=publish_library_index(db,settings,item,manifest,decision)
        assert index.parent==(settings.workspace_root/record.markdown_relpath).parent
        assert remaining==0
        assert original.read_bytes()==before


def test_pipeline_uses_five_sources_without_legacy_private_configuration(tmp_path):
    from types import SimpleNamespace
    from oa_knowledge.web.worker import OperationWorker
    settings,engine=setup_case(tmp_path,'合成材料','无明确来源的合成正文')
    calls=[]
    queue=SimpleNamespace(advance=lambda *args,**kwargs:calls.append(args),complete=lambda *args:None)
    worker=SimpleNamespace(settings=settings,engine=engine,owner='synthetic-worker',production_queue=queue)
    task=SimpleNamespace(id=90001,logical_item_key='done:synthetic')
    OperationWorker._pipeline_classify(worker,task)
    assert calls==[(90001,'synthetic-worker','index_publish')]


def test_index_escapes_literal_filename_markdown(tmp_path):
    from oa_knowledge.source_library import publish_library_index
    settings,engine=setup_case(tmp_path,'合成材料','无明确来源的合成正文')
    with Session(engine) as db:
        source=db.scalar(select(ArchivedFile));source.original_name='合成材料[1](1).pdf'
        item=db.scalar(select(OAItem));manifest=db.scalar(select(OAManifestItem))
        decision=classify_library_item(db,settings,manifest)
        index,_=publish_library_index(db,settings,item,manifest,decision)
        assert r'\[1\](1)' in index.read_text(encoding='utf8')
