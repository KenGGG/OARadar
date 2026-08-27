from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import unicodedata

import pytest

import oa_knowledge.classification.fingerprint as fingerprint_module

from oa_knowledge.classification.fingerprint import (
    AttachmentInput,
    DecisionInputs,
    NormalizedOAMetadata,
    ParseArtifactIdentity,
    ReadinessEvidenceInput,
    decision_input_sha256,
    readiness_evidence_from_assessment,
)
from oa_knowledge.classification.readiness import IntegrityAssessment, ReadinessEvidence


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
EMPTY_EVIDENCE_SHA256 = hashlib.sha256(b"[]").hexdigest()


def _inputs() -> DecisionInputs:
    return DecisionInputs(
        oa_item_key="done:synthetic-42",
        normalized_metadata=NormalizedOAMetadata(
            normalized_title="Synthetic transfer notice",
            initiator="person-synthetic-a",
            document_number="SYNTH-2026-0042",
            completed_at=datetime(2026, 8, 26, 8, 30, tzinfo=timezone.utc),
            oa_id_text="oa-synthetic-42",
            workitem_id_text="work-synthetic-42",
            process_id_text="process-synthetic-42",
            affair_id_text="affair-synthetic-42",
            summary_id_text="summary-synthetic-42",
        ),
        manifest_status="downloaded",
        excluded=False,
        exclusion_matches=("policy-synthetic-1",),
        content_integrity_status="ok",
        readiness_evidence=ReadinessEvidenceInput(
            manifest_processing_status="downloaded",
            manifest_no_attachment_confirmed=False,
            reason_codes=("VERIFIED_STORED_EVIDENCE",),
        ),
        attachments=(
            AttachmentInput(
                attachment_key="attachment-b",
                file_role="official_attachment",
                original_filename="synthetic-b.pdf",
                display_filename="Synthetic B",
                source_container_key="container-1",
                parent_container_key="root",
                container_path=("root", "container-1"),
                parent_attachment_key="attachment-a",
                local_relpath=Path("originals/done/synthetic/b.pdf"),
                size_bytes=20,
                expected_size=20,
                sha256=HASH_B.upper(),
                content_sha256=HASH_B,
                integrity_status="ok",
                depth=2,
            ),
            AttachmentInput(
                attachment_key="attachment-a",
                file_role="official_body",
                original_filename="synthetic-a.pdf",
                display_filename="Synthetic A",
                source_container_key="root",
                parent_container_key=None,
                container_path=("root",),
                local_relpath="originals/done/synthetic/a.pdf",
                size_bytes=10,
                expected_size=10,
                sha256=HASH_A,
                content_sha256=HASH_A,
                integrity_status="ok",
                depth=1,
            ),
        ),
        transfer_evidence=(
            {"ordinal": 2, "from": "person-synthetic-b", "to": "person-synthetic-c"},
            {"to": "person-synthetic-b", "from": "person-synthetic-a", "ordinal": 1},
        ),
        used_parse_artifacts=(
            ParseArtifactIdentity(
                content_sha256=HASH_B,
                parser_name="mineru",
                parser_version="2.1",
                parse_profile_version="classification-v1",
                parse_config_sha256=HASH_A,
            ),
        ),
        manual_decision_version=3,
        rule_version="rules-v1",
        schema_version="classification-v1",
        prompt_version="qwen-v1",
        private_config_sha256=HASH_B,
    )


def test_fingerprint_is_a_lowercase_sha256_and_stable_across_row_mapping_and_relationship_order() -> None:
    original = _inputs()
    reordered = replace(
        original,
        normalized_metadata=replace(
            original.normalized_metadata,
            completed_at="2026-08-26T16:30:00+08:00",
        ),
        attachments=tuple(reversed(original.attachments)),
        transfer_evidence=tuple(reversed(original.transfer_evidence)),
    )

    digest = decision_input_sha256(original)

    assert digest == decision_input_sha256(reordered)
    assert len(digest) == 64
    assert digest == digest.lower()


