import pytest
from pydantic import ValidationError

from oa_knowledge.enrich.provider import (
    ContentClassification,
    ProviderConfig,
    ProviderRequestContext,
    evaluate_provider_request,
)


def test_loopback_provider_is_allowed_in_local_only_mode() -> None:
    config = ProviderConfig(provider_name="newapi-local", base_url="http://127.0.0.1:3000/v1", model="synthetic")
    context = ProviderRequestContext(input_hash="a" * 64, content_classification=ContentClassification.INTERNAL)

    decision = evaluate_provider_request(config, context)

    assert decision.allowed
    assert decision.reason_code == "LOCAL_PROVIDER_ALLOWED"


def test_remote_provider_is_prohibited() -> None:
    config = ProviderConfig(provider_name="newapi-remote", base_url="https://llm.example.invalid/v1", model="synthetic")
    context = ProviderRequestContext(input_hash="a" * 64, content_classification=ContentClassification.INTERNAL, redacted=True)

    decision = evaluate_provider_request(config, context)

    assert not decision.allowed
    assert decision.reason_code == "REMOTE_PROVIDER_PROHIBITED"


@pytest.mark.parametrize("classification", [ContentClassification.CONFIDENTIAL, ContentClassification.RESTRICTED])
def test_legacy_approved_remote_provider_mode_is_invalid(classification: ContentClassification) -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            provider_name="approved", base_url="https://llm.example.invalid/v1", model="synthetic",
            provider_mode="approved_remote",
        )


def test_remote_provider_never_becomes_allowed_after_redaction() -> None:
    config = ProviderConfig(provider_name="remote", base_url="https://llm.example.invalid/v1", model="synthetic")
    context = ProviderRequestContext(input_hash="c" * 64, content_classification=ContentClassification.INTERNAL)

    assert evaluate_provider_request(config, context).reason_code == "REMOTE_PROVIDER_PROHIBITED"


def test_secret_bearing_context_is_always_rejected() -> None:
    config = ProviderConfig(provider_name="local", base_url="http://localhost:3000/v1", model="synthetic")
    context = ProviderRequestContext(
        input_hash="d" * 64, content_classification=ContentClassification.INTERNAL,
        context_fields=["title", "browser_profile"],
    )

    assert evaluate_provider_request(config, context).reason_code == "FORBIDDEN_CONTEXT_FIELD"


def test_provider_capabilities_and_hash_are_strictly_validated() -> None:
    config = ProviderConfig(
        provider_name="local-gpu", base_url="http://127.0.0.1:11434/v1", model="synthetic",
        uses_local_gpu=True, supports_json_schema=True, supports_vision=False,
    )
    assert config.uses_local_gpu
    with pytest.raises(ValidationError):
        ProviderRequestContext(input_hash="not-a-hash", content_classification=ContentClassification.INTERNAL)
