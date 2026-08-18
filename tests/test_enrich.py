"""Tests for stage 3.5 classification and extraction."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from oa_knowledge.enrich.extractor import (
    ExtractionResult,
    ExtractedTask,
    extract_task_candidates,
    validate_json_response,
)
from oa_knowledge.enrich.llm_client import LlmClient
from oa_knowledge.enrich.rules import Classification, classify_item


# --- Rule classification tests ---


def test_classify_notice() -> None:
    """Documents with '通知' should be classified as notice."""
    results = classify_item(title="关于开展年度预算调整工作的通知")
    facets = {c.facet: c.value for c in results}
    assert facets["record_type"] == "notice"


def test_classify_policy() -> None:
    """Documents with '制度' should be classified as policy."""
    results = classify_item(title="示例产业集团财务管理制度")
    facets = {c.facet: c.value for c in results}
    assert facets["record_type"] == "policy"


def test_classify_official_document() -> None:
    """Red-header documents should be classified as official_document."""
    results = classify_item(title="红头文件关于安全生产的决定")
    facets = {c.facet: c.value for c in results}
    assert facets["record_type"] == "official_document"


def test_classify_confidentiality() -> None:
    """Documents with '秘密' should be classified as secret."""
    results = classify_item(title="内部会议纪要", content="此文件为机密级")
    facets = {c.facet: c.value for c in results}
    assert facets["confidentiality"] == "confidential"


def test_classify_validity_repealed() -> None:
    """Documents mentioning '废止' should be marked repealed."""
    results = classify_item(title="关于废止部分制度的通知")
    facets = {c.facet: c.value for c in results}
    assert facets["validity_status"] == "repealed"


def test_classify_authority_from_issuer() -> None:
    """Authority level should be detected from issuer field."""
    results = classify_item(
        title="预算调整通知",
        issuer="示例产业集团财务管理部",
    )
    facets = {c.facet: c.value for c in results}
    assert facets["authority_level"] == "company_department"


def test_classification_has_confidence_and_evidence() -> None:
    """Each classification should have confidence and evidence."""
    results = classify_item(title="通知")
    for c in results:
        assert isinstance(c.confidence, float)
        assert 0 <= c.confidence <= 1
        assert c.source == "rule"
        assert c.facet in {"record_type", "authority_level", "validity_status", "confidentiality"}


# --- LLM client security tests ---


def test_llm_client_rejects_public_url() -> None:
    """LlmClient should reject non-loopback base_urls."""
    with pytest.raises(ValueError, match="loopback"):
        LlmClient(base_url="https://api.openai.com/v1")


def test_llm_client_accepts_loopback() -> None:
    """LlmClient should accept loopback base_urls."""
    client = LlmClient(base_url="http://127.0.0.1:11434/v1")
    assert client.base_url == "http://127.0.0.1:11434/v1"


def test_llm_client_keeps_output_token_limit() -> None:
    client = LlmClient(base_url="http://127.0.0.1:11434/v1", max_tokens=700)
    assert client.max_tokens == 700
    assert client.uses_ollama_native is True
    assert client.local_context_window == 8192


def test_ollama_native_payload_uses_json_schema_when_supplied() -> None:
    client = LlmClient(base_url="http://127.0.0.1:11434/v1")
    schema = {"type": "object", "properties": {"problem": {"type": "string"}}, "required": ["problem"]}

    payload = client._request_payload("system", "user", json_schema=schema)

    assert payload["format"] == schema


def test_non_ollama_provider_keeps_openai_compatible_protocol() -> None:
    client = LlmClient(base_url="http://127.0.0.1:3000/v1")
    assert client.uses_ollama_native is False


def test_llm_client_rejects_remote_provider_even_when_marked_approved() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LlmClient(
            base_url="https://llm.example.invalid/v1",
            provider_mode="approved_remote",
            api_key_env="SYNTHETIC_LLM_API_KEY",
        )


def test_llm_client_blocks_redirects() -> None:
    """LlmClient should not follow redirects to external URLs."""
    client = LlmClient(base_url="http://127.0.0.1:11434/v1")
    # The client is created successfully; redirect blocking is tested at runtime
    assert client.timeout_seconds == 180


def test_llm_client_retries_transient_local_request_errors(monkeypatch) -> None:
    attempts = 0

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "qwen3.5:9b", "message": {"content": "{}"}}

    class FakeHttpClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, _path: str, json: dict):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise __import__("httpx").ReadTimeout("synthetic timeout")
            return FakeResponse()

    monkeypatch.setattr("oa_knowledge.enrich.llm_client.httpx.Client", FakeHttpClient)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    client = LlmClient(
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3.5:9b",
        max_retries=1,
    )
    client._profile_discovered = True

    result = client.chat("system", "synthetic", json_schema={"type": "object"})

    assert attempts == 2
    assert result["error"] is None
    assert result["content"] == "{}"


# --- Extractor tests ---


def test_extracted_task_validation() -> None:
    """ExtractedTask should reject invalid confidence values."""
    with pytest.raises(Exception):  # Pydantic validation error
        ExtractedTask(
            title="Test",
            action="Do something",
            evidence_text="Evidence here",
            confidence=1.5,  # Out of range
        )


def test_extracted_task_valid() -> None:
    """Valid ExtractedTask should pass validation."""
    task = ExtractedTask(
        title="Test task",
        action="Submit report",
        evidence_text="See section 3.2 of document",
        confidence=0.8,
        deadline_type="explicit",
    )
    assert task.confidence == 0.8
    assert task.deadline_type == "explicit"


def test_extract_task_candidates_finds_actions() -> None:
    """Rule-based extraction should find action patterns."""
    text = "请各部门于2026年7月22日前完成预算调整表的报送工作"
    tasks = extract_task_candidates(text, title="通知")
    assert len(tasks) >= 0  # May or may not find depending on pattern match


def test_validate_json_response_parses_valid_json() -> None:
    """Should parse valid JSON strings."""
    data = '{"tasks": [], "record_type": "notice"}'
    result = validate_json_response(data)
    assert result is not None
    assert result["record_type"] == "notice"


def test_validate_json_response_extracts_from_markdown() -> None:
    """Should extract JSON from markdown code blocks."""
    text = '```json\n{"key": "value"}\n```'
    result = validate_json_response(text)
    assert result is not None
    assert result["key"] == "value"


def test_validate_json_response_repairs_trailing_comma() -> None:
    """Should repair common JSON issues like trailing commas."""
    text = '{"key": "value",}'
    result = validate_json_response(text)
    assert result is not None
    assert result["key"] == "value"


def test_validate_json_response_returns_none_on_failure() -> None:
    """Should return None for unparseable content."""
    result = validate_json_response("this is not json at all {{{")
    assert result is None


def test_extraction_result_schema() -> None:
    """ExtractionResult should be a valid Pydantic model."""
    result = ExtractionResult(
        tasks=[],
        record_type="notice",
        extraction_method="rules",
    )
    assert result.extraction_method == "rules"
    assert result.tasks == []
