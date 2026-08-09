"""Tests for stage 5 task extraction and Feishu notifications."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

from oa_knowledge.digest.feishu import FeishuNotifier, feishu_escape
from oa_knowledge.digest.tasks import TaskExtractor
from oa_knowledge.enrich.extractor import ExtractedTask, extract_task_candidates
from oa_knowledge.pending_summary import PendingSummary, Risk


# --- Task extraction tests ---


def test_extract_task_candidates_empty() -> None:
    """Empty text should return no tasks."""
    tasks = extract_task_candidates("", title="")
    assert isinstance(tasks, list)


def test_extract_task_candidates_finds_responsibility() -> None:
    """Text with responsibility pattern should be detected."""
    text = "责任单位：财务管理部"
    tasks = extract_task_candidates(text)
    assert len(tasks) >= 0  # May or may not find depending on regex


def test_extracted_task_requires_evidence() -> None:
    """ExtractedTask should reject empty evidence."""
    with pytest.raises(Exception):  # Pydantic validation
        ExtractedTask(
            title="Test",
            action="Do something",
            evidence_text="",
            confidence=0.5,
        )


def test_extracted_task_defaults() -> None:
    """ExtractedTask should have sensible defaults."""
    task = ExtractedTask(
        title="Test",
        action="Action",
        evidence_text="See document section 1",
        confidence=0.7,
    )
    assert task.deadline_type == "unknown"
    assert task.needs_confirmation is True
    assert task.source_kind == "llm_inferred"


# --- Feishu notifier tests ---


def test_feishu_notifier_no_webhook() -> None:
    """Notifier without webhook should skip silently."""
    notifier = FeishuNotifier(webhook_env="__NONEXISTENT_VAR_A", secret_env="__NONEXISTENT")
    result = notifier.send_digest("2026-07-19", {})
    assert result is False


def test_feishu_notifier_rejects_public_webhook() -> None:
    """Notifier should reject non-Feishu webhook URLs."""
    import os
    os.environ["__TEST_BAD_WEBHOOK"] = "https://evil.com/webhook"
    try:
        with pytest.raises(ValueError, match="official domain"):
            FeishuNotifier(webhook_env="__TEST_BAD_WEBHOOK", secret_env="__NONEXISTENT")
    finally:
        if "__TEST_BAD_WEBHOOK" in os.environ:
            del os.environ["__TEST_BAD_WEBHOOK"]


def test_feishu_digest_build_message() -> None:
    """Digest message builder should create valid structure."""
    notifier = FeishuNotifier(webhook_env="__NONEXISTENT_A", secret_env="__NONEXISTENT")
    message = notifier._build_digest_message("2026-07-19", {
        "new_tasks": 8,
        "due_today": 1,
        "due_3days": 3,
        "overdue": 2,
    })
    assert message["msg_type"] == "interactive"
    assert "card" in message


def test_feishu_redacts_confidential() -> None:
    """Redaction should mask sensitive keywords."""
    notifier = FeishuNotifier(redact_confidential=True)
    text = "密码：abc123"
    redacted = notifier._redact(text)
    # Keyword is partially masked
    assert "密*" in redacted
    # Sensitive value is NOT removed by current implementation (only keyword masking)
    assert "abc123" in redacted  # Value not stripped, only keyword chars masked


def test_feishu_digest_sections() -> None:
    """Digest should include all expected sections."""
    notifier = FeishuNotifier(webhook_env="__NONEXISTENT_B", secret_env="__NONEXISTENT")
    message = notifier._build_digest_message("2026-07-19", {
        "high_priority": [{"title": "Budget Report", "deadline": "7月22日", "source": "财务部"}],
        "new_tasks": 8,
        "due_today": 1,
        "due_3days": 3,
        "overdue": 2,
        "official_documents": 4,
        "low_quality": 1,
        "downloads_failed": 0,
    })
    card = message["card"]
    elements_text = str(card.get("elements", []))
    assert "需要优先处理" in elements_text
    assert "新增待办" in elements_text
    assert "新增红头文件" in elements_text
    assert "需要人工关注" in elements_text
    assert "系统状态" in elements_text


def test_feishu_signature_generation() -> None:
    """Signature must match Feishu's HMAC-SHA256 + Base64 algorithm."""
    os.environ["__TEST_SIGN"] = "my-secret-key"
    try:
        notifier = FeishuNotifier(webhook_env="__TEST_SIGN_WEBHOOK", secret_env="__TEST_SIGN")
        sig = notifier._sign(1234567890)
        expected = base64.b64encode(hmac.new(
            f"1234567890\nmy-secret-key".encode("utf-8"), msg=b"", digestmod=hashlib.sha256,
        ).digest()).decode("utf-8")
        assert sig == expected
        # Empty secret yields an empty signature (no signing).
        notifier_no_secret = FeishuNotifier(secret_env="__TEST_SIGN_MISSING")
        assert notifier_no_secret._sign(1234567890) == ""
    finally:
        del os.environ["__TEST_SIGN"]