@pytest.mark.parametrize(
    "changed",
    [
        lambda value: replace(
            value,
            normalized_metadata=replace(value.normalized_metadata, normalized_title="Changed"),
        ),
        lambda value: replace(
            value,
            manifest_status="download_failed",
            content_integrity_status="download_failed",
            readiness_evidence=replace(
                value.readiness_evidence,
                manifest_processing_status="download_failed",
                reason_codes=("DOWNLOAD_FAILED",),
            ),
        ),
        lambda value: replace(value, excluded=True),
        lambda value: replace(value, exclusion_matches=("policy-synthetic-2",)),
        lambda value: replace(
            value, attachments=value.attachments[1:], used_parse_artifacts=()
        ),
        lambda value: replace(
            value,
            attachments=(replace(value.attachments[0], sha256=HASH_A), *value.attachments[1:]),
        ),
        lambda value: replace(
            value,
            attachments=(replace(value.attachments[0], parent_attachment_key=None), *value.attachments[1:]),
        ),
        lambda value: replace(
            value,
            transfer_evidence=({"ordinal": 1, "from": "changed", "to": "person-synthetic-b"},),
        ),
        lambda value: replace(value, used_parse_artifacts=()),
        lambda value: replace(value, manual_decision_version=4),
        lambda value: replace(value, rule_version="rules-v2"),
        lambda value: replace(value, schema_version="classification-v2"),
        lambda value: replace(value, prompt_version="qwen-v2"),
        lambda value: replace(value, private_config_sha256=HASH_A),
        lambda value: replace(
            value,
            content_integrity_status="missing",
            readiness_evidence=replace(
                value.readiness_evidence,
                reason_codes=("AUDIT_INVENTORY_MISMATCH",),
            ),
        ),
        lambda value: replace(
                value,
                content_integrity_status="missing",
                readiness_evidence=replace(
                    value.readiness_evidence,
                    audit_evidence_mode="full",
                    audit_comparison_reason="inventory_changed",
                    audit_status="missing_download",
                    audit_finished_at="2026-08-26T09:00:00Z",
                    audit_recognized_attachments=2,
                    audit_database_attachments=2,
                    audit_downloaded_attachments=1,
                    audit_online_inventory_sha256=HASH_A,
                    audit_local_inventory_sha256=HASH_B,
                    audit_online_content_sha256=HASH_A,
                    audit_local_content_sha256=HASH_B,
                    audit_online_evidence_sha256=HASH_A,
                    audit_local_evidence_sha256=HASH_B,
                    audit_online_evidence_count=2,
                    audit_local_evidence_count=1,
                    reason_codes=("AUDIT_INVENTORY_MISMATCH",),
                ),
            ),
        lambda value: replace(
            value,
            attachments=(
                replace(value.attachments[0], original_filename="renamed-b.pdf"),
                *value.attachments[1:],
            ),
        ),
        lambda value: replace(
            value,
            attachments=(
                replace(value.attachments[0], display_filename="Renamed B"),
                *value.attachments[1:],
            ),
        ),
        lambda value: replace(
            value,
            attachments=(
                replace(
                    value.attachments[0],
                    source_container_key="container-2",
                    parent_container_key="root",
                    container_path=("root", "container-2"),
                ),
                *value.attachments[1:],
            ),
        ),
    ],
    ids=(
        "normalized-metadata",
        "manifest-state",
        "exclusion-state",
        "exclusion-match",
        "attachment-inventory",
        "attachment-hash",
        "attachment-relationship",
        "transfer-evidence",
        "used-parse-artifacts",
        "manual-decision-version",
        "rule-version",
        "schema-version",
        "prompt-version",
        "private-config-hash",
        "package-integrity-status",
        "readiness-evidence-freshness",
        "original-filename",
        "display-filename",
        "container-topology",
    ),
)
def test_every_decision_input_change_changes_only_that_item(changed) -> None:
    base = _inputs()

    assert decision_input_sha256(changed(base)) != decision_input_sha256(base)


def test_unrelated_global_rows_are_not_part_of_a_per_item_fingerprint() -> None:
    first = _inputs()
    second = replace(
        first,
        oa_item_key="done:unrelated-9",
        normalized_metadata=replace(first.normalized_metadata, normalized_title="Unrelated A"),
    )
    before = {
        first.oa_item_key: decision_input_sha256(first),
        second.oa_item_key: decision_input_sha256(second),
    }
    changed_second = replace(
        second,
        normalized_metadata=replace(second.normalized_metadata, normalized_title="Unrelated B"),
    )
    after = {
        first.oa_item_key: decision_input_sha256(first),
        changed_second.oa_item_key: decision_input_sha256(changed_second),
    }

    assert after[first.oa_item_key] == before[first.oa_item_key]
    assert after[second.oa_item_key] != before[second.oa_item_key]


