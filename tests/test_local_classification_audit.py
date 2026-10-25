"""Synthetic-only checks for the read-only review exporter."""
import csv
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('local_audit', Path(__file__).parents[1] / 'tools/local_classification_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_path_cannot_escape(tmp_path):
    for path in ('../private.md', str(tmp_path / 'absolute.md'), ''):
        with pytest.raises(ValueError):
            audit.safe_path(tmp_path, path)


def test_quote_can_only_recover_whitespace_changes():
    assert audit.exact_quote('合成单位发布年度经营报告', '合成单位\n发布年度经营报告') == '合成单位\n发布年度经营报告'
    assert audit.exact_quote('合成单位发布年度经营报告', '合成单位发布年度财务报告') == ''
    assert audit.exact_quote('单位', '合成单位发布报告') == ''


def test_internal_form_locks_origin_but_keeps_business_topic(monkeypatch):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    item = {'title':'合成客户监管户资金划转申请 内部事项呈批表(合成人员 2023-01-01 08:45)'}
    result = audit.apply_source_constraint(item, {'relation':'外部机构','issuer':'合成客户',
        'reason':'错误地按客户判断来源', 'topic':'财务资金'})
    assert result['relation'] == '本公司'
    assert result['issuer'] == '合成甲租赁'
    assert result['topic'] == '财务资金'
    assert result['model_origin_before_constraint']['issuer'] == '合成客户'


@pytest.mark.parametrize('title', ['关于《内部事项呈批表》的通知', '转发内部事项呈批表有关规定的通知', '《内部事项呈批表》'])
def test_quoted_form_name_does_not_force_internal_origin(monkeypatch, title):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    assert audit.source_constraint({'title':title}) is None


def test_internal_flow_prefers_own_form_over_external_attachment(monkeypatch):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    item = {'title':'合成事项 内部事项呈批表', 'exports':[
        {'id':1,'status':'success','source_role':'official_body','source_name':'外部通知.pdf','markdown_relpath':'synthetic/external.md'},
        {'id':2,'status':'success','source_role':'direct_attachment','source_name':'内部事项呈批表.doc','markdown_relpath':'synthetic/form.md'}]}
    ranked, _, _ = audit.primary_candidates(item)
    assert ranked[0]['id'] == 2


def test_model_cannot_reintroduce_customer_as_internal_form_issuer(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    monkeypatch.setattr(audit, 'chat', lambda *a, **kw: {
        'relation':'外部机构','issuer':'合成客户','topic':'财务资金','confidence':.95,
        'reason':'把客户误判为发文机构','evidence_quote':'合成客户监管户资金划转申请','needs_review':False})
    result = audit.classify({'title':'资金划转申请 内部事项呈批表','document_number':''},
        [{'path':'markdown/synthetic.md','text':'合成客户监管户资金划转申请'}], tmp_path)
    assert result['issuer'] == '合成甲租赁'
    assert result['suggested_directory'] == 'markdown/01_本公司资料/财务资金'
    assert result['origin_decision_source'] == 'oa_internal_form_rule'


def test_unknown_main_does_not_erase_known_form_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    monkeypatch.setattr(audit, 'primary_candidates', lambda item: ([], '主文未确定', True))
    result, docs, issues, _, _ = audit.classify_primary({'title':'合成事项 内部事项呈批表'}, tmp_path)
    assert result['issuer'] == '合成甲租赁'
    assert result['topic'] == '待确认'
    assert result['source_only'] is True
    assert result['confidence'] is None
    assert docs == []


@pytest.mark.parametrize('title,topic', [
    ('立项申请书(合成人员 2020-01-03 14:28)', '业务项目'),
    ('立项申请书(合成人员   2020-01-12 09:07)', '业务项目'),
    ('内部事项呈批表-关于采购一批合成纪念品的请示(合成人员 2023-01-09 11:04)', '人事行政与党群'),
])
def test_form_prefix_and_initiation_are_company_material(monkeypatch, title, topic):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    result = audit.source_only_result({'title':title})
    assert result['relation'] == '本公司'
    assert result['issuer'] == '合成甲租赁'
    assert result['topic'] == topic
    assert result['suggested_directory'] == 'markdown/01_本公司资料/' + topic


@pytest.mark.parametrize('title', ['关于《立项申请书》的通知', '立项申请书填写说明', '关于内部事项呈批表的通知'])
def test_form_reference_does_not_lock_origin(monkeypatch, title):
    monkeypatch.setattr(audit, 'IDENTITY', {'本公司':['合成甲租赁']})
    assert audit.source_constraint({'title':title}) is None


def test_official_body_wins_without_loading_attachments(monkeypatch):
    item = {'title': '合成通知', 'exports': [
        {'id': 1, 'status': 'success', 'source_role': 'official_attachment', 'source_name': '合成通知.pdf', 'markdown_relpath': 'synthetic/attachment.md'},
        {'id': 2, 'status': 'success', 'source_role': 'official_body', 'source_name': '主文.pdf', 'markdown_relpath': 'synthetic/main.md'}]}
    monkeypatch.setattr(audit, 'chat', lambda *a, **kw: pytest.fail('Metadata already identifies the body'))
    ranked, reason, uncertain = audit.primary_candidates(item)
    assert ranked[0]['id'] == 2
    assert '正式正文' in reason
    assert uncertain is False


def test_clear_primary_does_not_load_secondary(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'primary_candidates', lambda item: ([{'id':1},{'id':2}], '合成主文', False))
    seen=[]
    def load(item, selected):
        seen.extend(e['id'] for e in selected)
        return [{'text':'合成主体发文依据', 'path':'markdown/synthetic.md'}], []
    monkeypatch.setattr(audit, 'load_documents', load)
    monkeypatch.setattr(audit, 'classify', lambda *a: {'issuer':'合成主体', 'relation':'外部机构', 'confidence':.95})
    result, docs, issues, reason, supplement = audit.classify_primary({}, tmp_path)
    assert seen == [1]
    assert supplement == ''


def test_unclear_primary_loads_only_one_secondary(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'primary_candidates', lambda item: ([{'id':1},{'id':2},{'id':3}], '合成主文', False))
    seen=[]
    def load(item, selected):
        seen.extend(e['id'] for e in selected)
        return [{'text':'合成证据', 'scope':'主文全文'}], []
    monkeypatch.setattr(audit, 'load_documents', load)
    monkeypatch.setattr(audit, 'classify', lambda *a: {'issuer':'', 'relation':'待确认', 'confidence':.3})
    result, docs, issues, reason, supplement = audit.classify_primary({}, tmp_path)
    assert seen == [1,2]
    assert supplement


def test_selected_loading_skips_other_files_and_labels_excerpt(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    md = tmp_path / 'data/markdown/synthetic.md'
    md.parent.mkdir(parents=True)
    md.write_text('合成首段' + '合成正文' * 5000 + '合成落款', encoding='utf-8')
    selected = {'status':'success', 'markdown_relpath':'synthetic.md', 'markdown_sha256':None}
    ignored = {'status':'success', 'markdown_relpath':'missing.md', 'markdown_sha256':None}
    docs, issues = audit.load_documents({'exports':[selected,ignored]}, [selected])
    assert len(docs) == 1
    assert not issues
    assert docs[0]['full_characters'] > len(docs[0]['text'])
    assert docs[0]['text'].startswith('合成首段')
    assert docs[0]['text'].endswith('合成落款')
    assert '首10000' in docs[0]['scope']


def test_report_preserves_missing_rows_and_text_identifiers(tmp_path):
    items = [{'key': '9007199254740993999', 'workitem_id': '00123', 'title': '=SYNTHETIC()',
              'exports': [], 'original_directory': 'originals/synthetic'}]
    counts = audit.export_report(items, {}, tmp_path)
    with (tmp_path / 'OA分类对照总表.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 2
    assert rows[1][0:3] == ['9007199254740993999', '00123', "'=SYNTHETIC()"]
    assert rows[1][10] == '未处理'
    assert counts == {'未处理': 1}


def test_quote_hallucination_requires_review(tmp_path, monkeypatch):
    responses = iter([
        {'issuer': '合成机构', 'relation': '外部机构', 'topic': '财务资金',
         'evidence_quote': '不存在的证据', 'summary': '合成摘要'},
        {'issuer': '合成机构', 'relation': '外部机构', 'topic': '财务资金',
         'evidence_quote': '不存在的证据', 'summary': '合成摘要'},
        {'relation': '外部机构', 'issuer': '合成机构', 'topic': '财务资金',
         'confidence': .99, 'reason': '合成说明', 'evidence_quote': '不存在的证据', 'needs_review': False},
    ])
    monkeypatch.setattr(audit, 'chat', lambda *a, **kw: next(responses))
    docs = [{'path': 'markdown/synthetic.md', 'sha256': 'synthetic', 'text': '仅含合成资料' * 3001}]
    result = audit.classify({'title': '合成事项', 'document_number': ''}, docs, tmp_path)
    assert result['needs_review'] is True
    assert result['quote_verified'] is False
    cached = next(tmp_path.glob('*.json')).read_text(encoding='utf-8')
    assert 'unverified_quote' in cached
