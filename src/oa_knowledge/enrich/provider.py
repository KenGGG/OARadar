"""Provider capability and confidentiality policy for OpenAI-compatible LLMs."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class ContentClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ProviderConfig(BaseModel):
    provider_name: str
    base_url: str
    api_key_env: str = "NEWAPI_API_KEY"
    model: str
    timeout_seconds: int = Field(default=180, ge=1, le=7200)
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    temperature: float = Field(default=0.1, ge=0, le=2)
    provider_mode: str = Field(default="local_only", pattern="^(local_only|approved_remote)$")
    supports_json_schema: bool = False
    supports_vision: bool = False
    uses_local_gpu: bool = False
    allow_confidential: bool = False
    allow_restricted: bool = False
    require_redaction: bool = True
    max_concurrency: int = Field(default=1, ge=1, le=32)

    @field_validator("base_url")
    @classmethod
    def valid_http_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("provider base_url must be an HTTP(S) endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider base_url must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @property
    def is_loopback(self) -> bool:
        return (urlparse(self.base_url).hostname or "") in {"localhost", "127.0.0.1", "::1"}


class ProviderRequestContext(BaseModel):
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_classification: ContentClassification
    redacted: bool = False
    context_fields: list[str] = Field(default_factory=list)


class ProviderDecision(BaseModel):
    allowed: bool
    reason_code: str
    provider_name: str
    provider_mode: str
    uses_local_gpu: bool


_FORBIDDEN_FIELDS = {
    "cookie", "cookies", "browser_profile", "credentials", "database_url",
    "database_connection", "api_key", "authorization",
}


def evaluate_provider_request(config: ProviderConfig, context: ProviderRequestContext) -> ProviderDecision:
    reason = "LOCAL_PROVIDER_ALLOWED"
    allowed = True
    normalized_fields = {field.strip().lower() for field in context.context_fields}

    if normalized_fields & _FORBIDDEN_FIELDS:
        allowed, reason = False, "FORBIDDEN_CONTEXT_FIELD"
    elif not config.is_loopback and config.provider_mode != "approved_remote":
        allowed, reason = False, "REMOTE_PROVIDER_NOT_APPROVED"
    elif not config.is_loopback and (
        context.content_classification == ContentClassification.CONFIDENTIAL and not config.allow_confidential
        or context.content_classification == ContentClassification.RESTRICTED and not config.allow_restricted
    ):
        allowed, reason = False, "CONTENT_CLASSIFICATION_DENIED"
    elif not config.is_loopback and config.require_redaction and not context.redacted:
        allowed, reason = False, "REDACTION_REQUIRED"
    elif not config.is_loopback:
        reason = "APPROVED_REMOTE_ALLOWED"

    return ProviderDecision(
        allowed=allowed,
        reason_code=reason,
        provider_name=config.provider_name,
        provider_mode=config.provider_mode,
        uses_local_gpu=config.uses_local_gpu,
    )