def test_parse_profile_and_parser_versions_are_part_of_the_reuse_identity() -> None:
    inputs = _inputs()
    identity = inputs.used_parse_artifacts[0]

    assert decision_input_sha256(
        replace(inputs, used_parse_artifacts=(replace(identity, parser_version="2.2"),))
    ) != decision_input_sha256(inputs)
    assert decision_input_sha256(
        replace(inputs, used_parse_artifacts=(replace(identity, parse_profile_version="classification-v2"),))
    ) != decision_input_sha256(inputs)


def test_only_artifacts_actually_used_by_this_item_are_hashed() -> None:
    inputs = _inputs()
    unused_global_artifact = ParseArtifactIdentity(
        content_sha256=HASH_A,
        parser_name="markitdown",
        parser_version="1.0",
        parse_profile_version="unrelated",
        parse_config_sha256=HASH_B,
    )

    before = decision_input_sha256(inputs)
    _global_parse_rows = [*inputs.used_parse_artifacts, unused_global_artifact]

    assert decision_input_sha256(inputs) == before


@pytest.mark.parametrize(
    "invalid",
    [
        lambda value: replace(value, attachments=(replace(value.attachments[0], local_relpath="/absolute/file.pdf"),)),
        lambda value: replace(value, attachments=(replace(value.attachments[0], local_relpath="originals/../secret.pdf"),)),
        lambda value: replace(value, attachments=(replace(value.attachments[0], sha256="not-a-hash"),)),
        lambda value: replace(value, private_config_sha256="f" * 63),
        lambda value: replace(value, normalized_metadata={"bad": Decimal("1.5")}),
        lambda value: replace(value, normalized_metadata={1: "non-text mapping key"}),
        lambda value: replace(value, normalized_metadata={"not_finite": float("nan")}),
        lambda value: replace(value, transfer_evidence=({"bad": {"set-is-lossy"}},)),
    ],
    ids=(
        "absolute-path",
        "traversal-path",
        "invalid-attachment-hash",
        "invalid-config-hash",
        "unsupported-number",
        "non-text-key",
        "non-finite-float",
        "unordered-set",
    ),
)
def test_invalid_or_lossy_inputs_are_rejected(invalid) -> None:
    with pytest.raises((TypeError, ValueError)):
        decision_input_sha256(invalid(_inputs()))


def test_duplicate_conflicting_attachment_identity_is_rejected() -> None:
    inputs = _inputs()
    duplicate = replace(inputs.attachments[0], sha256=HASH_A)

    with pytest.raises(ValueError, match="duplicate attachment identity"):
        decision_input_sha256(replace(inputs, attachments=(*inputs.attachments, duplicate)))


def test_duplicate_conflicting_parse_reuse_identity_is_rejected() -> None:
    inputs = _inputs()
    identity = inputs.used_parse_artifacts[0]
    duplicate = replace(identity, source_relpath="originals/done/synthetic/other.pdf")

    with pytest.raises(ValueError, match="duplicate parse artifact identity"):
        decision_input_sha256(
            replace(inputs, used_parse_artifacts=(*inputs.used_parse_artifacts, duplicate))
        )


def test_duplicate_conflicting_transfer_ordinal_is_rejected() -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="duplicate transfer relationship identity"):
        decision_input_sha256(
            replace(
                inputs,
                transfer_evidence=(
                    {"ordinal": 1, "from": "person-a", "to": "person-b"},
                    {"ordinal": 1, "from": "person-a", "to": "person-c"},
                ),
            )
        )


def test_attachment_depth_beyond_archive_limit_is_rejected() -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="depth must be at most 10"):
        decision_input_sha256(
            replace(
                inputs,
                attachments=(
                    replace(inputs.attachments[0], depth=11),
                    *inputs.attachments[1:],
                ),
            )
        )


