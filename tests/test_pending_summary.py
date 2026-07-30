import pytest
from pydantic import ValidationError

from oa_knowledge.pending_summary import PendingSummary, normalize_pending_content, normalize_pending_response


def test_pending_summary_schema_accepts_evidence_backed_empty_optional_values() -> None:
    result = PendingSummary.model_validate({
        "summary": "关于制度征求意见的待办。", "matter_type": "征求意见", "initiator": "综合部",
        "current_stage": "部门办理", "key_points": ["核对反馈期限"], "required_action": "审阅并反馈",
        "amounts": [], "deadlines": [], "risks": [],
        "attachment_overview": [{"filename": "制度稿.docx", "likely_role": "征求意见稿"}],
        "confidence": 0.82,
    })
    assert result.confidence == 0.82


def test_pending_summary_schema_rejects_unbounded_confidence() -> None:
    with pytest.raises(ValidationError):
        PendingSummary.model_validate({
            "summary": "x", "matter_type": "x", "initiator": "x", "current_stage": "x",
            "key_points": [], "required_action": "", "amounts": [], "deadlines": [], "risks": [],
            "attachment_overview": [], "confidence": 2,
        })


def test_pending_summary_normalizes_model_response_without_inventing_optional_facts() -> None:
    result = normalize_pending_response({"title": "事项", "summary": "请审阅文件", "amount": "100万", "date": "明天"})
    assert result.summary == "请审阅文件"
    assert result.amounts == []
    assert result.deadlines == []
    assert result.confidence == 0.5


def test_pending_summary_uses_nonempty_plain_text_when_model_omits_json() -> None:
    result = normalize_pending_content("请审阅附件并按期反馈。")

    assert result.summary == "请审阅附件并按期反馈。"
    assert result.confidence == 0.25
    assert result.amounts == []
