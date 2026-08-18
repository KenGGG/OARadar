"""Conservative context accounting for the local Ollama text model."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from urllib.parse import urlparse

import httpx


class ContextBudgetExceeded(ValueError):
    """Raised before a request whose estimated tokens exceed its budget."""


@dataclass(frozen=True)
class LocalModelProfile:
    model: str
    context_window: int
    discovered: bool


@dataclass(frozen=True)
class ContextBudget:
    context_window: int
    max_output_tokens: int
    system_tokens: int = 0
    safety_margin: int = 1024

    @property
    def max_input_tokens(self) -> int:
        return max(0, self.context_window - self.max_output_tokens - self.system_tokens - self.safety_margin)

    def ensure_fits(self, text: str) -> None:
        estimated = estimate_tokens(text)
        if estimated > self.max_input_tokens:
            raise ContextBudgetExceeded(
                f"estimated input tokens {estimated} exceed safe budget {self.max_input_tokens}"
            )


def estimate_tokens(text: str) -> int:
    """Overestimate mixed Chinese/ASCII tokens without sending text elsewhere."""
    if not text:
        return 0
    utf8_estimate = math.ceil(len(text.encode("utf-8")) / 2)
    ascii_estimate = math.ceil(len(text) / 4)
    return max(utf8_estimate, ascii_estimate)


def discover_ollama_profile(
    base_url: str,
    model: str,
    *,
    fallback_context_window: int = 8192,
    context_window_cap: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LocalModelProfile:
    """Read Ollama model metadata, returning a conservative fallback on error."""
    parsed = urlparse(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.port != 11434:
        return LocalModelProfile(model=model, context_window=fallback_context_window, discovered=False)
    native_base = base_url.rstrip("/").removesuffix("/v1")
    try:
        with httpx.Client(base_url=native_base, timeout=5, follow_redirects=False, transport=transport) as client:
            response = client.post("/api/show", json={"model": model})
            response.raise_for_status()
            data = response.json()
        values: list[int] = []
        details_value = data.get("details", {}).get("context_length")
        if isinstance(details_value, int):
            values.append(details_value)
        for key, value in data.get("model_info", {}).items():
            if str(key).endswith(".context_length") and isinstance(value, int):
                values.append(value)
        if not values:
            raise ValueError("context length missing")
        window = max(values)
        if context_window_cap is not None:
            window = min(window, context_window_cap)
        if window < 2048:
            raise ValueError("context length implausibly small")
        return LocalModelProfile(model=model, context_window=window, discovered=True)
    except Exception:
        return LocalModelProfile(model=model, context_window=fallback_context_window, discovered=False)


def _hard_split(text: str, max_tokens: int) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = start + 1
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(text[start:middle]) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        chunks.append(text[start:best])
        start = best
    return chunks


def chunk_text(text: str, *, max_tokens: int, overlap_tokens: int = 0) -> list[str]:
    """Split in source order at paragraph boundaries; every chunk is bounded."""
    if max_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("invalid chunk token limits")
    if estimate_tokens(text) <= max_tokens:
        return [text] if text else []
    units = [unit for unit in re.split(r"(?<=\n\n)", text) if unit]
    units = [part for unit in units for part in _hard_split(unit, max_tokens)]
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = current + unit
        if current and estimate_tokens(candidate) > max_tokens:
            chunks.append(current)
            overlap = ""
            if overlap_tokens:
                for size in range(1, len(current) + 1):
                    tail = current[-size:]
                    if estimate_tokens(tail) > overlap_tokens:
                        break
                    overlap = tail
            current = overlap + unit
            if estimate_tokens(current) > max_tokens:
                current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