def test_equivalent_text_ids_paths_hashes_and_timestamps_normalize_identically() -> None:
    inputs = _inputs()
    numeric_keys = {
        attachment.attachment_key: int(str(attachment.attachment_key)[-1], 36)
        for attachment in inputs.attachments
    }
    normalized = replace(
        inputs,
        oa_item_key=42,
        normalized_metadata=replace(
            inputs.normalized_metadata,
            completed_at=datetime(
                2026, 8, 26, 16, 30, tzinfo=timezone(timedelta(hours=8))
            ),
        ),
        attachments=tuple(
            replace(
                attachment,
                attachment_key=numeric_keys[attachment.attachment_key],
                parent_attachment_key=(
                    numeric_keys[attachment.parent_attachment_key]
                    if attachment.parent_attachment_key is not None
                    else None
                ),
                local_relpath=str(attachment.local_relpath).replace("/synthetic/", "/synthetic/./"),
                sha256=attachment.sha256.upper() if attachment.sha256 else None,
            )
            for attachment in inputs.attachments
        ),
    )
    textual = replace(
        normalized,
        oa_item_key="42",
        attachments=tuple(
            replace(
                attachment,
                attachment_key=str(attachment.attachment_key),
                parent_attachment_key=(
                    str(attachment.parent_attachment_key)
                    if attachment.parent_attachment_key is not None
                    else None
                ),
            )
            for attachment in normalized.attachments
        ),
    )

    assert decision_input_sha256(normalized) == decision_input_sha256(textual)


def test_transfer_relationship_text_ids_normalize_identically() -> None:
    inputs = _inputs()
    numeric = replace(
        inputs,
        transfer_evidence=({"ordinal": 1, "from": 42, "to": 43},),
    )
    textual = replace(
        inputs,
        transfer_evidence=({"to": "43", "from": "42", "ordinal": 1},),
    )

    assert decision_input_sha256(numeric) == decision_input_sha256(textual)


def test_decision_contract_has_explicit_typed_package_readiness_inputs() -> None:
    fields = DecisionInputs.__dataclass_fields__

    assert "content_integrity_status" in fields
    assert "readiness_evidence" in fields
    assert hasattr(fingerprint_module, "ReadinessEvidenceInput")


def test_metadata_contract_is_typed_for_all_designed_text_identifiers() -> None:
    metadata_type = getattr(fingerprint_module, "NormalizedOAMetadata", None)

    assert metadata_type is not None
    assert {
        "oa_id_text",
        "workitem_id_text",
        "process_id_text",
        "affair_id_text",
        "summary_id_text",
    } <= metadata_type.__dataclass_fields__.keys()


def test_attachment_contract_captures_filenames_and_complete_container_topology() -> None:
    fields = AttachmentInput.__dataclass_fields__

    assert {
        "original_filename",
        "display_filename",
        "parent_container_key",
        "container_path",
    } <= fields.keys()


def _full_audit_evidence() -> ReadinessEvidenceInput:
    return replace(
        _inputs().readiness_evidence,
        audit_evidence_mode="full",
        audit_comparison_reason="exact_match",
        audit_status="matched",
        audit_recognized_attachments=1,
        audit_database_attachments=2,
        audit_downloaded_attachments=1,
        audit_online_evidence_count=1,
        audit_local_evidence_count=1,
        audit_online_inventory_sha256=HASH_A,
        audit_local_inventory_sha256=HASH_A,
        audit_online_content_sha256=HASH_B,
        audit_local_content_sha256=HASH_B,
        audit_online_evidence_sha256=HASH_C,
        audit_local_evidence_sha256=HASH_C,
    )


def _count_only_audit_evidence() -> ReadinessEvidenceInput:
    return replace(
        _inputs().readiness_evidence,
        audit_evidence_mode="count_only",
        audit_comparison_reason="evidence_unavailable",
        audit_status="matched",
        audit_recognized_attachments=1,
        audit_database_attachments=2,
        audit_downloaded_attachments=1,
        audit_online_evidence_count=0,
        audit_local_evidence_count=1,
        audit_online_inventory_sha256=None,
        audit_local_inventory_sha256=HASH_A,
        audit_online_content_sha256=None,
        audit_local_content_sha256=HASH_B,
        audit_online_evidence_sha256=EMPTY_EVIDENCE_SHA256,
        audit_local_evidence_sha256=HASH_C,
    )


