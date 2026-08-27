from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from oa_knowledge.classification.evidence import (
    AttachmentCandidate,
    ClassificationItem,
    Evidence,
)
from oa_knowledge.classification.metadata_rules import (
    build_transfer_chain,
    classify_from_metadata,
    collect_metadata_evidence,
    normalize_issuer,
)
from oa_knowledge.classification.schemas import PrivateClassificationConfig

FIXTURE_PATH = Path(__file__).parent / "fixtures/classification/metadata_cases.yaml"


@pytest.fixture(scope="module")
def config() -> PrivateClassificationConfig:
    return PrivateClassificationConfig.model_validate(
        {
            "initiators": {
                "synth.person.internal": {
                    "role": "internal",
                    "aliases": ["Synthetic Internal Alpha"],
                },
                "synth.person.internal.beta": {
                    "role": "internal",
                    "aliases": ["Synthetic Internal Beta"],
                },
                "synth.person.internal.gamma": {
                    "role": "internal",
                    "aliases": ["Synthetic Internal Gamma"],
                },
                "synth.person.external": {
                    "role": "external",
                    "aliases": ["Synthetic External Person"],
                },
                "synth.person.mixed": {
                    "role": "mixed",
                    "aliases": ["Synthetic Mixed Person"],
                },
                "synth.person.system": {
                    "role": "system",
                    "aliases": ["Synthetic Workflow Account"],
                },
                "synth.person.unknown": {
                    "role": "unknown",
                    "aliases": ["Synthetic Unknown Person"],
                },
            },
            "document_number_issuers": [
                {
                    "pattern": r"^SYN-AUTH-[0-9]{4}-[0-9]+$",
                    "canonical_issuer": "Synthetic Records Authority",
                    "document_type": "notice",
                }
            ],
            "issuer_aliases": {
                "SRA": "Synthetic Records Authority",
                "Synthetic Records Authority": "Synthetic Records Authority",
                "Synthetic Alternate Authority": "Synthetic Alternate Authority",
            },
            "title_templates": [
                {
                    "pattern": r"^Synthetic internal approval:",
                    "content_origin": "internal",
                    "flow_type": "approval",
                    "business_category": "08_synthetic_administration",
                },
                {
                    "pattern": r"^Synthetic external circulation:",
                    "content_origin": "external",
                    "flow_type": "circulation",
                    "canonical_issuer": "Synthetic Records Authority",
                },
            ],
        }
    )


def _item(raw: dict[str, object]) -> ClassificationItem:
    attachments = tuple(
        AttachmentCandidate(**attachment)
        for attachment in raw.get("attachments", [])  # type: ignore[union-attr]
    )
    return ClassificationItem(
        item_key=str(raw["item_key"]),
        title=str(raw["title"]),
        initiator=str(raw["initiator"]),
        document_number=raw.get("document_number"),  # type: ignore[arg-type]
        issuer=raw.get("issuer"),  # type: ignore[arg-type]
        transfer_people=tuple(raw.get("transfer_people", [])),  # type: ignore[arg-type]
        attachments=attachments,
    )


def _cases() -> list[dict[str, object]]:
    loaded = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
    return loaded["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_synthetic_metadata_decision_table(
    case: dict[str, object], config: PrivateClassificationConfig
) -> None:
    item = _item(case["item"])  # type: ignore[arg-type]
    expected = case["expected"]  # type: ignore[assignment]

    evidence = collect_metadata_evidence(item, config)
    outcome = classify_from_metadata(evidence)

    assert (
        sorted({entry.evidence_scope for entry in evidence})
        == expected["evidence_scopes"]
    )
    assert outcome.classification_status == expected["status"]
    assert outcome.content_origin == expected["origin"]
    assert outcome.business_category == expected["business_category"]
    assert outcome.canonical_issuer == expected["canonical_issuer"]
    assert outcome.confidence == expected["confidence"]
    assert outcome.decision_source == expected["decision_source"]
    assert [hop.person_identifier for hop in outcome.transfer_chain] == expected[
        "transfer_chain"
    ]
    assert outcome.relay_from == expected["relay_from"]
    assert outcome.escalation_action == expected["action"]
    if "normalized_title" in expected:
        assert outcome.normalized_title == expected["normalized_title"]


