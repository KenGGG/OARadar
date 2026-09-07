from dataclasses import replace

import pytest

from oa_knowledge.classification.evidence_dossier import OAEvidenceDossier
from oa_knowledge.classification.per_item_classifier import classify_dossier
from oa_knowledge.classification.schemas import PrivateClassificationConfig


def _rules() -> PrivateClassificationConfig:
    return PrivateClassificationConfig.model_validate(
        {
            "initiators": {"internal": {"role": "internal", "aliases": ["内部经办人"]}},
            "document_number_issuers": [
                {
                    "pattern": r"甲集团〔2026〕\d+号",
                    "canonical_issuer": "甲金融服务集团有限公司",
                    "document_type": "通知",
                }
            ],
            "issuer_aliases": {"甲集团": "甲金融服务集团有限公司"},
            "title_templates": [{"pattern": r"^不会匹配$", "content_origin": "internal", "flow_type": "approval"}],
        }
    )


def _dossier(title: str, *, document_number: str | None = None) -> OAEvidenceDossier:
    return OAEvidenceDossier(
        oa_item_key="done:synthetic-1",
        current_decision_id=1,
        title=title,
        normalized_title=title,
        initiator="内部经办人",
        initiator_profile="internal",
        relay_from=None,
        workflow_name=None,
        form_name=None,
        document_number=document_number,
        no_attachment_confirmed=True,
        attachments=(),
        primary_attachment=None,
        primary_attachment_reason="no_attachment_confirmed",
        primary_attachment_conflict=False,
    )


def test_document_number_beats_original_sender_marker() -> None:
    result = classify_dossier(
        _dossier("【传阅】甲集团通知（由经办人原发）", document_number="甲集团〔2026〕8号"),
        _rules(),
    )

    assert result.classification_status == "classified"
    assert result.content_origin == "external"
    assert result.canonical_issuer == "甲金融服务集团有限公司"


def test_original_sender_marker_does_not_alone_make_an_item_external() -> None:
    result = classify_dossier(_dossier("业务一部工作会议纪要（由经办人原发）"), _rules())

    assert result.classification_status == "classified"
    assert result.content_origin == "internal"
    assert result.business_category == "05_经营计划与绩效考核"


def test_topic_beats_generic_contract_and_seal_forms() -> None:
    project = classify_dossier(_dossier("业务合同审批表—甲项目保密协议"), _rules())
    archive = classify_dossier(_dossier("用印申请—档案整理"), _rules())

    assert (project.content_origin, project.business_category) == (
        "internal", "02_业务项目与投放租后"
    )
    assert (archive.content_origin, archive.business_category) == (
        "internal", "08_行政采购与信息化"
    )


def test_bare_original_sender_marker_stays_unresolved() -> None:
    result = classify_dossier(_dossier("请阅材料（由经办人原发）"), _rules())

    assert result.classification_status == "needs_review"
    assert result.content_origin is None
    assert result.review_reason == "origin_unresolved"


def test_current_outer_issuer_is_kept_without_inventing_an_alias() -> None:
    result = classify_dossier(
        _dossier("甲区财政局转发乙市财政局转发国务院关于资金管理的通知"),
        _rules(),
    )

    assert result.classification_status == "classified"
    assert result.content_origin == "external"
    assert result.raw_issuer == "甲区财政局"
    assert result.canonical_issuer == "甲区财政局"


def test_internal_department_is_not_treated_as_external_issuer() -> None:
    result = classify_dossier(_dossier("业务一部工作简报"), _rules())

    assert result.content_origin != "external"


def test_state_council_is_a_direct_external_issuer() -> None:
    result = classify_dossier(_dossier("国务院关于开展专项工作的意见（由经办人原发）"), _rules())

    assert result.classification_status == "classified"
    assert result.content_origin == "external"
    assert result.canonical_issuer == "国务院"


def test_complete_self_company_board_meeting_keeps_governance_category() -> None:
    result = classify_dossier(
        replace(
            _dossier("凯得融资租赁关于召开合成董事会测试会议的通知（审议合成薪酬议案）"),
            initiator_profile="mixed",
        ),
        _rules(),
    )

    assert (result.content_origin, result.business_category) == (
        "internal", "01_公司治理与决策"
    )


@pytest.mark.parametrize(("title", "category"), [
    ("采购合同审批表（常年法律顾问协议书）", "03_风险合规审计法务"),
    ("采购合同审批表（员工福利服务协议）", "06_人力资源"),
    ("采购合同审批表（员工培训服务协议）", "06_人力资源"),
    ("采购合同审批表（年度审计服务）", "03_风险合规审计法务"),
    ("采购合同审批表（档案整理服务）", "08_行政采购与信息化"),
    ("采购合同审批表（软件许可服务）", "08_行政采购与信息化"),
    ("甲项目尽职调查的内部请示", "02_业务项目与投放租后"),
])
def test_specific_subject_before_form(title, category):
    result = classify_dossier(replace(_dossier(title), initiator_profile="mixed"), _rules())
    assert (result.content_origin, result.business_category) == ("internal", category)


@pytest.mark.parametrize("prefix", ["新增某同志批示：", "【新增某同志批示】", "二次办理：", "【文件传阅】（新增某同志批示）（二次办理，新增另一同志批示）"])
def test_processing_annotation_is_not_part_of_issuer(prefix):
    title = prefix + "国务院办公厅关于专项工作的通知"
    result = classify_dossier(_dossier(title), _rules())
    assert result.canonical_issuer == "国务院办公厅"
    assert _dossier(title).title == title


def test_joint_issuers_remain_separate():
    result = classify_dossier(_dossier("(盖章版)甲市财政局 乙市审计局关于联合检查的通知"), _rules())
    assert result.canonical_issuer == "甲市财政局、乙市审计局"


def test_board_procedure_does_not_override_subject():
    result = classify_dossier(_dossier("内部事项呈批表（经董事会审议的通知存款管理办法）"), _rules())
    assert result.business_category == "04_财务资金与融资"


@pytest.mark.parametrize(("title", "category"), [
    ("凯得租赁合成测试董事会会议材料", "01_公司治理与决策"),
    ("广州凯得融资租赁有限公司合成测试发展定位与经营规划报告", "05_经营计划与绩效考核"),
])
def test_self_owned_materials_are_internal_even_with_unknown_initiator(title, category):
    result = classify_dossier(replace(_dossier(title), initiator_profile="unknown"), _rules())
    assert (result.content_origin, result.business_category) == ("internal", category)


def test_self_company_recipient_does_not_make_external_notice_internal():
    result = classify_dossier(
        _dossier("甲市财政局关于通知凯得租赁报送统计数据的函"), _rules()
    )
    assert result.content_origin == "external"
    assert result.canonical_issuer == "甲市财政局"


def test_procurement_form_without_a_subject_requires_evidence():
    result = classify_dossier(_dossier("采购合同审批表（甲机构）"), _rules())
    assert result.content_origin == "internal"
    assert result.classification_status == "needs_review"
    assert result.review_reason == "business_category_missing"


def test_department_without_parent_organization_is_not_a_complete_issuer():
    result = classify_dossier(_dossier("【文件传阅】编研部关于资料整理的通知"), _rules())
    assert result.content_origin == "external"
    assert result.classification_status == "needs_review"
    assert result.canonical_issuer is None
