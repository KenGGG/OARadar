from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from .evidence import (
    ClassificationItem,
    Evidence,
    IssuerResolution,
    RuleOutcome,
    TransferHop,
)
from .internal_classification import extract_document_type
from .schemas import InitiatorProfile, PrivateClassificationConfig

_DATE_WRAPPER = re.compile(
    r"^\s*[【\[]\s*\d{1,2}月\d{1,2}日(?:\s+\d{1,2}时(?:\d{1,2}分)?)?\s*[】\]]\s*"
)
_ORDINAL_WRAPPER = re.compile(r"^\s*[（(]\s*第\s*\d+\s*次\s*[）)]\s*")
_PERSON_ORGANIZATION = re.compile(r"^\s*(?P<person>[^()（）]+?)\s*[()（](?P<organization>[^()（）]+)[)）]\s*$")
_FILE_TRANSFER = re.compile(r"(?:文件传阅|传阅件|【传阅】|由.+原发)")
_SELF_ISSUERS = frozenset({"广州凯得融资租赁有限公司"})


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def normalize_person(value: str) -> tuple[str, str | None]:
    """Return the canonical person token and separately retained OA organization."""
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    match = _PERSON_ORGANIZATION.fullmatch(normalized)
    if match is None:
        return normalized, None
    return match.group("person").strip(), match.group("organization").strip()


def _issuer_selection_key(value: str) -> tuple[str, str, str]:
    normalized_spelling = " ".join(unicodedata.normalize("NFKC", value).split())
    return (_key(value), normalized_spelling, value)


def _normalize_title(raw: str) -> str:
    title = unicodedata.normalize("NFKC", raw)
    while True:
        cleaned = _DATE_WRAPPER.sub("", title, count=1)
        cleaned = _ORDINAL_WRAPPER.sub("", cleaned, count=1)
        if cleaned == title:
            break
        title = cleaned
    return " ".join(title.split())


def normalize_issuer(raw: str, aliases: Mapping[str, str]) -> IssuerResolution:
    raw_key = _key(raw)
    candidates: set[str] = set()
    for alias, canonical in aliases.items():
        if _key(alias) == raw_key:
            candidates.add(canonical.strip())
    if len(candidates) == 1:
        return IssuerResolution(
            raw_issuer=raw, status="resolved", canonical_issuer=next(iter(candidates))
        )
    if len(candidates) > 1:
        return IssuerResolution(
            raw_issuer=raw, status="ambiguous", canonical_issuer=None
        )
    return IssuerResolution(
        raw_issuer=raw, status="unrecognized", canonical_issuer=None
    )


def _initiator_index(
    config: PrivateClassificationConfig,
) -> dict[str, tuple[str, InitiatorProfile]]:
    index: dict[str, tuple[str, InitiatorProfile]] = {}
    for identifier, profile in config.initiators.items():
        for name in (identifier, *profile.aliases):
            canonical, organization = normalize_person(name)
            key = _key(canonical)
            # A base-name declaration is authoritative over a legacy
            # organization-suffixed declaration for the same person.
            if key not in index or organization is None:
                index[key] = (canonical, profile)
    return index


def _is_configured_person(
    value: str | None, config: PrivateClassificationConfig
) -> bool:
    if value is None:
        return False
    canonical, _organization = normalize_person(value)
    return _key(canonical) in _initiator_index(config)


def _document_evidence(
    document_number: str,
    *,
    scope: str,
    attachment_key: str | None,
    source_file_id: int | None,
    config: PrivateClassificationConfig,
) -> list[Evidence]:
    entries: list[Evidence] = []
    for rule in config.document_number_issuers:
        if re.search(rule.pattern, document_number):
            invalid_issuer = _is_configured_person(rule.canonical_issuer, config)
            is_self_issuer = rule.canonical_issuer in _SELF_ISSUERS
            entries.append(
                Evidence(
                    evidence_scope=scope,  # type: ignore[arg-type]
                    attachment_key=attachment_key,
                    source_file_id=source_file_id,
                    code="document_number",
                    priority=1,
                    confidence=0.99,
                    content_origin="internal" if is_self_issuer else "external",
                    flow_type="approval" if is_self_issuer else "formal_document",
                    issuer=None if is_self_issuer else rule.canonical_issuer,
                    canonical_issuer=None if is_self_issuer else rule.canonical_issuer,
                    document_number=document_number,
                    document_type=rule.document_type,
                    issuer_resolution_status="resolved",
                    blocks_lower_priority=invalid_issuer,
                )
            )
    return entries


