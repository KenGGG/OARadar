from __future__ import annotations

import json

import httpx
import pytest

from oa_knowledge.enrich.context_budget import (
    ContextBudget,
    ContextBudgetExceeded,
    chunk_text,
    discover_ollama_profile,
    estimate_tokens,
)


def test_discovers_context_window_from_ollama_show() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        assert json.loads(request.content)["model"] == "qwen3.5:9b"
        return httpx.Response(200, json={"model_info": {"qwen35.context_length": 262_144}})

    profile = discover_ollama_profile(
        "http://127.0.0.1:11434/v1",
        "qwen3.5:9b",
        transport=httpx.MockTransport(handler),
    )

    assert profile.context_window == 262_144
    assert profile.discovered is True


def test_profile_discovery_uses_conservative_fallback() -> None:
    profile = discover_ollama_profile(
        "http://127.0.0.1:11434/v1",
        "qwen3.5:9b",
        fallback_context_window=16_384,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )

    assert profile.context_window == 16_384
    assert profile.discovered is False


def test_token_estimate_is_conservative_for_chinese_and_ascii() -> None:
    assert estimate_tokens("甲" * 100) >= 100
    assert estimate_tokens("a" * 100) >= 25


def test_budget_reserves_system_output_and_safety_tokens() -> None:
    budget = ContextBudget(context_window=1_000, max_output_tokens=200, system_tokens=100, safety_margin=100)

    assert budget.max_input_tokens == 600
    with pytest.raises(ContextBudgetExceeded):
        budget.ensure_fits("甲" * 601)


def test_chunk_text_preserves_order_and_fits_budget() -> None:
    text = "\n\n".join(f"第{i}段：" + "甲" * 80 for i in range(8))
    chunks = chunk_text(text, max_tokens=150, overlap_tokens=10)

    assert len(chunks) > 1
    assert all(estimate_tokens(chunk) <= 150 for chunk in chunks)
    assert "第0段" in chunks[0]
    assert "第7段" in chunks[-1]