def test_internal_package_keeps_attachment_document_evidence_scoped(
    config: PrivateClassificationConfig,
) -> None:
    case = next(
        case
        for case in _cases()
        if case["id"] == "package_internal_with_external_attachment"
    )
    item = _item(case["item"])

    evidence = collect_metadata_evidence(item, config)
    outcome = classify_from_metadata(evidence)

    attachment_document = next(
        entry for entry in evidence if entry.code == "document_number"
    )
    assert attachment_document.evidence_scope == "attachment"
    assert attachment_document.attachment_key == "synthetic-att-1"
    assert attachment_document.canonical_issuer == "Synthetic Records Authority"
    assert outcome.content_origin == "internal"
    assert outcome.canonical_issuer is None


def test_alias_normalization_detects_case_unicode_and_whitespace_collisions() -> None:
    resolved = normalize_issuer(
        "  ＳＲＡ  ",
        {"sra": "Synthetic Records Authority"},
    )
    ambiguous = normalize_issuer(
        "SRA",
        {
            "SRA": "Synthetic Records Authority",
            "ｓｒａ": "Synthetic Alternate Authority",
        },
    )
    unknown = normalize_issuer(
        "Unlisted Synthetic Office", {"SRA": "Synthetic Records Authority"}
    )

    assert (resolved.status, resolved.canonical_issuer) == (
        "resolved",
        "Synthetic Records Authority",
    )
    assert (ambiguous.status, ambiguous.canonical_issuer) == ("ambiguous", None)
    assert (unknown.status, unknown.canonical_issuer) == ("unrecognized", None)


def test_equal_priority_conflict_cannot_be_hidden_by_confidence() -> None:
    evidence = [
        Evidence(
            evidence_scope="package",
            code="explicit_issuer",
            priority=1,
            confidence=0.99,
            content_origin="external",
            canonical_issuer="Synthetic Authority A",
        ),
        Evidence(
            evidence_scope="package",
            code="document_number",
            priority=1,
            confidence=0.99,
            content_origin="external",
            canonical_issuer="Synthetic Authority B",
        ),
    ]

    outcome = classify_from_metadata(evidence)

    assert outcome.classification_status == "needs_review"
    assert outcome.canonical_issuer is None
    assert outcome.conflict_codes == ("document_number", "explicit_issuer")


def test_equal_priority_flow_type_conflict_is_unresolved() -> None:
    evidence = [
        Evidence(
            evidence_scope="package",
            code="internal_template_a",
            priority=3,
            confidence=0.96,
            content_origin="internal",
            flow_type="approval",
            business_category="08_synthetic_administration",
        ),
        Evidence(
            evidence_scope="package",
            code="internal_template_b",
            priority=3,
            confidence=0.96,
            content_origin="internal",
            flow_type="circulation",
            business_category="08_synthetic_administration",
        ),
    ]

    outcome = classify_from_metadata(evidence)

    assert outcome.classification_status == "needs_review"
    assert outcome.flow_type is None
    assert outcome.conflict_codes == ("internal_template_a", "internal_template_b")


