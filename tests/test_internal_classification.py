from __future__ import annotations

import importlib
import importlib.util
import json

import pytest

MODULE = "oa_knowledge.classification.internal_classification"


def _module():
    assert importlib.util.find_spec(MODULE) is not None, (
        "the focused internal classification module must exist"
    )
    return importlib.import_module(MODULE)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Synthetic 融资租赁项目立项申请书", "立项申请书"),
        ("Synthetic 董事会会议纪要", "会议纪要"),
        ("Synthetic 项目合同用印 印鉴使用申请表", "印鉴使用申请"),
        ("Synthetic neutral internal matter", None),
    ],
)
def test_document_type_is_extracted_without_becoming_a_business_category(
    title: str, expected: str | None
) -> None:
    module = _module()

    assert module.extract_document_type(title) == expected


@pytest.mark.parametrize(
    ("title", "body", "category"),
    [
        ("融资租赁项目立项申请书", "租赁项目投放方案和客户资料", "02_业务项目与投放租后"),
        ("项目合同用印", "融资租赁项目合同及投放安排", "02_业务项目与投放租后"),
        ("授信材料用印", "向银行申请授信额度及融资材料", "04_财务资金与融资"),
        ("董事会会议纪要", "董事会审议公司治理事项", "01_公司治理与决策"),
        ("项目评审会议纪要", "融资租赁项目评审及投放条件", "02_业务项目与投放租后"),
        ("风险专题会议纪要", "风险合规专题审议", "03_风险合规审计法务"),
    ],
)
def test_existing_categories_are_selected_from_business_context(
    title: str, body: str, category: str
) -> None:
    module = _module()

    result = module.classify_by_content(title, (body,))

    assert result is not None
    assert result.business_category == category
    assert result.business_category != result.document_type
    assert result.decision_source == "content_rule"


def test_ambiguous_internal_content_does_not_default_to_99() -> None:
    module = _module()

    result = module.classify_by_content(
        "内部工作事项",
        ("讨论后续安排，未说明所属业务领域。",),
    )

    assert result is None


@pytest.mark.parametrize(
    ("title", "body", "category"),
    [
        ("光机电客户租后检查及资产分类", "本次材料含财务数据和风险提示。", "02_业务项目与投放租后"),
        ("九州能源直租项目租后报告", "出现资产与资金等常规词。", "02_业务项目与投放租后"),
        ("西陇科学售后回租项目业务方案变更", "项目风险分析。", "02_业务项目与投放租后"),
        ("五舟科技项目预审会决议申请续议", "项目评审材料。", "02_业务项目与投放租后"),
        ("报送金服集团—我司走访及服务企业更新数据", "客户资产信息。", "09_对外报送与监管反馈"),
        ("业务一部工作简报", "本周常规经营工作安排。", "05_经营计划与绩效考核"),
    ],
)
def test_business_subject_beats_incidental_risk_finance_or_document_form(
    title: str, body: str, category: str
) -> None:
    module = _module()

    result = module.classify_by_content(title, (body,))

    assert result is not None
    assert result.business_category == category


class _FakeClient:
    def __init__(self, payload: dict | None = None, *, error: str | None = None, raw_content: str | None = None):
        self.payload = payload
        self.error = error
        self.raw_content = raw_content
        self.calls = 0

    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict):
        self.calls += 1
        return {
            "content": self.raw_content if self.raw_content is not None else json.dumps(self.payload, ensure_ascii=False)
            if self.payload is not None
            else None,
            "model": "synthetic-qwen",
            "usage": None,
            "error": self.error,
            "elapsed_seconds": 0.01,
        }


def _qwen_payload(
    category: str,
    *,
    confidence: float = 0.9,
    outside_existing_categories: bool = False,
) -> dict:
    return {
        "business_category": category,
        "document_type": "会议纪要",
        "confidence": confidence,
        "evidence_quote": "Synthetic evidence describing the actual internal business context.",
        "outside_existing_categories": outside_existing_categories,
        "reason": "Synthetic structured classification reason.",
    }


def test_qwen_can_select_an_existing_category_with_bounded_structured_output() -> None:
    module = _module()
    client = _FakeClient(_qwen_payload("04_财务资金与融资"))

    result = module.LocalQwenInternalClassifier(client).classify(
        "Synthetic ambiguous title", ("Synthetic bounded attachment text",), None
    )

    assert client.calls == 1
    assert result is not None
    assert result.business_category == "04_财务资金与融资"
    assert result.decision_source == "local_qwen"


def test_qwen_99_requires_explicit_evidence_that_01_through_09_do_not_apply() -> None:
    module = _module()
    rejected = _FakeClient(
        _qwen_payload("99_其他内部", outside_existing_categories=False)
    )
    accepted = _FakeClient(
        _qwen_payload("99_其他内部", outside_existing_categories=True)
    )

    rejected_result = module.LocalQwenInternalClassifier(rejected).classify(
        "Synthetic other matter", ("Synthetic evidence",), "会议纪要"
    )
    accepted_result = module.LocalQwenInternalClassifier(accepted).classify(
        "Synthetic other matter", ("Synthetic evidence",), "会议纪要"
    )

    assert rejected_result is None
    assert accepted_result is not None
    assert accepted_result.business_category == "99_其他内部"


@pytest.mark.parametrize(
    "payload",
    [
        _qwen_payload("03_风险合规审计法务", confidence=0.74),
        {"business_category": "99_其他内部"},
        _qwen_payload("10_不存在的类别"),
    ],
)
def test_low_confidence_or_invalid_qwen_output_becomes_needs_review(
    payload: dict,
) -> None:
    module = _module()

    result = module.LocalQwenInternalClassifier(_FakeClient(payload)).classify(
        "Synthetic uncertain matter", ("Synthetic uncertain content",), None
    )

    assert result is None


def test_qwen_accepts_one_schema_valid_object_after_a_leading_think_block() -> None:
    module = _module()
    raw = "<think>internal reasoning is not part of the answer</think>\n" + json.dumps(
        _qwen_payload("02_业务项目与投放租后"), ensure_ascii=False
    )
    classifier = module.LocalQwenInternalClassifier(_FakeClient(raw_content=raw))

    result = classifier.classify("Synthetic ambiguous title", ("Synthetic attachment evidence",), None)

    assert result is not None
    assert result.business_category == "02_业务项目与投放租后"
    assert classifier.last_rejection_code is None


@pytest.mark.parametrize(
    ("raw_content", "expected_reason"),
    [
        ('{"business_category":"10_不存在"}', "schema_invalid"),
        ("<think>only reasoning</think>", "json_missing"),
    ],
)
def test_qwen_rejection_reason_is_explicit_without_loosening_the_schema(
    raw_content: str, expected_reason: str
) -> None:
    module = _module()
    classifier = module.LocalQwenInternalClassifier(_FakeClient(raw_content=raw_content))

    assert classifier.classify("Synthetic uncertain title", ("Synthetic uncertain attachment",), None) is None
    assert classifier.last_rejection_code == expected_reason