@pytest.mark.parametrize("evidence", [_full_audit_evidence, _count_only_audit_evidence])
def test_each_coherent_audit_evidence_mode_updates_fingerprint(evidence) -> None:
    inputs = _inputs()

    updated = replace(inputs, readiness_evidence=evidence())

    assert decision_input_sha256(updated) != decision_input_sha256(inputs)


@pytest.mark.parametrize(
    "evidence",
    [
        lambda: replace(_full_audit_evidence(), audit_recognized_attachments=2),
        lambda: replace(_full_audit_evidence(), audit_local_inventory_sha256=HASH_B),
        lambda: replace(_full_audit_evidence(), audit_local_evidence_sha256=None),
        lambda: replace(_count_only_audit_evidence(), audit_online_inventory_sha256=HASH_A),
        lambda: replace(
            _full_audit_evidence(),
            audit_status="missing_download",
            reason_codes=("VERIFIED_STORED_EVIDENCE",),
        ),
    ],
    ids=(
        "impossible-counts",
        "unequal-full-inventory",
        "one-sided-full-evidence",
        "count-only-has-online-hash",
        "terminal-reason-conflicts-audit-status",
    ),
)
def test_contradictory_raw_audit_bundles_are_rejected(evidence) -> None:
    with pytest.raises(ValueError, match="readiness evidence"):
        decision_input_sha256(replace(_inputs(), readiness_evidence=evidence()))


@pytest.mark.parametrize(
    "evidence",
    [
        lambda: replace(
            _count_only_audit_evidence(),
            audit_status="missing_download",
            audit_recognized_attachments=0,
            reason_codes=("AUDIT_INVENTORY_MISMATCH",),
        ),
        lambda: replace(
            _full_audit_evidence(),
            audit_status="content_mismatch",
            reason_codes=("SHA256_MISMATCH",),
        ),
        lambda: replace(
            _full_audit_evidence(),
            audit_status="content_mismatch",
            audit_comparison_reason="exact_match",
            audit_local_content_sha256=HASH_A,
            audit_local_evidence_sha256=HASH_A,
            reason_codes=("SHA256_MISMATCH",),
        ),
    ],
    ids=(
        "missing-download-has-fewer-recognized-than-downloaded",
        "content-mismatch-has-equal-content",
        "exact-match-reason-has-different-evidence",
    ),
)
def test_nonmatched_audit_status_must_be_derived_from_raw_facts(evidence) -> None:
    with pytest.raises(ValueError, match="readiness evidence"):
        decision_input_sha256(
            replace(
                _inputs(),
                content_integrity_status=(
                    "sha256_mismatch"
                    if evidence().audit_status == "content_mismatch"
                    else "missing"
                ),
                readiness_evidence=evidence(),
            )
        )