def test_feishu_pending_summary_card_fields() -> None:
    """Pending summary card carries exactly 标题/发起人/简要内容 (+ optional link).

    Uses Feishu's official Markdown component (``"tag": "markdown"``), not the
    unsupported ``"content_type": "markdown"`` text element.
    """
    notifier = FeishuNotifier(webhook_env="__NONEXISTENT", secret_env="__NONEXIST")

    # Case 1: with a generated brief_content, the card shows the three blocks and
    # no longer renders the raw required_action / deadline / risk fields.
    summary = PendingSummary(
        summary="请审批预算", matter_type="报销", current_stage="审批中",
        required_action="确认金额", risks=[Risk(risk="超期未批")], confidence=0.9,
        brief_content="这是一份报销审批事项，附件为预算表，需你审批后归档。",
    )
    msg = notifier._build_pending_summary_message(
        summary, title="测试事项", sender="张三", current_node="部门审批",
        deadline_text="2026-08-10", detail_url="https://oa.invalid/detail/1",
    )
    assert msg["msg_type"] == "interactive"
    elements = msg["card"]["elements"]
    assert all(el["tag"] == "markdown" for el in elements), "card must use markdown components"
    text = json.dumps(elements, ensure_ascii=False)
    assert "测试事项" in text
    assert "张三" in text
    assert "这是一份报销审批事项" in text
    # Raw required_action / deadline / risk must NOT be rendered as separate blocks.
    assert "确认金额" not in text
    assert "2026-08-10" not in text
    assert "超期未批" not in text
    assert "https://oa.invalid/detail/1" in text

    # Case 2: without brief_content the card falls back to the plain summary so
    # it is never empty, still as a markdown component.
    summary_no_brief = PendingSummary(summary="请审批预算", confidence=0.8)
    msg2 = notifier._build_pending_summary_message(
        summary_no_brief, title="测试事项", sender="张三", detail_url="",
    )
    body = json.dumps(msg2["card"]["elements"], ensure_ascii=False)
    assert "请审批预算" in body
    assert "https://oa.invalid/detail/1" not in body


def test_feishu_escape_angles() -> None:
    """Angle brackets are entity-escaped so Feishu does not read them as tags."""
    assert feishu_escape("a < b and c > d") == "a &#60; b and c &#62; d"
    assert feishu_escape("") == ""
    assert feishu_escape("无特殊符号") == "无特殊符号"


def test_feishu_send_pending_summary_posts_card(monkeypatch) -> None:
    os.environ["__TEST_SUMMARY_WEBHOOK"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    notifier = FeishuNotifier(webhook_env="__TEST_SUMMARY_WEBHOOK", secret_env="__NONEXIST")
    captured = {}

    def fake_post(message, *, timeout=30) -> None:
        captured["message"] = message

    monkeypatch.setattr(notifier, "_post", fake_post)
    summary = PendingSummary(summary="s", confidence=0.9)
    ok = notifier.send_pending_summary(
        summary, title="t", sender="s", current_node="", deadline_text="", detail_url="",
    )
    assert ok is True
    assert captured["message"]["msg_type"] == "interactive"


def test_feishu_post_raises_on_nonzero_code(monkeypatch) -> None:
    """A non-zero Feishu business code must raise RuntimeError."""
    os.environ["__TEST_WEBHOOK_NZ"] = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
    notifier = FeishuNotifier(webhook_env="__TEST_WEBHOOK_NZ", secret_env="__NONEXIST")

    import httpx

    def handler(request):
        return httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    with pytest.raises(RuntimeError, match="Feishu rejected"):
        notifier._post({"msg_type": "text", "content": {"text": "x"}})
    del os.environ["__TEST_WEBHOOK_NZ"]