def _issuer_evidence(
    issuer: str,
    *,
    scope: str,
    attachment_key: str | None,
    source_file_id: int | None,
    config: PrivateClassificationConfig,
) -> Evidence:
    resolution = normalize_issuer(issuer, config.issuer_aliases)
    invalid_issuer = _is_configured_person(resolution.canonical_issuer, config)
    return Evidence(
        evidence_scope=scope,  # type: ignore[arg-type]
        attachment_key=attachment_key,
        source_file_id=source_file_id,
        code="explicit_issuer",
        priority=1,
        confidence=0.99,
        content_origin="external" if resolution.status == "resolved" else None,
        flow_type="formal_document",
        issuer=issuer,
        canonical_issuer=resolution.canonical_issuer,
        issuer_resolution_status=resolution.status,
        blocks_lower_priority=resolution.status != "resolved" or invalid_issuer,
    )


def collect_metadata_evidence(
    item: ClassificationItem, config: PrivateClassificationConfig
) -> list[Evidence]:
    normalized_title = _normalize_title(item.title)
    evidence: list[Evidence] = [
        Evidence(
            evidence_scope="package",
            code="normalized_title",
            priority=99,
            confidence=0.0,
            normalized_title=normalized_title,
        )
    ]
    document_type = extract_document_type(normalized_title)
    if document_type:
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="title_document_type",
                priority=99,
                confidence=0.9,
                document_type=document_type,
            )
        )

    if item.document_number:
        evidence.extend(
            _document_evidence(
                item.document_number,
                scope="package",
                attachment_key=None,
                source_file_id=None,
                config=config,
            )
        )
    if item.issuer:
        evidence.append(
            _issuer_evidence(
                item.issuer,
                scope="package",
                attachment_key=None,
                source_file_id=None,
                config=config,
            )
        )
    if _FILE_TRANSFER.search(normalized_title):
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="file_transfer",
                priority=2,
                confidence=0.95,
                content_origin="external",
                flow_type="transfer",
            )
        )

    for template in config.title_templates:
        if not re.search(template.pattern, normalized_title):
            continue
        is_external = template.content_origin == "external"
        invalid_issuer = is_external and _is_configured_person(
            template.canonical_issuer, config
        )
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="external_template" if is_external else "internal_template",
                priority=2 if is_external else 3,
                confidence=0.97 if is_external else 0.96,
                content_origin=template.content_origin,
                flow_type=template.flow_type,
                business_category=template.business_category
                if not is_external
                else None,
                issuer=template.canonical_issuer if is_external else None,
                canonical_issuer=template.canonical_issuer if is_external else None,
                normalized_title=normalized_title,
                issuer_resolution_status="resolved"
                if is_external and template.canonical_issuer
                else None,
                blocks_lower_priority=invalid_issuer,
            )
        )

    people = _initiator_index(config)
    canonical_person, organization = normalize_person(item.initiator)
    initiator_match = people.get(_key(canonical_person))
    if initiator_match is not None:
        identifier, profile = initiator_match
        origin = profile.role if profile.role in ("internal", "external") else None
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="initiator_role",
                priority=5,
                confidence=0.86 if origin else 0.0,
                content_origin=origin,
                flow_type="initiated",
                initiator_role=profile.role,
                person_identifier=identifier,
                organization=organization,
            )
        )
    else:
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="initiator_role",
                priority=5,
                confidence=0.0,
                initiator_role="unknown",
                person_identifier=canonical_person,
                organization=organization,
            )
        )

    previous: str | None = None
    for ordinal, raw_person in enumerate(item.transfer_people, start=1):
        canonical_person, organization = normalize_person(raw_person)
        match = people.get(_key(canonical_person))
        identifier = match[0] if match else canonical_person
        role = match[1].role if match else "unknown"
        origin = role if role in ("internal", "external") else None
        evidence.append(
            Evidence(
                evidence_scope="package",
                code="structured_transfer",
                priority=4,
                confidence=0.92 if origin else 0.0,
                content_origin=origin,
                flow_type="transfer",
                initiator_role=role,
                person_identifier=identifier,
                organization=organization,
                transfer_ordinal=ordinal,
                transfer_from=previous,
                transfer_to=identifier,
                blocks_lower_priority=origin is None,
            )
        )
        previous = identifier

    for attachment in item.attachments:
        evidence.append(
            Evidence(
                evidence_scope="attachment",
                attachment_key=attachment.attachment_key,
                source_file_id=attachment.source_file_id,
                code="attachment_candidate",
                priority=99,
                confidence=0.0,
            )
        )
        if attachment.document_number:
            evidence.extend(
                _document_evidence(
                    attachment.document_number,
                    scope="attachment",
                    attachment_key=attachment.attachment_key,
                    source_file_id=attachment.source_file_id,
                    config=config,
                )
            )
        if attachment.issuer:
            evidence.append(
                _issuer_evidence(
                    attachment.issuer,
                    scope="attachment",
                    attachment_key=attachment.attachment_key,
                    source_file_id=attachment.source_file_id,
                    config=config,
                )
            )

    return sorted(evidence, key=_evidence_sort_key)