@pytest.mark.parametrize(
    ("evidence", "integrity_status", "reason_codes"),
    [
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="missing_download",
                audit_comparison_reason="inventory_changed",
                audit_recognized_attachments=2,
                audit_online_evidence_count=2,
                audit_local_inventory_sha256=HASH_B,
                audit_local_content_sha256=HASH_A,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="inventory_mismatch",
                audit_comparison_reason="inventory_changed",
                audit_local_inventory_sha256=HASH_B,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="historical_retained",
                audit_comparison_reason="historical_retained",
                audit_downloaded_attachments=2,
                audit_local_evidence_count=2,
                audit_local_inventory_sha256=HASH_B,
                audit_local_content_sha256=HASH_A,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="content_mismatch",
                audit_comparison_reason="content_changed",
                audit_online_content_sha256=HASH_A,
                audit_local_content_sha256=HASH_B,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "sha256_mismatch",
            ("SHA256_MISMATCH",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="content_unverified",
                audit_online_content_sha256=None,
                audit_local_content_sha256=None,
            ),
            "not_checked",
            ("NOT_CHECKED",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="depth_limit_reached",
                audit_depth_limit_reached=True,
            ),
            "missing",
            ("DEPTH_LIMIT_REACHED",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="access_failed",
                audit_comparison_reason=None,
            ),
            "not_checked",
            ("NOT_CHECKED",),
        ),
        (
            lambda: replace(
                _count_only_audit_evidence(),
                audit_status="missing_download",
                audit_recognized_attachments=2,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
        (
            lambda: replace(
                _count_only_audit_evidence(),
                audit_status="historical_retained",
                audit_downloaded_attachments=2,
                audit_local_evidence_count=2,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
    ],
    ids=(
        "full-missing-download",
        "full-inventory-mismatch",
        "full-historical-retained",
        "full-content-mismatch",
        "full-content-unverified",
        "full-depth-limit",
        "access-failed",
        "count-only-missing-download",
        "count-only-historical-retained",
    ),
)
def test_coherent_nonmatched_audit_producer_shapes_are_accepted(
    evidence, integrity_status: str, reason_codes: tuple[str, ...]
) -> None:
    inputs = _inputs()
    audit_evidence = replace(evidence(), reason_codes=reason_codes)

    digest = decision_input_sha256(
        replace(
            inputs,
            content_integrity_status=integrity_status,
            readiness_evidence=audit_evidence,
        )
    )

    assert len(digest) == 64


@pytest.mark.parametrize(
    ("evidence", "integrity_status", "reason_codes"),
    [
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="historical_retained",
                audit_comparison_reason="historical_retained",
                audit_local_evidence_count=2,
                audit_local_inventory_sha256=HASH_B,
                audit_local_content_sha256=HASH_A,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "missing",
            ("AUDIT_INVENTORY_MISMATCH",),
        ),
        (
            lambda: replace(
                _full_audit_evidence(),
                audit_status="content_unverified",
                audit_comparison_reason="content_changed",
                audit_online_content_sha256=None,
                audit_local_content_sha256=None,
                audit_online_evidence_sha256=HASH_A,
                audit_local_evidence_sha256=HASH_B,
            ),
            "not_checked",
            ("NOT_CHECKED",),
        ),
    ],
    ids=(
        "same-sha-historical-subset-keeps-canonical-download-count",
        "metadata-change-with-both-content-summaries-unverified",
    ),
)
def test_canonical_download_and_unverified_content_producer_edges_are_accepted(
    evidence, integrity_status: str, reason_codes: tuple[str, ...]
) -> None:
    inputs = _inputs()

    digest = decision_input_sha256(
        replace(
            inputs,
            content_integrity_status=integrity_status,
            readiness_evidence=replace(evidence(), reason_codes=reason_codes),
        )
    )

    assert len(digest) == 64


@pytest.mark.parametrize(
    "evidence",
    [
        lambda: replace(
            _full_audit_evidence(),
            audit_status="historical_retained",
            audit_downloaded_attachments=2,
            audit_local_evidence_count=2,
            reason_codes=("AUDIT_INVENTORY_MISMATCH",),
        ),
        lambda: replace(
            _full_audit_evidence(),
            audit_status="inventory_mismatch",
            audit_comparison_reason="content_changed",
            audit_local_inventory_sha256=HASH_B,
            audit_online_evidence_sha256=HASH_A,
            audit_local_evidence_sha256=HASH_B,
            reason_codes=("AUDIT_INVENTORY_MISMATCH",),
        ),
        lambda: replace(
            _full_audit_evidence(),
            audit_status="content_mismatch",
            audit_comparison_reason="inventory_changed",
            audit_online_content_sha256=HASH_A,
            audit_local_content_sha256=HASH_B,
            audit_online_evidence_sha256=HASH_A,
            audit_local_evidence_sha256=HASH_B,
            reason_codes=("SHA256_MISMATCH",),
        ),
    ],
    ids=(
        "equal-ledger-hash-with-different-ledger-counts",
        "content-changed-reason-with-inventory-change",
        "inventory-changed-reason-with-content-only-change",
    ),
)
def test_comparison_reason_must_match_ledger_and_aggregate_facts(evidence) -> None:
    audit_evidence = evidence()
    integrity_status = (
        "sha256_mismatch"
        if audit_evidence.audit_status == "content_mismatch"
        else "missing"
    )

    with pytest.raises(ValueError, match="readiness evidence"):
        decision_input_sha256(
            replace(
                _inputs(),
                content_integrity_status=integrity_status,
                readiness_evidence=audit_evidence,
            )
        )


def test_audit_timestamp_alone_does_not_invalidate_unchanged_effective_inputs() -> None:
    inputs = _inputs()
    earlier_evidence = replace(
        _full_audit_evidence(), audit_finished_at="2026-08-26T09:00:00Z"
    )
    later_evidence = replace(
        earlier_evidence, audit_finished_at="2026-08-27T09:00:00Z"
    )

    assert decision_input_sha256(
        replace(inputs, readiness_evidence=earlier_evidence)
    ) == decision_input_sha256(replace(inputs, readiness_evidence=later_evidence))


@pytest.mark.parametrize(
    "evidence",
    [
        lambda value: replace(value, audit_finished_at="2026-08-27T09:00:00Z"),
        lambda value: replace(value, audit_depth_limit_reached=True),
    ],
    ids=("timestamp", "depth-limit"),
)
def test_no_audit_mode_rejects_orphan_audit_facts(evidence) -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="readiness evidence"):
        decision_input_sha256(
            replace(inputs, readiness_evidence=evidence(inputs.readiness_evidence))
        )


def test_package_integrity_status_is_strictly_validated() -> None:
    with pytest.raises(ValueError, match="content_integrity_status"):
        decision_input_sha256(replace(_inputs(), content_integrity_status="verified"))


def test_foreign_parse_artifact_content_is_rejected() -> None:
    inputs = _inputs()
    foreign = replace(inputs.used_parse_artifacts[0], content_sha256=HASH_C)

    with pytest.raises(ValueError, match="package attachment content"):
        decision_input_sha256(replace(inputs, used_parse_artifacts=(foreign,)))


def test_file_sha_cannot_substitute_for_parse_content_object_identity() -> None:
    inputs = _inputs()
    attachment = replace(
        inputs.attachments[0],
        sha256=HASH_A,
        content_sha256=HASH_B,
        parent_attachment_key=None,
    )
    artifact = replace(inputs.used_parse_artifacts[0], content_sha256=HASH_A)

    with pytest.raises(ValueError, match="package attachment content"):
        decision_input_sha256(
            replace(
                inputs,
                attachments=(attachment,),
                used_parse_artifacts=(artifact,),
            )
        )


@pytest.mark.parametrize(
    "attachments",
    [
        lambda rows: (replace(rows[0], parent_attachment_key="outside-package"), rows[1]),
        lambda rows: (replace(rows[0], parent_attachment_key=rows[0].attachment_key), rows[1]),
        lambda rows: (
            rows[0],
            replace(rows[1], parent_attachment_key=rows[0].attachment_key),
        ),
    ],
    ids=("missing-parent", "self-parent", "cycle"),
)
def test_invalid_attachment_parent_topology_is_rejected(attachments) -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="attachment parent topology"):
        decision_input_sha256(replace(inputs, attachments=attachments(inputs.attachments)))


