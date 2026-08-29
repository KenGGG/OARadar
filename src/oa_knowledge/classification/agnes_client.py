"""Minimal audited transport for the explicitly approved Agnes public API."""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_APPROVED_BASE_URL = "https://apihub.agnes-ai.com/v1"


class AgnesPublicClient:
    """OpenAI-compatible client that cannot be pointed at another public host."""

    def __init__(
        self,
        base_url: str = _APPROVED_BASE_URL,
        *,
        api_key_env: str = "AGNES_API_KEY",
        model: str = "agnes-2.0-flash",
        timeout_seconds: int = 180,
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> None:
        if base_url.rstrip("/") != _APPROVED_BASE_URL:
            raise ValueError("Agnes public client requires the approved endpoint")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "apihub.agnes-ai.com":
            raise ValueError("Agnes public client requires the approved endpoint")
        if api_key_env != "AGNES_API_KEY" or model != "agnes-2.0-flash":
            raise ValueError("Agnes client configuration is not approved")
        self.base_url = _APPROVED_BASE_URL
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._api_key_env = api_key_env

    def chat(self, system_prompt: str, user_prompt: str, *, json_schema: dict) -> dict:
        """Return transport metadata without ever logging body content or credentials."""
        key = os.environ.get(self._api_key_env, "")
        if not key:
            return {"content": None, "model": self.model, "error": "credential_missing"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "oa_semantic_classification", "schema": json_schema},
            },
        }
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = client.post("/chat/completions", json=payload)
                    response.raise_for_status()
                    value = response.json()
                choices = value.get("choices", [])
                content = choices[0].get("message", {}).get("content") if choices else None
                return {"content": content, "model": value.get("model", self.model), "error": None}
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                retryable = code == 429 or 500 <= code <= 599
                error = f"http_{code}"
            except httpx.RequestError:
                retryable = True
                error = "http_request_error"
            except (TypeError, ValueError, KeyError):
                retryable = False
                error = "response_invalid"
            if not retryable or attempt >= self.max_retries:
                logger.warning("agnes_call_failed model=%s error=%s", self.model, error)
                return {"content": None, "model": self.model, "error": error}
            time.sleep(min(2**attempt, 8))
        return {"content": None, "model": self.model, "error": "unexpected_error"}
