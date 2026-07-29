from pathlib import Path

from oa_knowledge.vault_rebuild import classify_item, stable_item_id


def test_upstream_forwarded_document_keeps_upstream_source() -> None:
    result = classify_item("【文件传阅】某区关于统计报送工作的通知", "")
    assert result.parts[0] == "01_政府及上级部门文件"


def test_forwarding_sender_does_not_replace_actual_issuer() -> None:
    result = classify_item("【文件传阅】关于开展专项工作的通知(由合成人员原发)", "上级金融集团 合成人员")
    assert result.parts[0] == "01_政府及上级部门文件"
    assert result.method == "forwarding_rule"


def test_government_aliases_are_not_internal_or_external() -> None:
    for title in (
        "中央纪委办公厅关于纠治四风的通知",
        "某市政府人工智能产业发展办公室工作要点",
        "【文件传阅】区政府科学技术协会关于调整工作分工的通知",
        "金融局调查企业融资情况的通知",
    ):
        assert classify_item(title, "").parts[0] == "01_政府及上级部门文件"


def test_government_assessment_uses_defined_regulatory_directory() -> None:
    result = classify_item("某市国资委关于开展年度考核评价的通知", "")
    assert result.parts == ("01_政府及上级部门文件", "02_监管要求")


def test_company_project_item_uses_project_stage_not_customer_source() -> None:
    result = classify_item("示例项目联合承租项目出账申请", "本公司")
    assert result.parts[:2] == ("04_本公司文件", "03_融资租赁项目")
    assert result.parts[-1] == "05_投放与出账"
    assert result.project_name


def test_time_is_not_a_business_category() -> None:
    result = classify_item("2026年关于印发资金管理制度的通知", "上级控股集团")
    assert all("2026" not in part for part in result.parts)


def test_stable_id_prefers_logical_item_and_falls_back_to_oa_id() -> None:
    assert stable_item_id(12, "-123") == "LI000012"
    assert stable_item_id(None, "-123") == "W-123"