@pytest.mark.parametrize(
    "changed",
    [
        lambda row: replace(row, parent_container_key="outside-package"),
        lambda row: replace(row, container_path=("root", "wrong-container")),
        lambda row: replace(row, container_path=("container-1",)),
    ],
    ids=("wrong-parent-container", "wrong-path-leaf", "wrong-depth"),
)
def test_inconsistent_container_topology_is_rejected(changed) -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="container topology"):
        decision_input_sha256(
            replace(
                inputs,
                attachments=(changed(inputs.attachments[0]), *inputs.attachments[1:]),
            )
        )


def test_empty_parent_container_is_valid_when_declared_by_child_path() -> None:
    inputs = _inputs()
    child = replace(inputs.attachments[0], parent_attachment_key=None)

    digest = decision_input_sha256(
        replace(inputs, attachments=(child,), used_parse_artifacts=inputs.used_parse_artifacts)
    )

    assert len(digest) == 64


def test_same_container_key_cannot_declare_two_parent_paths() -> None:
    inputs = _inputs()
    first = replace(inputs.attachments[0], parent_attachment_key=None)
    second = replace(
        inputs.attachments[1],
        source_container_key=first.source_container_key,
        parent_container_key="root-2",
        container_path=("root-2", first.source_container_key),
        depth=2,
    )

    with pytest.raises(ValueError, match="container topology"):
        decision_input_sha256(
            replace(inputs, attachments=(first, second), used_parse_artifacts=())
        )


