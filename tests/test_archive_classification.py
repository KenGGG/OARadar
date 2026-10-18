from datetime import datetime
from types import SimpleNamespace

import pytest

from oa_knowledge.archive_classification import ArchiveRules, CATEGORIES, apply_spec, settings_state
from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import OAManifestItem
from sqlalchemy.orm import Session
from test_title_screening import specification as screening_spec, import_spec, view
import json


def specification():
    def institution(identity, name, code):
        return dict(id=identity, name=name, issuer_name_pattern=name, document_code_pattern=code)
    return dict(version="synthetic-classification-v1", normalization=dict(optional_display_prefixes=["【文件传阅】"]),
        folder_structure=dict(categories=[dict(id=key, folder=value) for key, value in CATEGORIES.items()], create_month_subfolder=False, missing_year_value="年份待确认"),
        classification=dict(rules=[dict(id=rule, category=category) for rule, category in zip(["C01","C02","C03","C04","C99"], ["PUBLICATION","SELF","GROUP","EXTERNAL","UNKNOWN"])], self_workflow_title_patterns=[r"^业务一部工作简报", r"^.{0,70}直租项目风险评审报告$"]),
        mappings=dict(self_organization=institution("S", "合成租赁", "合租"), group_organizations=[institution("G", "合成集团", "合集")], external_organizations=[institution("E", "合成监管局", "合监")], publication_series=[dict(id="P", name="合成信息", title_pattern="合成信息"), dict(id="P2", name="合成学习选刊", title_pattern="合成学习选刊")]))


def row(title):
    return SimpleNamespace(title=title, workitem_id_text="-9000000000000000001", oa_item_key="done:-9000000000000000001", initiated_at=datetime(2026,10,10), completed_at=None, sender="合成监管局")


@pytest.mark.parametrize("title,category", [
    ("合租〔2026〕12号 关于发布办法", "SELF"), ("客户直租项目风险评审报告", "SELF"),
    ("合集[2026]12号 文件", "GROUP"), ("合成监管局关于发布通知", "EXTERNAL"),
    ("合成信息 第三期", "PUBLICATION"), ("业务一部工作简报0830", "SELF"),
    ("关于发布通知", "UNKNOWN"), ("政府学习资料20261010", "UNKNOWN"),
    ("客户合同涉及合成监管局", "UNKNOWN"), ("合成监管局客户合同", "UNKNOWN"),
    ("合成信息和合成学习选刊", "UNKNOWN"), ("合成集团与合成监管局关于通知", "UNKNOWN"),
    ("合集[2026]1号 合监[2026]2号", "UNKNOWN"),
    ("【文件传阅】合成监管局关于通知(由某人原发)", "EXTERNAL"),
])
def test_source_and_path(title, category):
    result = ArchiveRules(specification()).classify(row(title), "download")
    assert result["archive_category_id"] == category
    assert result["virtual_archive_path"].endswith("/2026/-9000000000000000001")
    assert len(result["virtual_archive_path"].split("/")) == (4 if category in {"GROUP","EXTERNAL","PUBLICATION"} else 3)
    assert result["needs_review"] == (category == "UNKNOWN")


def test_explicit_body_precedence_and_conflicts():
    rules = ArchiveRules(specification())
    assert rules.classify(row("合集[2026]1号"), "download", issuer_names=["合成监管局"])["archive_category_id"] == "EXTERNAL"
    for issuers in (["合成集团", "合成监管局"], ["未知机构"], ["合成监管局", "未知机构"]):
        assert rules.classify(row("合集[2026]1号"), "download", issuer_names=issuers)["archive_category_id"] == "UNKNOWN"
    assert rules.body_issuers("正文提到合成监管局\n发文单位：合成集团") == ["合成集团"]
    assert rules.body_issuers("发起人：合成监管局") == []


@pytest.mark.parametrize('title', ['【公告】合集〔2026〕12号-关于合成事项的通知',
    '合集〔2026〕12-关于合成事项的通知(由某人原发)'])
def test_primary_group_code_overrides_reference_attachment_issuers(title):
    spec=specification();spec['source_priority']={'primary_group_document_code':True}
    result=ArchiveRules(spec).classify(row(title),'download',issuer_names=['合成监管局','合成租赁'])
    assert result['archive_category_id']=='GROUP'
    assert result['archive_organization']=='合成集团'
    assert result['classification_source']=='primary_group_document_code'


def test_cited_group_code_and_multiple_primary_codes_are_not_authoritative():
    spec=specification();spec['source_priority']={'primary_group_document_code':True}
    rules=ArchiveRules(spec)
    assert rules.primary_group_code('根据合集〔2026〕12号要求填报我司材料') is None
    assert rules.primary_group_code('合集〔2026〕12号 合监〔2026〕3号 关于合成事项') is None
    assert rules.classify(row('合集〔2026〕12号'),'skip')['archive_category_id']=='EXCLUDED'


def test_excluded_and_missing_year():
    rules = ArchiveRules(specification())
    assert rules.classify(row("合成信息"), "skip")["virtual_archive_path"] is None
    item = row("合成信息"); item.initiated_at = None; item.completed_at = datetime(2025,1,1)
    assert rules.classify(item, "download")["archive_year"] == "2025"
    item.completed_at = None
    assert rules.classify(item, "download")["archive_year"] == "年份待确认"


def test_activation_preserves_download_and_archive(config_file, tmp_path):
    settings = load_settings(config_file); upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(OAManifestItem(oa_item_key="done:-90001", workitem_id_text="-90001", title="合成信息", list_page=1, list_ordinal=1, processing_status="discovered", archive_relpath="synthetic/original")); session.commit()
    source = tmp_path / "screening.json"; source.write_text(json.dumps(screening_spec()), encoding="utf-8")
    from oa_knowledge.title_screening import set_active
    set_active(settings, import_spec(settings, source))
    before = view(settings)["counts"]
    with Session(engine) as session:
        original_status = session.query(OAManifestItem).one().processing_status
    assert apply_spec(settings, specification())["counts"] == {"PUBLICATION":1}
    assert settings_state(settings)["plan_id"] is None
    result = apply_spec(settings, specification(), save=True)
    assert settings_state(settings)["plan_id"] == result["plan_id"]
    after = view(settings)
    assert after["counts"] == before
    assert after["items"][0]["archive_category_id"] == "PUBLICATION"
    edited = specification()
    edited["mappings"]["publication_series"][0]["title_pattern"] = "不会匹配的合成系列"
    assert apply_spec(settings, edited, base_plan_id=result["plan_id"])["counts"] == {"UNKNOWN": 1}
    next_version = apply_spec(settings, edited, base_plan_id=result["plan_id"], save=True)
    assert next_version["plan_id"] != result["plan_id"]
    from oa_knowledge.archive_classification import activate
    activate(settings, result["plan_id"])
    assert view(settings)["items"][0]["archive_category_id"] == "PUBLICATION"
    from oa_knowledge.title_screening import override
    with Session(engine) as session:
        item_id = session.query(OAManifestItem).one().id
        assert session.query(OAManifestItem).one().processing_status == original_status
    override(settings, item_id, action="skip", reason="合成测试人工排除", folder=None)
    assert view(settings)["items"][0]["virtual_archive_path"] is None
    with Session(engine) as session:
        item = session.query(OAManifestItem).one()
        assert item.archive_relpath == "synthetic/original"
    engine.dispose()
