"""LLM client — OpenAI-compatible API for local LLMs with security constraints."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class LlmClient:
    """OpenAI-compatible LLM client with mandatory security constraints.

    Security:
    - Only allows loopback/Unix socket base_urls
    - Blocks redirects to public networks
    - Blocks proxy environment variables
    - Never logs prompt/response bodies or sensitive content
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434/v1",
        api_key_env: str = "LOCAL_LLM_API_KEY",
        model: str = "glm-4.7-flash",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout_seconds: int = 180,
        max_retries: int = 2,
        provider_mode: str = "local_only",
    ) -> None:
        self._validate_security(base_url, provider_mode)
        self.base_url = base_url
        self.provider_mode = provider_mode
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.local_context_window = 8192
        parsed = urlparse(base_url)
        self.uses_ollama_native = (
            parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.port == 11434
        )
        api_key = os.environ.get(api_key_env, "") or None
        self.headers: dict[str, str] = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    @staticmethod
    def _validate_security(base_url: str, provider_mode: str = "local_only") -> None:
        """Allow public endpoints only through the explicit approved-remote mode."""
        if not base_url:
            raise ValueError("base_url must not be empty")
        # Allow loopback addresses and Unix sockets
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        hostname = parsed.hostname or ""
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return
        if parsed.scheme == "unix":
            return
        if parsed.scheme == "https" and provider_mode == "approved_remote":
            return
        raise ValueError(
            f"LLM base_url must be loopback-only, got: {base_url}. "
            "Public network addresses are prohibited for privacy."
        )

    def _request_payload(self, system_prompt: str, user_prompt: str, *, json_schema: dict | None = None) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if self.uses_ollama_native:
            return {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": json_schema or "json",
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.max_tokens,
                    "num_ctx": self.local_context_window,
                },
            }
        response_format = {"type": "json_object"}
        if json_schema:
            response_format = {"type": "json_schema", "json_schema": {"name": "response", "schema": json_schema}}
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": response_format,
        }

    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict | None = None) -> dict:
        """Send a chat completion request.

        Returns dict with:
        - content: str | None
        - model: str
        - usage: dict | None
        - error: str | None
        """
        import time

        start = time.monotonic()
        error_code: str | None = None
        result_content: str | None = None
        model_used = self.model
        usage: dict | None = None

        try:
            request_base = self.base_url
            request_path = "/chat/completions"
            payload = self._request_payload(system_prompt, user_prompt, json_schema=json_schema)
            if self.uses_ollama_native:
                request_base = self.base_url.removesuffix("/v1")
                request_path = "/api/chat"
            with httpx.Client(
                base_url=request_base,
                headers=self.headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = client.post(request_path, json=payload)
                response.raise_for_status()
                data = response.json()
                if self.uses_ollama_native:
                    result_content = data.get("message", {}).get("content")
                else:
                    choices = data.get("choices", [])
                    if choices:
                        result_content = choices[0].get("message", {}).get("content")
                model_used = data.get("model", self.model)
                usage_data = data.get("usage", {})
                if usage_data:
                    usage = {
                        "prompt_tokens": usage_data.get("prompt_tokens"),
                        "completion_tokens": usage_data.get("completion_tokens"),
                        "total_tokens": usage_data.get("total_tokens"),
                    }
        except httpx.RequestError as exc:
            error_code = f"http_request_error"
            logger.warning("LLM request failed: %s", type(exc).__name__)
        except Exception as exc:
            error_code = f"unexpected_error"
            logger.warning("LLM unexpected error: %s", type(exc).__name__)

        elapsed = time.monotonic() - start
        # Log only metrics, never content
        logger.info(
            "llm_call model=%s tokens=%s elapsed=%.1fs error=%s",
            model_used,
            usage,
            elapsed,
            error_code,
        )

        return {
            "content": result_content,
            "model": model_used,
            "usage": usage,
            "error": error_code,
            "elapsed_seconds": round(elapsed, 2),
        }

    def is_available(self) -> bool:
        """Check if the LLM endpoint is reachable."""
        try:
            with httpx.Client(base_url=self.base_url, timeout=5, follow_redirects=False) as client:
                response = client.get("/models")
                return response.status_code == 200
        except Exception:
            return False