def test_readiness_adapter_is_lossless_and_inconsistent_inputs_are_rejected() -> None:
    assessment = IntegrityAssessment(
        content_integrity_status="missing",
        publishable=False,
        reason_codes=("DEPTH_LIMIT_REACHED",),
        evidence=ReadinessEvidence(
            manifest_processing_status="depth_limit_reached",
            manifest_no_attachment_confirmed=False,
            audit_evidence_mode="full",
            audit_comparison_reason="exact_match",
            audit_status="depth_limit_reached",
            audit_finished_at=datetime(2026, 8, 26, 9, tzinfo=timezone.utc),
            audit_recognized_attachments=1,
            audit_database_attachments=1,
            audit_downloaded_attachments=1,
            audit_online_inventory_sha256=HASH_A,
            audit_local_inventory_sha256=HASH_A,
            audit_online_content_sha256=HASH_B,
            audit_local_content_sha256=HASH_B,
            audit_online_evidence_sha256=HASH_C,
            audit_local_evidence_sha256=HASH_C,
            audit_online_evidence_count=1,
            audit_local_evidence_count=1,
            audit_depth_limit_reached=True,
        ),
    )

    adapted = readiness_evidence_from_assessment(assessment)

    assert adapted.audit_depth_limit_reached is True
    assert adapted.reason_codes == assessment.reason_codes
    with pytest.raises(ValueError, match="readiness evidence"):
        decision_input_sha256(
            replace(
                _inputs(),
                manifest_status="depth_limit_reached",
                content_integrity_status="ok",
                readiness_evidence=adapted,
            )
        )


def test_manifest_status_must_match_readiness_evidence() -> None:
    with pytest.raises(ValueError, match="manifest status"):
        decision_input_sha256(
            replace(
                _inputs(),
                manifest_status="download_failed",
            )
        )


@pytest.mark.parametrize("field", ["original_filename", "display_filename"])
def test_filename_inputs_are_normalized_and_nonempty(field: str) -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="filename"):
        decision_input_sha256(
            replace(
                inputs,
                attachments=(
                    replace(inputs.attachments[0], **{field: "   "}),
                    *inputs.attachments[1:],
                ),
            )
        )


@pytest.mark.parametrize("unsafe", ["C:/secret.pdf", "C:secret.pdf", "//server/share/file.pdf"])
def test_windows_drive_and_unc_paths_are_rejected(unsafe: str) -> None:
    inputs = _inputs()

    with pytest.raises(ValueError, match="relative path"):
        decision_input_sha256(
            replace(
                inputs,
                attachments=(
                    replace(inputs.attachments[0], local_relpath=unsafe),
                    *inputs.attachments[1:],
                ),
            )
        )


def test_relative_paths_normalize_unicode_to_nfc() -> None:
    inputs = _inputs()
    nfc = "originals/done/synthetic/Caf\u00e9.pdf"
    nfd = unicodedata.normalize("NFD", nfc)

    assert decision_input_sha256(
        replace(
            inputs,
            attachments=(
                replace(inputs.attachments[0], local_relpath=nfc),
                *inputs.attachments[1:],
            ),
        )
    ) == decision_input_sha256(
        replace(
            inputs,
            attachments=(
                replace(inputs.attachments[0], local_relpath=nfd),
                *inputs.attachments[1:],
            ),
        )
    )


@pytest.mark.parametrize(
    "field",
    ["oa_id_text", "workitem_id_text", "process_id_text", "affair_id_text", "summary_id_text"],
)
def test_all_designed_metadata_ids_normalize_to_text(field: str) -> None:
    inputs = _inputs()
    numeric = replace(
        inputs, normalized_metadata=replace(inputs.normalized_metadata, **{field: 42})
    )
    textual = replace(
        inputs, normalized_metadata=replace(inputs.normalized_metadata, **{field: "42"})
    )

    assert decision_input_sha256(numeric) == decision_input_sha256(textual)
