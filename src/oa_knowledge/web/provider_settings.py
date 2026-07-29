"""Safe Web settings for LLM and Feishu providers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from oa_knowledge.config import Settings, load_settings


LLM_FIELDS = {"enabled", "active_provider", "ollama_base_url", "ollama_model", "agnes_base_url", "agnes_model", "timeout_seconds", "max_tokens", "temperature", "max_retries", "max_concurrency"}
FEISHU_FIELDS = {"enabled", "message_type", "max_items_per_section", "redact_confidential", "retry_attempts"}


def provider_settings_view(settings: Settings) -> dict[str, Any]:
    return {
        "agnes": {
            **{name: getattr(settings.llm, name) for name in sorted(LLM_FIELDS)},
            "api_key_env": settings.llm.api_key_env,
            "api_key_configured": bool(os.environ.get(settings.llm.api_key_env)),
            "agnes_api_key_configured": bool(os.environ.get("AGNES_API_KEY")),
            "ollama_available": _ollama_available(settings.llm.ollama_base_url),
            "provider_name": settings.llm.provider_name,
            "base_url": settings.llm.base_url,
            "model": settings.llm.model,
            "uses_local_gpu": settings.llm.uses_local_gpu,
            "real_oa_delivery_enabled": False,
            "delivery_block_reason": "OA content is confidential and local-only",
        },
        "feishu": {
            **{name: getattr(settings.feishu, name) for name in sorted(FEISHU_FIELDS)},
            "webhook_env": settings.feishu.webhook_env,
            "secret_env": settings.feishu.secret_env,
            "webhook_configured": bool(os.environ.get(settings.feishu.webhook_env)),
            "secret_configured": bool(os.environ.get(settings.feishu.secret_env)),
        },
    }


def _ollama_available(base_url: str) -> bool:
    from urllib.request import urlopen
    try:
        root = base_url.removesuffix("/v1").rstrip("/")
        with urlopen(f"{root}/api/tags", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def update_provider_settings(config_path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    if config_path is None:
        raise ValueError("configuration file is unavailable")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    for section, allowed in (("llm", LLM_FIELDS), ("feishu", FEISHU_FIELDS)):
        incoming = payload.get("agnes" if section == "llm" else section, {})
        if not isinstance(incoming, dict):
            raise ValueError(f"{section} settings must be a mapping")
        unknown = set(incoming) - allowed
        if unknown:
            raise ValueError(f"unsupported {section} settings: {', '.join(sorted(unknown))}")
        current = raw.setdefault(section, {})
        current.update(incoming)
    # Validate before replacing the configured file. Credentials are never accepted here.
    candidate = config_path.with_suffix(config_path.suffix + ".candidate")
    candidate.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        updated = load_settings(candidate)
        candidate.replace(config_path)
    finally:
        candidate.unlink(missing_ok=True)
    return {**provider_settings_view(updated), "restart_required": True}
