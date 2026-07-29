import pytest
from pydantic import ValidationError

from oa_knowledge.done_knowledge import DoneKnowledge, build_attachment_evidence, done_generation_schema, done_max_tokens, find_vault_overview, normalize_done_response, retry_evidence


def test_done_knowledge_requires_evidence_for_each_conclusion() -> None:
    result = DoneKnowledge.model_validate({
        "problem": "明确项目投放条件", "core_conclusions": [{"text": "通过投放", "evidence": "附件1 block-3"}],
        "business_data": [], "approval_conditions": [], "risks": [], "actions": [], "reusable_knowledge": [],
        "confidence": 0.8,
    })
    assert result.core_conclusions[0].evidence


def test_done_knowledge_rejects_conclusion_without_evidence() -> None:
    with pytest.raises(ValidationError):
        DoneKnowledge.model_validate({
            "problem": "x", "core_conclusions": [{"text": "结论", "evidence": ""}],
            "business_data": [], "approval_conditions": [], "risks": [], "actions": [], "reusable_knowledge": [],
            "confidence": 0.8,
        })


def test_done_knowledge_normalizes_summary_only_response_without_fake_evidence() -> None:
    result = normalize_done_response({"summary": "该文件明确了报送安排", "key_points": ["无来源的点"]})
    assert result.problem == "该文件明确了报送安排"
    assert result.core_conclusions == []


def test_done_knowledge_normalizes_qwen_nested_summary_shape() -> None:
    result = normalize_done_response({
        "title": "事项标题",
        "header_sha256": "a" * 64,
        "issue_info": {"summary": "附件说明了项目安排和主要要求"},
        "sections": [{"heading": "主要内容", "content": "需要在规定期限内完成报送"}],
    })

    assert result.problem == "附件说明了项目安排和主要要求"
    assert result.confidence == 0.5
    assert result.core_conclusions == []


def test_retry_evidence_shrinks_after_invalid_model_output() -> None:
    source = "x" * 8000

    assert len(retry_evidence(source, 1)) == 8000
    assert len(retry_evidence(source, 2)) == 4000
    assert len(retry_evidence(source, 4)) == 2000


def test_done_summary_caps_verbose_local_model_output() -> None:
    assert done_max_tokens(4096) == 600
    assert done_max_tokens(900) == 600


def test_done_generation_schema_only_requests_the_overview() -> None:
    schema = done_generation_schema()

    assert set(schema["properties"]) == {"problem", "confidence"}
    assert set(schema["required"]) == {"problem", "confidence"}


def test_attachment_evidence_samples_each_attachment_head_evenly() -> None:
    source, ids = build_attachment_evidence([
        (11, "附件甲.pdf", "甲" * 5000),
        (12, "附件乙.docx", "乙" * 5000),
        (13, "附件丙.xlsx", "丙" * 5000),
    ], total_budget=6000)

    assert ids == [11, 12, 13]
    assert "\n" + "甲" * 2000 in source
    assert "\n" + "乙" * 2000 in source
    assert "\n" + "丙" * 2000 in source
    assert "parse_artifact:11" in source
    assert "parse_artifact:13" in source
    assert "附件乙.docx" in source


def test_vault_overview_lookup_parses_quoted_yaml_identifier(tmp_path) -> None:
    folder = tmp_path / "04_本公司文件" / "事项"
    folder.mkdir(parents=True)
    overview = folder / "OA-W1__事项总览.md"
    overview.write_text("---\noa_item_id: 'done:123'\nmanaged_by: oaradar\n---\n# 标题\n", encoding="utf-8")

    assert find_vault_overview(tmp_path, "done:123") == overview