@pytest.mark.parametrize(
    (
        "first_document_number",
        "first_document_type",
        "second_document_number",
        "second_document_type",
    ),
    [
        ("SYN-AUTH-2026-1", "notice", "SYN-AUTH-2026-1", "directive"),
        ("SYN-AUTH-2026-1", "notice", "SYN-AUTH-2026-2", "notice"),
    ],
)
def test_equal_priority_document_metadata_conflict_is_unresolved(
    first_document_number: str,
    first_document_type: str,
    second_document_number: str,
    second_document_type: str,
) -> None:
    evidence = [
        Evidence(
            evidence_scope="package",
            code="document_rule_a",
            priority=1,
            confidence=0.99,
            content_origin="external",
            flow_type="formal_document",
            canonical_issuer="Synthetic Records Authority",
            document_number=first_document_number,
            document_type=first_document_type,
        ),
        Evidence(
            evidence_scope="package",
            code="document_rule_b",
            priority=1,
            confidence=0.99,
            content_origin="external",
            flow_type="formal_document",
            canonical_issuer="Synthetic Records Authority",
            document_number=second_document_number,
            document_type=second_document_type,
        ),
    ]

    forward = classify_from_metadata(evidence)
    reversed_result = classify_from_metadata(tuple(reversed(evidence)))

    assert forward == reversed_result
    assert forward.classification_status == "needs_review"
    assert forward.document_number is None
    assert forward.document_type is None


def test_compatible_equal_priority_winners_merge_independent_of_evidence_order() -> (
    None
):
    evidence = [
        Evidence(
            evidence_scope="package",
            code="document_number",
            priority=1,
            confidence=0.99,
            content_origin="external",
            flow_type="formal_document",
            issuer="Synthetic Records Authority",
            canonical_issuer="Synthetic Records Authority",
            document_number="SYN-AUTH-2026-3",
            document_type="notice",
        ),
        Evidence(
            evidence_scope="package",
            code="explicit_issuer",
            priority=1,
            confidence=0.99,
            content_origin="external",
            flow_type="formal_document",
            issuer="SRA",
            canonical_issuer="Synthetic Records Authority",
        ),
    ]

    forward = classify_from_metadata(evidence)
    reversed_result = classify_from_metadata(tuple(reversed(evidence)))

    assert forward == reversed_result
    assert forward.classification_status == "classified"
    assert forward.canonical_issuer == "Synthetic Records Authority"
    assert forward.document_number == "SYN-AUTH-2026-3"
    assert forward.document_type == "notice"


def test_overlapping_rules_are_unresolved_independent_of_declaration_order(
    config: PrivateClassificationConfig,
) -> None:
    raw = config.model_dump(mode="python")
    raw["title_templates"] = [
        {
            "pattern": r"^Synthetic overlapping",
            "content_origin": "internal",
            "flow_type": "flow-z",
            "business_category": "08_synthetic_administration",
        },
        {
            "pattern": r"overlapping title$",
            "content_origin": "internal",
            "flow_type": "flow-a",
            "business_category": "08_synthetic_administration",
        },
    ]
    raw["document_number_issuers"] = [
        {
            "pattern": r"^SYN-OVERLAP-",
            "canonical_issuer": "Synthetic Records Authority",
            "document_type": "type-z",
        },
        {
            "pattern": r"-2026-1$",
            "canonical_issuer": "Synthetic Records Authority",
            "document_type": "type-a",
        },
    ]
    forward_config = PrivateClassificationConfig.model_validate(raw)
    reverse_raw = forward_config.model_dump(mode="python")
    reverse_raw["title_templates"].reverse()
    reverse_raw["document_number_issuers"].reverse()
    reverse_config = PrivateClassificationConfig.model_validate(reverse_raw)

    title_item = ClassificationItem(
        item_key="synthetic-overlap-title",
        title="Synthetic overlapping title",
        initiator="synth.person.internal",
    )
    document_item = ClassificationItem(
        item_key="synthetic-overlap-document",
        title="Synthetic neutral document",
        initiator="synth.person.internal",
        document_number="SYN-OVERLAP-2026-1",
    )

    for item in (title_item, document_item):
        forward = classify_from_metadata(
            collect_metadata_evidence(item, forward_config)
        )
        reversed_result = classify_from_metadata(
            collect_metadata_evidence(item, reverse_config)
        )
        assert forward == reversed_result
        assert forward.classification_status == "needs_review"


