from copy import deepcopy
import pytest
from test_archive_classification import specification, row
from oa_knowledge.archive_classification import ArchiveRules
from oa_knowledge.archive_patterns import validate_second_pass


def supplemental():
    spec=specification()
    def rule(identity, kind, category, pattern, label='', transmission=False):
        conditions=[{'op':'regex','field':'clean','value':pattern}]
        if transmission:conditions.insert(0,{'op':'transmission'})
        return dict(id=identity,source_rule_id=identity,name=identity,kind=kind,category=category,label=label,reason='合成匹配依据',condition=dict(op='all',conditions=conditions),enabled=True)
    spec['second_pass']=dict(enabled=True,rules=[
        rule('N01','series','PUBLICATION',r'^合成内参第\d+期','合成内参'),
        rule('N04','workflow','SELF',r'^内部事项呈批表'),
        rule('N08','workflow','SELF',r'项目评审'),
        rule('N05','joint','EXTERNAL',r'^合成集团与合成监管局关于','合成联合来源',True),
        rule('N02','theme','EXTERNAL',r'^关于举办.+的通知$','活动通知',True),
        rule('N03D','review','UNKNOWN',r'^关于.+任免的通知$','人事来源待核实',True)])
    return spec


def test_supplement_only_unknown_preserves_existing_and_download():
    rules=ArchiveRules(supplemental())
    existing=rules.classify(row('合成监管局关于举办项目评审会的通知'),'download')
    assert existing['matched_rule_id']=='C04'
    assert rules.classify(row('内部事项呈批表 项目'),'skip')['virtual_archive_path'] is None
    internal=rules.classify(row('内部事项呈批表 项目'),'download')
    assert internal['archive_category_id']=='SELF' and internal['matched_rule_id']=='N04'
    publication=rules.classify(row('【阅件】合成内参第12期'),'download')
    assert publication['archive_category_id']=='PUBLICATION'


def test_themes_do_not_forge_issuer_or_confirm_human_review():
    rules=ArchiveRules(supplemental())
    theme=rules.classify(row('【文件传阅】关于举办合成活动的通知'),'download')
    assert theme['archive_category_id']=='EXTERNAL'
    assert theme['archive_organization'] is None and theme['archive_bucket']=='活动通知'
    assert theme['classification_status']=='source_pending' and theme['needs_review']
    assert not theme['source_organization_verified']
    personnel=rules.classify(row('【文件传阅】关于合成人员任免的通知'),'download')
    assert personnel['archive_category_id']=='UNKNOWN' and personnel['archive_organization'] is None
    assert personnel['virtual_archive_path'].startswith('05_待确认/')


def test_external_regulations_and_body_conflicts_are_not_our_workflows():
    rules=ArchiveRules(supplemental())
    for title in ('【文件传阅】内部事项呈批表通知','关于印发项目评审办法的通知'):
        assert rules.classify(row(title),'download')['archive_category_id']=='UNKNOWN'
    assert rules.classify(row('内部事项呈批表'),'download',issuer_names=['未知机构'])['archive_category_id']=='UNKNOWN'
    assert rules.classify(row('内部事项呈批表'),'download',issuer_names=['合成监管局'])['archive_category_id']=='EXTERNAL'


def test_joint_sources_are_preserved_and_not_a_single_issuer():
    result=ArchiveRules(supplemental()).classify(row('【文件传阅】合成集团与合成监管局关于发布通知'),'download')
    assert result['archive_category_id']=='EXTERNAL' and result['archive_organization'] is None
    assert set(result['source_organizations'])=={'合成集团','合成监管局'}
    assert result['source_relationship']=='joint_heading'
    multi=ArchiveRules(supplemental()).classify(row('【文件传阅】合成集团与合成监管局关于发布通知；合成监管局关于第二份文件'),'download')
    assert multi['source_relationship']=='multiple_documents'
    assert '第二份文件' not in multi['source_heading']


def test_enabled_and_invalid_condition():
    spec=supplemental();spec['second_pass']['enabled']=False
    assert ArchiveRules(spec).classify(row('内部事项呈批表'),'download')['archive_category_id']=='UNKNOWN'
    broken=deepcopy(spec['second_pass']);broken['rules'][0]['condition']={'op':'execute','value':'anything'}
    with pytest.raises(ValueError):validate_second_pass(broken)
