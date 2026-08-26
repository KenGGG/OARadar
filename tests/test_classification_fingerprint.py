from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


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
        lambda value: replace(value, manifest_status="download_failed"),
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
        lambda value: replace(value, content_integrity_status="missing"),
        lambda value: replace(
            value,
            readiness_evidence=replace(
                value.readiness_evidence,
                audit_status="missing_download",
                audit_finished_at="2026-08-26T09:00:00Z",
                audit_recognized_attachments=2,
                audit_downloaded_attachments=1,
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


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("manifest_processing_status", "download_failed"),
        ("manifest_no_attachment_confirmed", True),
        ("audit_status", "matched"),
        ("audit_finished_at", "2026-08-26T09:00:00Z"),
        ("audit_recognized_attachments", 1),
        ("audit_database_attachments", 1),
        ("audit_downloaded_attachments", 1),
        ("audit_online_inventory_sha256", HASH_A),
        ("audit_local_inventory_sha256", HASH_A),
        ("audit_online_content_sha256", HASH_A),
        ("audit_local_content_sha256", HASH_A),
        ("audit_online_evidence_sha256", HASH_A),
        ("audit_local_evidence_sha256", HASH_A),
        ("audit_depth_limit_reached", True),
        ("reason_codes", ("NOT_CHECKED",)),
    ],
)
def test_each_normalized_readiness_evidence_change_updates_fingerprint(
    field: str, changed: object
) -> None:
    inputs = _inputs()

    updated = replace(
        inputs,
        readiness_evidence=replace(inputs.readiness_evidence, **{field: changed}),
    )

    assert decision_input_sha256(updated) != decision_input_sha256(inputs)


def test_package_integrity_status_is_strictly_validated() -> None:
    with pytest.raises(ValueError, match="content_integrity_status"):
        decision_input_sha256(replace(_inputs(), content_integrity_status="verified"))


def test_foreign_parse_artifact_content_is_rejected() -> None:
    inputs = _inputs()
    foreign = replace(inputs.used_parse_artifacts[0], content_sha256=HASH_C)

    with pytest.raises(ValueError, match="package attachment content"):
        decision_input_sha256(replace(inputs, used_parse_artifacts=(foreign,)))


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
