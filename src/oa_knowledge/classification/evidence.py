from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .schemas import ContentOrigin, InitiatorRole

EvidenceScope = Literal["package", "attachment"]
DecisionSource = Literal["metadata_rule", "content_rule", "local_qwen"]
EscalationAction = Literal["resolved", "parse_attachment", "needs_review"]


@dataclass(frozen=True, slots=True)
class AttachmentCandidate:
    attachment_key: str
    filename: str
    source_file_id: int | None = None
    document_number: str | None = None
    issuer: str | None = None


@dataclass(frozen=True, slots=True)
class ClassificationItem:
    item_key: str
    title: str
    initiator: str
    document_number: str | None = None
    issuer: str | None = None
    transfer_people: tuple[str, ...] = ()
    attachments: tuple[AttachmentCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class TransferHop:
    ordinal: int
    person_identifier: str
    role_type: InitiatorRole
    from_person: str | None
    to_person: str
    evidence_source: str


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_scope: EvidenceScope
    code: str
    priority: int
    confidence: float
    decision_source: DecisionSource = "metadata_rule"
    content_origin: ContentOrigin | None = None
    flow_type: str | None = None
    initiator_role: InitiatorRole | None = None
    business_category: str | None = None
    issuer: str | None = None
    canonical_issuer: str | None = None
    document_number: str | None = None
    document_type: str | None = None
    normalized_title: str | None = None
    attachment_key: str | None = None
    source_file_id: int | None = None
    person_identifier: str | None = None
    transfer_ordinal: int | None = None
    transfer_from: str | None = None
    transfer_to: str | None = None
    issuer_resolution_status: (
        Literal["resolved", "unrecognized", "ambiguous"] | None
    ) = None
    blocks_lower_priority: bool = False


@dataclass(frozen=True, slots=True)
class IssuerResolution:
    raw_issuer: str
    status: Literal["resolved", "unrecognized", "ambiguous"]
    canonical_issuer: str | None


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    classification_status: Literal["classified", "needs_review"]
    content_origin: ContentOrigin | None
    flow_type: str | None
    initiator_role: InitiatorRole | None
    business_category: str | None
    issuer: str | None
    canonical_issuer: str | None
    document_number: str | None
    document_type: str | None
    normalized_title: str
    decision_source: DecisionSource | None
    confidence: float
    transfer_chain: tuple[TransferHop, ...]
    relay_from: str | None
    escalation_action: EscalationAction
    conflict_codes: tuple[str, ...] = ()