def test_result_is_independent_of_evidence_and_config_declaration_order(
    config: PrivateClassificationConfig,
) -> None:
    item = ClassificationItem(
        item_key="synthetic-order",
        title="Synthetic external circulation: stable ordering",
        initiator="synth.person.internal",
    )
    reordered_config = PrivateClassificationConfig(
        initiators=dict(reversed(list(config.initiators.items()))),
        document_number_issuers=list(reversed(config.document_number_issuers)),
        issuer_aliases=dict(reversed(list(config.issuer_aliases.items()))),
        title_templates=list(reversed(config.title_templates)),
    )
    first_evidence = collect_metadata_evidence(item, config)
    second_evidence = collect_metadata_evidence(item, reordered_config)

    assert first_evidence == second_evidence
    assert classify_from_metadata(first_evidence) == classify_from_metadata(
        tuple(reversed(second_evidence))
    )


def test_transfer_hops_are_structured_and_relay_is_final_predecessor(
    config: PrivateClassificationConfig,
) -> None:
    case = next(
        case for case in _cases() if case["id"] == "three_level_internal_transfer"
    )
    item = _item(case["item"])
    evidence = collect_metadata_evidence(item, config)

    chain = build_transfer_chain(item, evidence)

    assert [(hop.ordinal, hop.from_person, hop.to_person) for hop in chain] == [
        (1, None, "synth.person.internal"),
        (2, "synth.person.internal", "synth.person.internal.beta"),
        (3, "synth.person.internal.beta", "synth.person.internal.gamma"),
    ]
    assert all(hop.role_type == "internal" for hop in chain)
    assert all(hop.evidence_source == "structured_transfer" for hop in chain)


def test_attachment_scope_never_competes_with_package_scope() -> None:
    package = Evidence(
        evidence_scope="package",
        code="internal_template",
        priority=3,
        confidence=0.96,
        content_origin="internal",
        business_category="08_synthetic_administration",
    )
    attachment = Evidence(
        evidence_scope="attachment",
        attachment_key="synthetic-att",
        code="document_number",
        priority=1,
        confidence=0.99,
        content_origin="external",
        canonical_issuer="Synthetic Records Authority",
    )

    assert classify_from_metadata([attachment, package]).content_origin == "internal"


def test_origin_directory_invariants_are_enforced() -> None:
    internal = classify_from_metadata(
        [
            Evidence(
                evidence_scope="package",
                code="internal_template",
                priority=3,
                confidence=0.96,
                content_origin="internal",
            )
        ]
    )
    invalid_external = classify_from_metadata(
        [
            Evidence(
                evidence_scope="package",
                code="external_initiator",
                priority=5,
                confidence=0.86,
                content_origin="external",
                person_identifier="synth.person.external",
                initiator_role="external",
            )
        ]
    )

    assert internal.business_category == "99_其他内部"
    assert internal.canonical_issuer is None
    assert invalid_external.classification_status == "needs_review"
    assert invalid_external.business_category is None


def test_configured_person_identifier_can_never_become_issuer(
    config: PrivateClassificationConfig,
) -> None:
    raw_config = config.model_dump(mode="python")
    raw_config["document_number_issuers"][0]["canonical_issuer"] = (
        "synth.person.external"
    )
    person_issuer_config = PrivateClassificationConfig.model_validate(raw_config)
    evidence = collect_metadata_evidence(
        ClassificationItem(
            item_key="synthetic-person-issuer",
            title="Synthetic incoming formal item",
            initiator="synth.person.internal",
            document_number="SYN-AUTH-2026-99",
        ),
        person_issuer_config,
    )

    outcome = classify_from_metadata(evidence)

    assert outcome.classification_status == "needs_review"
    assert outcome.canonical_issuer is None


def test_outcome_dtos_are_immutable(config: PrivateClassificationConfig) -> None:
    evidence = collect_metadata_evidence(
        ClassificationItem(
            item_key="synthetic-immutable",
            title="Synthetic internal approval: immutable",
            initiator="synth.person.internal",
        ),
        config,
    )
    outcome = classify_from_metadata(evidence)

    with pytest.raises((AttributeError, TypeError)):
        outcome.content_origin = "external"  # type: ignore[misc]
    assert replace(outcome, confidence=0.95).confidence == 0.95