def find_configured_document_number(
    text: str, config: PrivateClassificationConfig
) -> str | None:
    """Return the first configured formal number present in existing metadata."""
    for rule in config.document_number_issuers:
        match = re.search(rule.pattern, text)
        if match is not None:
            return match.group(0).strip()
    return None


def resolve_configured_document_issuer(
    text: str, config: PrivateClassificationConfig
) -> tuple[str, str, str | None] | None:
    """Resolve a configured formal document number found in parsed content.

    The return value is ``(document_number, canonical_issuer, document_type)``.
    A self-issued number is deliberately not returned as an external issuer:
    callers must keep that item on the internal classification path.
    """
    for rule in config.document_number_issuers:
        match = re.search(rule.pattern, text)
        if match is not None and rule.canonical_issuer not in _SELF_ISSUERS:
            return match.group(0).strip(), rule.canonical_issuer, rule.document_type
    return None


def resolve_issuer_from_text(
    title: str, bodies: Sequence[str], config: PrivateClassificationConfig
) -> str | None:
    """Resolve a known issuer from a title, red-header cue, or signature.

    Only configured aliases are accepted here.  This keeps deterministic
    extraction auditable and leaves genuinely new organizations for the
    bounded issuer-extraction fallback rather than inventing a canonical name.
    """
    text = "\n".join((title, *bodies))
    cue = re.search(r"(?:发文机关|发文单位|落款)\s*[:：]\s*([^\n]{2,80})", text)
    candidates: list[str] = []
    if cue is not None:
        candidates.append(cue.group(1).strip(" ：:，,。."))
    candidates.extend(
        alias for alias in config.issuer_aliases if alias and alias in text
    )
    for raw in sorted(candidates, key=len, reverse=True):
        normalized = normalize_issuer(raw, config.issuer_aliases)
        if normalized.status == "resolved":
            return normalized.canonical_issuer
    return None


