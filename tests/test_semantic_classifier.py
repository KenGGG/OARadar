from pathlib import Path

from oa_knowledge.classification.agnes_eligibility import AgnesEligibility
from oa_knowledge.classification.semantic_classifier import (
    JsonSemanticCache,
    SemanticClassifier,
    SemanticPackage,
    _SEMANTIC_ATTACHMENT_CONTEXT_MAX_CHARS,
    _system_prompt,
    _user_prompt,
)


class _FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str, dict]] = []

    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict) -> dict:
        self.calls.append((system_prompt, user_prompt, json_schema))
        return {"content": self.content, "model": "synthetic-model", "error": None}


class _SequenceClient:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls: list[tuple[str, str, dict]] = []

    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict) -> dict:
        self.calls.append((system_prompt, user_prompt, json_schema))
        return {
            "content": self.contents.pop(0),
            "model": "synthetic-model",
            "error": None,
        }


_EXTERNAL = """{
  "classification_status":"classified",
  "content_origin":"external",
  "flow_type":"external_inbound",
  "canonical_issuer":"广州市工业和信息化局",
  "business_category":null,
  "document_type":"通知",
  "confidence":0.96,
  "review_current":"classify",
  "evidence":[{"source":"附件01","type":"signature","summary":"文尾落款为广州市工业和信息化局"}],
  "reason":"落款能够识别发文机关。"
}"""


def _package() -> SemanticPackage:
    return SemanticPackage(
        oa_item_key="done:synthetic",
        title="广州市工业和信息化局关于开展服务企业工作的通知",
        document_number="穗工信函〔2025〕18号",
        attachment_names=("通知.pdf",),
        parsed_attachments=(("附件01", "广州市工业和信息化局\n\n关于开展服务企业工作的通知\n\n广州市工业和信息化局"),),
        parse_artifact_hashes=("a" * 64,),
        current_classification=None,
    )


def test_allowed_public_package_uses_agnes_with_minimal_payload(tmp_path: Path) -> None:
    agnes = _FakeClient(_EXTERNAL)
    local = _FakeClient(_EXTERNAL)
    classifier = SemanticClassifier(agnes, local, JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1")

    result = classifier.classify(
        _package(), AgnesEligibility("allowed", "external_public_formal_document")
    )

    assert result.provider == "agnes"
    assert result.outcome.canonical_issuer == "广州市工业和信息化局"
    assert len(agnes.calls) == 1
    assert local.calls == []
    payload = agnes.calls[0][1]
    assert "/data/" not in payload
    assert "done:synthetic" not in payload


def test_local_only_package_never_calls_agnes(tmp_path: Path) -> None:
    agnes = _FakeClient(_EXTERNAL)
    local = _FakeClient(_EXTERNAL)
    classifier = SemanticClassifier(agnes, local, JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1")

    result = classifier.classify(
        _package(), AgnesEligibility("local_only", "sensitive_signal")
    )

    assert result.provider == "local_qwen"
    assert agnes.calls == []
    assert len(local.calls) == 1


def test_invalid_model_json_is_not_a_classification_and_is_cached_nowhere(tmp_path: Path) -> None:
    agnes = _FakeClient("not-json")
    classifier = SemanticClassifier(agnes, _FakeClient(_EXTERNAL), JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1")

    result = classifier.classify(
        _package(), AgnesEligibility("allowed", "external_public_formal_document")
    )

    assert result.outcome is None
    assert result.rejection_code == "schema_invalid"
    assert list(tmp_path.iterdir()) == []


def test_public_agnes_schema_invalid_response_is_retried_once(tmp_path: Path) -> None:
    agnes = _SequenceClient("not-json", _EXTERNAL)
    classifier = SemanticClassifier(
        agnes, _FakeClient(_EXTERNAL), JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1"
    )

    result = classifier.classify(
        _package(), AgnesEligibility("allowed", "external_public_formal_document")
    )

    assert result.outcome is not None
    assert result.outcome.canonical_issuer == "广州市工业和信息化局"
    assert len(agnes.calls) == 2


def test_local_qwen_schema_invalid_response_is_retried_once(tmp_path: Path) -> None:
    local = _SequenceClient("not-json", _EXTERNAL)
    classifier = SemanticClassifier(
        _FakeClient(_EXTERNAL), local, JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1"
    )

    result = classifier.classify(_package(), AgnesEligibility("local_only", "sensitive_signal"))

    assert result.outcome is not None
    assert result.provider == "local_qwen"
    assert len(local.calls) == 2


def test_identical_input_reuses_local_cache_without_second_model_call(tmp_path: Path) -> None:
    agnes = _FakeClient(_EXTERNAL)
    classifier = SemanticClassifier(agnes, _FakeClient(_EXTERNAL), JsonSemanticCache(tmp_path), prompt_version="agnes-classifier-v1")
    eligibility = AgnesEligibility("allowed", "external_public_formal_document")

    first = classifier.classify(_package(), eligibility)
    second = classifier.classify(_package(), eligibility)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(agnes.calls) == 1
    assert second.input_sha256 == first.input_sha256


def test_semantic_prompt_names_every_required_json_key_for_local_qwen() -> None:
    prompt = _system_prompt()
    for key in (
        "classification_status",
        "content_origin",
        "flow_type",
        "canonical_issuer",
        "business_category",
        "document_type",
        "confidence",
        "review_current",
        "evidence",
        "reason",
    ):
        assert key in prompt
    assert "keep、replace、needs_review 或 classify" in prompt


def test_semantic_prompt_bounds_total_attachment_context_but_keeps_each_boundary() -> None:
    package = SemanticPackage(
        oa_item_key="done:multi-attachment",
        title="Synthetic multi attachment OA",
        document_number=None,
        attachment_names=tuple(f"附件{number}.pdf" for number in range(1, 6)),
        parsed_attachments=tuple(
            (
                f"附件{number}",
                f"HEAD-{number}\n" + "正文" * 20_000 + f"\nTAIL-{number}",
            )
            for number in range(1, 6)
        ),
        parse_artifact_hashes=tuple(str(number) * 64 for number in range(1, 6)),
        current_classification=None,
    )

    prompt = _user_prompt(package, public=False)

    assert len(prompt) < _SEMANTIC_ATTACHMENT_CONTEXT_MAX_CHARS + 2_000
    for number in range(1, 6):
        assert f"===== 附件{number} =====" in prompt
        assert f"HEAD-{number}" in prompt
        assert f"TAIL-{number}" in prompt
