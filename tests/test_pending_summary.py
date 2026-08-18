import pytest
from pydantic import ValidationError

from oa_knowledge.pending_summary import (
    PendingSummary, PendingSummaryError, normalize_pending_content, normalize_pending_response,
    deterministic_pending_fallback, pending_evidence, summarize_evidence,
)
from oa_knowledge.enrich.context_budget import estimate_tokens


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


def test_pending_summary_rejects_non_json_model_output() -> None:
    with pytest.raises(PendingSummaryError):
        normalize_pending_content("请审阅附件并按期反馈。")


def test_pending_summary_uses_common_text_field_from_alternate_json_shape() -> None:
    result = normalize_pending_content('{"content":"请审阅并反馈。"}')

    assert result.summary == "请审阅并反馈。"
    assert result.confidence == 0.25


def test_pending_summary_uses_message_from_model_json() -> None:
    result = normalize_pending_content('{"message":"请审阅并反馈。"}')

    assert result.summary == "请审阅并反馈。"
    assert result.confidence == 0.25


def test_pending_evidence_stays_within_local_model_context_budget() -> None:
    evidence = pending_evidence("甲" * 60_000)

    assert len(evidence) == 12_000


class ChunkingClient:
    def __init__(self):
        self.calls = []

    def chat(self, system, user, **_kwargs):
        self.calls.append((system, user))
        if "分块提要" in system:
            return {"error": None, "content": '{"summary":"本块涉及合成事项。","evidence":["合成证据"]}'}
        return {"error": None, "content": '{"summary":"合成待办摘要","matter_type":"阅知","initiator":"","current_stage":"","key_points":[],"required_action":"阅知","amounts":[],"deadlines":[],"risks":[],"attachment_overview":[],"brief_content":"合成摘要","confidence":0.8}'}


def test_long_pending_evidence_is_chunked_and_every_call_is_bounded() -> None:
    client = ChunkingClient()
    result = summarize_evidence(client, "甲" * 20_000, max_input_tokens=1000)

    assert result.summary == "合成待办摘要"
    assert len(client.calls) > 2
    assert all(estimate_tokens(user) <= 1000 for _system, user in client.calls)


def test_deterministic_fallback_is_marked_and_does_not_invent_facts() -> None:
    result = deterministic_pending_fallback('{"title":"合成待办","current_node":"部门阅知"}')

    assert result.summary.startswith("【本地模型降级】")
    assert result.current_stage == "部门阅知"
    assert result.amounts == [] and result.deadlines == [] and result.risks == []
    assert result.confidence == 0
