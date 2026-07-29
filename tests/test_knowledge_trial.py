from pathlib import Path

from oa_knowledge.knowledge_trial import (
    ItemClassification,
    KnowledgeClassification,
    item_destination,
    render_source_record,
    parse_classification,
    render_knowledge_note,
    normalize_classification,
    safe_name,
)


def test_parse_classification_accepts_fenced_model_json() -> None:
    result = parse_classification(
        """```json
        {"canonical_title":"管理办法","primary_category":"制度与内部流程",
         "organization_scope":"本公司","document_type":"制度",
         "business_domains":["公司治理"],"projects":[],"knowledge_admission":true,
         "confidence":0.92,"reason":"正式制度文件"}
        ```"""
    )

    assert result.primary_category == "制度与内部流程"
    assert result.knowledge_admission is True


def test_parse_classification_rejects_unknown_directory() -> None:
    result = parse_classification(
        '{"canonical_title":"文件","primary_category":"随意目录",'
        '"organization_scope":"其他","document_type":"其他",'
        '"business_domains":[],"projects":[],"knowledge_admission":true,'
        '"confidence":0.9,"reason":"test"}'
    )

    assert result.primary_category == "待审核"
    assert result.knowledge_admission is False


def test_rendered_note_uses_attachment_as_knowledge_and_oa_as_source() -> None:
    classification = KnowledgeClassification(
        canonical_title="示例融资租赁有限公司董事会议事规则",
        primary_category="制度与内部流程",
        organization_scope="本公司",
        document_type="制度",
        business_domains=["公司治理"],
        projects=[],
        knowledge_admission=True,
        confidence=0.96,
        reason="正式公司治理制度",
    )

    note = render_knowledge_note(
        knowledge_id="KD-14",
        classification=classification,
        source_oa_id="oa-123",
        source_oa_title="征求意见",
        source_filename="议事规则.docx",
        content_sha256="a" * 64,
        parse_artifact_id=14,
        parse_engine="markitdown",
        body="# 正文",
        model_name="qwen3.5:9b",
    )

    assert "note_type: knowledge_document" in note
    assert "source_oa_ids:\n- oa-123" in note
    assert "classification_model: qwen3.5:9b" in note
    assert "# 正文" in note
    assert safe_name("a/b:c") == "a_b_c"


def test_document_type_takes_priority_over_organization_directory() -> None:
    model_result = KnowledgeClassification(
        canonical_title="上市公司股份质押合同",
        primary_category="本公司文件",
        organization_scope="本公司",
        document_type="合同",
        business_domains=["融资租赁"],
        projects=["示例客户"],
        knowledge_admission=True,
        confidence=0.98,
        reason="项目合同",
    )

    normalized = normalize_classification(model_result, "股份质押合同.pdf")
    assert normalized.primary_category == "合同与法律"


def test_risk_form_and_spreadsheet_title_are_not_misclassified_as_contract() -> None:
    model_result = KnowledgeClassification(
        canonical_title="关于提前还款的申请",
        primary_category="本公司文件",
        organization_scope="本公司",
        document_type="表格",
        business_domains=["风险管理"],
        projects=[],
        knowledge_admission=True,
        confidence=0.95,
        reason="业务表格",
    )
    risk = normalize_classification(
        model_result, "债务合同限制性重大事项填报表-2026年7月.xlsx"
    )
    table = normalize_classification(model_result, "示例客户提前还款测算表.xlsx")

    assert risk.primary_category == "风险与租后"
    assert table.canonical_title == "示例客户提前还款测算表"


def test_project_item_keeps_all_attachments_under_one_project_topic() -> None:
    classification = ItemClassification(
        item_kind="project_topic",
        canonical_title="关于示例客户项目全部提前还款的申请",
        document_number="",
        organization_scope="本公司",
        project_name="示例客户",
        project_topic="提前还款",
        internal_activity="",
        importance="high",
        confidence=0.97,
        reason="围绕单一项目的提前还款事项",
    )

    assert item_destination(classification) == Path("知识库/项目专题/示例客户/提前还款")


def test_source_record_is_a_named_flat_markdown_file() -> None:
    relpath, text = render_source_record(
        oa_id="123",
        oa_title="测试事项",
        classification_kind="internal_operations",
        knowledge_links=[("知识库/内部运行资料/测试.md", "测试")],
    )

    assert relpath == Path("_来源记录/OA/123__测试事项.md")
    assert "[[知识库/内部运行资料/测试|测试]]" in text