def extract_issuer_candidate(title: str, bodies: Sequence[str]) -> str | None:
    """Extract a plausible issuing organization without treating it as canonical."""
    text = "\n".join((title, *bodies))
    patterns = (
        r"(?:发文机关|发文单位|落款)\s*[:：]\s*([^\n]{2,80})",
        r"(?:^|\n)\s*([^\n]{2,60}?(?:有限公司|委员会|人民政府|管理委员会|监督管理局|办公室))\s*(?:关于|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            candidate = match.group(1).strip(" ：:，,。.")
            if 2 <= len(candidate) <= 80:
                return candidate
    return None


def _evidence_sort_key(entry: Evidence) -> tuple[object, ...]:
    return (
        entry.evidence_scope,
        entry.priority,
        entry.code,
        entry.attachment_key or "",
        entry.transfer_ordinal or 0,
        entry.canonical_issuer or "",
        entry.person_identifier or "",
    )


def build_transfer_chain(
    item: ClassificationItem, evidence: Sequence[Evidence]
) -> list[TransferHop]:
    del item
    transfer_evidence = sorted(
        (
            entry
            for entry in evidence
            if entry.evidence_scope == "package"
            and entry.code == "structured_transfer"
            and entry.transfer_ordinal is not None
            and entry.person_identifier is not None
            and entry.initiator_role is not None
        ),
        key=lambda entry: (entry.transfer_ordinal or 0, entry.person_identifier or ""),
    )
    return [
        TransferHop(
            ordinal=entry.transfer_ordinal or 0,
            person_identifier=entry.person_identifier or "",
            role_type=entry.initiator_role or "unknown",
            from_person=entry.transfer_from,
            to_person=entry.transfer_to or entry.person_identifier or "",
            evidence_source=entry.code,
        )
        for entry in transfer_evidence
    ]


def _transfer_chain_from_evidence(
    evidence: Sequence[Evidence],
) -> tuple[TransferHop, ...]:
    placeholder = ClassificationItem(item_key="", title="", initiator="")
    return tuple(build_transfer_chain(placeholder, evidence))


def _unresolved(
    evidence: Sequence[Evidence],
    *,
    confidence: float,
    conflict_codes: tuple[str, ...] = (),
    content_origin: str | None = None,
    flow_type: str | None = None,
) -> RuleOutcome:
    chain = _transfer_chain_from_evidence(evidence)
    title = next(
        (entry.normalized_title for entry in evidence if entry.normalized_title), ""
    )
    initiator_role = next(
        (entry.initiator_role for entry in evidence if entry.code == "initiator_role"),
        None,
    )
    has_attachment = any(entry.evidence_scope == "attachment" for entry in evidence)
    return RuleOutcome(
        classification_status="needs_review",
        content_origin=content_origin,  # type: ignore[arg-type]
        flow_type=flow_type,
        initiator_role=initiator_role,
        business_category=None,
        issuer=None,
        canonical_issuer=None,
        document_number=None,
        document_type=None,
        normalized_title=title,
        decision_source=None,
        confidence=confidence,
        transfer_chain=chain,
        relay_from=chain[-2].person_identifier if len(chain) > 1 else None,
        escalation_action="parse_attachment" if has_attachment else "needs_review",
        conflict_codes=conflict_codes,
    )


def classify_from_metadata(evidence: Sequence[Evidence]) -> RuleOutcome:
    ordered = sorted(evidence, key=_evidence_sort_key)
    package_candidates = [
        entry
        for entry in ordered
        if entry.evidence_scope == "package"
        and (entry.content_origin is not None or entry.blocks_lower_priority)
    ]
    if not package_candidates:
        return _unresolved(ordered, confidence=0.0)

    winning_priority = min(entry.priority for entry in package_candidates)
    winners = [
        entry for entry in package_candidates if entry.priority == winning_priority
    ]
    confidence = max(entry.confidence for entry in winners)
    if any(entry.blocks_lower_priority for entry in winners):
        return _unresolved(
            ordered,
            confidence=confidence,
            conflict_codes=tuple(sorted({entry.code for entry in winners})),
        )

    decision_fields = (
        "content_origin",
        "flow_type",
        "canonical_issuer",
        "business_category",
        "document_number",
        "document_type",
    )
    resolved_values = {
        field: {
            value for entry in winners if (value := getattr(entry, field)) is not None
        }
        for field in decision_fields
    }
    if any(len(values) > 1 for values in resolved_values.values()):
        return _unresolved(
            ordered,
            confidence=confidence,
            conflict_codes=tuple(sorted({entry.code for entry in winners})),
        )

    origin = next(iter(resolved_values["content_origin"]), None)
    canonical_issuer = next(iter(resolved_values["canonical_issuer"]), None)
    people = {entry.person_identifier for entry in ordered if entry.person_identifier}
    if origin == "external" and (not canonical_issuer or canonical_issuer in people):
        transfer_winner = any(entry.code == "file_transfer" for entry in winners)
        return _unresolved(
            ordered,
            confidence=confidence,
            content_origin="external" if transfer_winner else None,
            flow_type=(
                next(iter(resolved_values["flow_type"]), None)
                if transfer_winner
                else None
            ),
        )

    chain = _transfer_chain_from_evidence(ordered)
    normalized_title = next(
        (entry.normalized_title for entry in ordered if entry.normalized_title), ""
    )
    initiator_role = next(
        (entry.initiator_role for entry in ordered if entry.code == "initiator_role"),
        None,
    )
    is_internal = origin == "internal"
    document_type = next(
        (entry.document_type for entry in ordered if entry.document_type), None
    )
    issuer = min(
        (entry.issuer for entry in winners if entry.issuer is not None),
        key=_issuer_selection_key,
        default=None,
    )
    business_category = next(iter(resolved_values["business_category"]), None)
    if is_internal and business_category is None:
        has_attachment = any(
            entry.evidence_scope == "attachment" for entry in ordered
        )
        return RuleOutcome(
            classification_status="needs_review",
            content_origin="internal",
            flow_type=next(iter(resolved_values["flow_type"]), None),
            initiator_role=initiator_role,
            business_category=None,
            issuer=None,
            canonical_issuer=None,
            document_number=next(iter(resolved_values["document_number"]), None),
            document_type=document_type,
            normalized_title=normalized_title,
            decision_source=None,
            confidence=confidence,
            transfer_chain=chain,
            relay_from=chain[-2].person_identifier if len(chain) > 1 else None,
            escalation_action="parse_attachment" if has_attachment else "needs_review",
        )
    return RuleOutcome(
        classification_status="classified",
        content_origin=origin,
        flow_type=next(iter(resolved_values["flow_type"]), None),
        initiator_role=initiator_role,
        business_category=business_category if is_internal else None,
        issuer=issuer if not is_internal else None,
        canonical_issuer=canonical_issuer if not is_internal else None,
        document_number=next(iter(resolved_values["document_number"]), None),
        document_type=document_type,
        normalized_title=normalized_title,
        decision_source="metadata_rule",
        confidence=confidence,
        transfer_chain=chain,
        relay_from=chain[-2].person_identifier if len(chain) > 1 else None,
        escalation_action="resolved",
    )
