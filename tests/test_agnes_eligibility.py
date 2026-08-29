from oa_knowledge.classification.agnes_eligibility import (
    AgnesEligibilityInput,
    determine_agnes_eligibility,
)
from oa_knowledge.config import Settings


def test_allows_a_forwarded_public_government_document_with_formal_number() -> None:
    result = determine_agnes_eligibility(
        AgnesEligibilityInput(
            title="【文件传阅】广州市工业和信息化局关于开展服务企业工作的通知",
            content_origin="external",
            flow_type="external_inbound",
            document_number="穗工信函〔2025〕18号",
            canonical_issuer="广州市工业和信息化局",
            workflow="文件传阅",
            attachment_names=("广州市工业和信息化局通知.pdf",),
        )
    )

    assert result.status == "allowed"
    assert result.reason == "external_public_formal_document"


def test_defaults_to_local_only_for_ambiguous_forwarded_external_content() -> None:
    result = determine_agnes_eligibility(
        AgnesEligibilityInput(
            title="【文件传阅】关于有关事项的通知",
            content_origin="external",
            flow_type="external_inbound",
            document_number=None,
            canonical_issuer=None,
            workflow="文件传阅",
            attachment_names=("附件.pdf",),
        )
    )

    assert result.status == "local_only"
    assert result.reason == "external_public_not_proven"


def test_keeps_project_and_finance_content_local_even_if_sender_looks_public() -> None:
    result = determine_agnes_eligibility(
        AgnesEligibilityInput(
            title="【文件传阅】广州市国资委关于融资租赁项目风险情况的函",
            content_origin="external",
            flow_type="external_inbound",
            document_number="穗国资函〔2025〕9号",
            canonical_issuer="广州市国资委",
            workflow="文件传阅",
            attachment_names=("项目风险情况说明.pdf",),
        )
    )

    assert result.status == "local_only"
    assert result.reason == "sensitive_signal"


def test_keeps_internal_outbound_material_local() -> None:
    result = determine_agnes_eligibility(
        AgnesEligibilityInput(
            title="关于报送年度经营情况的请示",
            content_origin="internal",
            flow_type="outbound_submission",
            document_number=None,
            canonical_issuer=None,
            workflow="内部事项呈批表",
            attachment_names=("年度经营数据.xlsx",),
        )
    )

    assert result.status == "local_only"
    assert result.reason == "non_external_content"


def test_agnes_configuration_allows_only_the_approved_public_endpoint() -> None:
    settings = Settings.model_validate(
        {
            "agnes": {
                "enabled": True,
                "base_url": "https://apihub.agnes-ai.com/v1",
                "api_key_env": "AGNES_API_KEY",
                "model": "agnes-2.0-flash",
            }
        }
    )

    assert settings.agnes.enabled is True
    assert settings.agnes.base_url == "https://apihub.agnes-ai.com/v1"
