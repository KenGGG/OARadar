"""Deterministic, package-local inputs for OA classification decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import unicodedata
from typing import Any


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_INTEGRITY_STATUSES = frozenset(
    {
        "ok",
        "no_attachment_confirmed",
        "missing",
        "size_mismatch",
        "sha256_mismatch",
        "download_failed",
        "not_checked",
    }
)
_TRANSFER_TEXT_ID_KEYS = frozenset(
    {"from", "to", "person_id", "identifier", "relay_from"}
)


@dataclass(frozen=True)
class NormalizedOAMetadata:
    normalized_title: str
    initiator: str | int | None = None
    document_number: str | None = None
    completed_at: datetime | str | None = None
    oa_id_text: str | int | None = None
    workitem_id_text: str | int | None = None
    process_id_text: str | int | None = None
    affair_id_text: str | int | None = None
    summary_id_text: str | int | None = None
    initiated_at: datetime | str | None = None
    received_at: datetime | str | None = None
    sender: str | None = None
    department: str | None = None
    flow_type: str | None = None


@dataclass(frozen=True)
class ReadinessEvidenceInput:
    manifest_processing_status: str | None
    manifest_no_attachment_confirmed: bool
    audit_evidence_mode: str = "none"
    audit_comparison_reason: str | None = None
    audit_status: str | None = None
    audit_finished_at: datetime | str | None = None
    audit_recognized_attachments: int | None = None
    audit_database_attachments: int | None = None
    audit_downloaded_attachments: int | None = None
    audit_online_inventory_sha256: str | None = None
    audit_local_inventory_sha256: str | None = None
    audit_online_content_sha256: str | None = None
    audit_local_content_sha256: str | None = None
    audit_online_evidence_sha256: str | None = None
    audit_local_evidence_sha256: str | None = None
    audit_online_evidence_count: int | None = None
    audit_local_evidence_count: int | None = None
    audit_depth_limit_reached: bool = False
    reason_codes: Sequence[str] = ()


@dataclass(frozen=True)
class AttachmentInput:
    attachment_key: str | int
    file_role: str
    original_filename: str
    display_filename: str
    source_container_key: str | int
    parent_container_key: str | int | None
    container_path: Sequence[str | int]
    local_relpath: str | Path | None
    size_bytes: int | None
    expected_size: int | None
    sha256: str | None
    content_sha256: str | None
    integrity_status: str
    depth: int
    parent_attachment_key: str | int | None = None


@dataclass(frozen=True)
class ParseArtifactIdentity:
    content_sha256: str
    parser_name: str
    parser_version: str
    parse_profile_version: str
    parse_config_sha256: str
    source_relpath: str | Path | None = None


@dataclass(frozen=True)
class DecisionInputs:
    oa_item_key: str | int
    normalized_metadata: NormalizedOAMetadata
    manifest_status: str
    excluded: bool
    exclusion_matches: Sequence[str | int]
    content_integrity_status: str
    readiness_evidence: ReadinessEvidenceInput
    attachments: Sequence[AttachmentInput]
    transfer_evidence: Sequence[Mapping[str, Any]]
    used_parse_artifacts: Sequence[ParseArtifactIdentity]
    manual_decision_version: int | None
    rule_version: str
    schema_version: str
    prompt_version: str
    private_config_sha256: str


def _text(value: object, *, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError(f"{field} must be a text-compatible identifier")
    normalized = unicodedata.normalize("NFC", str(value)).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _optional_text_id(value: object, *, field: str) -> str | None:
    return None if value is None else _text(value, field=field)


def _plain_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _optional_plain_text(value: object, *, field: str) -> str | None:
    return None if value is None else _plain_text(value, field=field)


def _hash(value: object, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _integer(
    value: object, *, field: str, minimum: int = 0, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def _optional_integer(value: object, *, field: str) -> int | None:
    return None if value is None else _integer(value, field=field)


def _timestamp(value: datetime | str, *, field: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _optional_timestamp(value: datetime | str | None, *, field: str) -> str | None:
    return None if value is None else _timestamp(value, field=field)


def _relative_path(value: str | Path | None, *, field: str) -> str | None:
    if value is None:
        return None
    raw = unicodedata.normalize("NFC", str(value))
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or raw.startswith("//")
        or _WINDOWS_DRIVE_RE.match(raw)
    ):
        raise ValueError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a relative path without parent traversal")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError(f"{field} must identify a file")
    return normalized


def _json_safe(value: object, *, field: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite float")
        return 0.0 if value == 0 else value
    if isinstance(value, datetime):
        return _timestamp(value, field=field)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field} contains a non-text mapping key")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(f"{field} contains duplicate normalized mapping keys")
            if normalized_key in _TRANSFER_TEXT_ID_KEYS and child is not None:
                normalized[normalized_key] = _text(
                    child, field=f"{field}.{normalized_key}"
                )
            elif normalized_key.endswith("_at") and isinstance(child, (datetime, str)):
                normalized[normalized_key] = _timestamp(
                    child, field=f"{field}.{normalized_key}"
                )
            else:
                normalized[normalized_key] = _json_safe(
                    child, field=f"{field}.{normalized_key}"
                )
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(child, field=f"{field}[{index}]")
            for index, child in enumerate(value)
        ]
    raise TypeError(f"{field} contains a value that is not JSON-safe")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _metadata_row(metadata: NormalizedOAMetadata) -> dict[str, Any]:
    if not isinstance(metadata, NormalizedOAMetadata):
        raise TypeError("normalized_metadata must be a NormalizedOAMetadata value")
    return {
        "normalized_title": _plain_text(
            metadata.normalized_title, field="normalized_metadata.normalized_title"
        ),
        "initiator": _optional_text_id(
            metadata.initiator, field="normalized_metadata.initiator"
        ),
        "document_number": _optional_plain_text(
            metadata.document_number, field="normalized_metadata.document_number"
        ),
        "completed_at": _optional_timestamp(
            metadata.completed_at, field="normalized_metadata.completed_at"
        ),
        "initiated_at": _optional_timestamp(
            metadata.initiated_at, field="normalized_metadata.initiated_at"
        ),
        "received_at": _optional_timestamp(
            metadata.received_at, field="normalized_metadata.received_at"
        ),
        "oa_id_text": _optional_text_id(
            metadata.oa_id_text, field="normalized_metadata.oa_id_text"
        ),
        "workitem_id_text": _optional_text_id(
            metadata.workitem_id_text, field="normalized_metadata.workitem_id_text"
        ),
        "process_id_text": _optional_text_id(
            metadata.process_id_text, field="normalized_metadata.process_id_text"
        ),
        "affair_id_text": _optional_text_id(
            metadata.affair_id_text, field="normalized_metadata.affair_id_text"
        ),
        "summary_id_text": _optional_text_id(
            metadata.summary_id_text, field="normalized_metadata.summary_id_text"
        ),
        "sender": _optional_plain_text(metadata.sender, field="normalized_metadata.sender"),
        "department": _optional_plain_text(
            metadata.department, field="normalized_metadata.department"
        ),
        "flow_type": _optional_plain_text(
            metadata.flow_type, field="normalized_metadata.flow_type"
        ),
    }


def _readiness_row(evidence: ReadinessEvidenceInput) -> dict[str, Any]:
    if not isinstance(evidence, ReadinessEvidenceInput):
        raise TypeError("readiness_evidence must be a ReadinessEvidenceInput value")
    if not isinstance(evidence.manifest_no_attachment_confirmed, bool):
        raise TypeError("manifest_no_attachment_confirmed must be a boolean")
    if not isinstance(evidence.audit_depth_limit_reached, bool):
        raise TypeError("audit_depth_limit_reached must be a boolean")
    _optional_timestamp(
        evidence.audit_finished_at, field="readiness_evidence.audit_finished_at"
    )
    evidence_mode = _plain_text(
        evidence.audit_evidence_mode, field="readiness_evidence.audit_evidence_mode"
    )
    if evidence_mode not in {"none", "full", "count_only"}:
        raise ValueError("readiness evidence mode is not supported")
    if evidence_mode == "none" and (
        evidence.audit_finished_at is not None or evidence.audit_depth_limit_reached
    ):
        raise ValueError("readiness evidence mode none contains audit facts")
    return {
        "manifest_processing_status": _optional_plain_text(
            evidence.manifest_processing_status,
            field="readiness_evidence.manifest_processing_status",
        ),
        "manifest_no_attachment_confirmed": evidence.manifest_no_attachment_confirmed,
        "audit_evidence_mode": evidence_mode,
        "audit_comparison_reason": _optional_plain_text(
            evidence.audit_comparison_reason,
            field="readiness_evidence.audit_comparison_reason",
        ),
        "audit_status": _optional_plain_text(
            evidence.audit_status, field="readiness_evidence.audit_status"
        ),
        "audit_recognized_attachments": _optional_integer(
            evidence.audit_recognized_attachments,
            field="readiness_evidence.audit_recognized_attachments",
        ),
        "audit_database_attachments": _optional_integer(
            evidence.audit_database_attachments,
            field="readiness_evidence.audit_database_attachments",
        ),
        "audit_downloaded_attachments": _optional_integer(
            evidence.audit_downloaded_attachments,
            field="readiness_evidence.audit_downloaded_attachments",
        ),
        "audit_online_inventory_sha256": _hash(
            evidence.audit_online_inventory_sha256,
            field="readiness_evidence.audit_online_inventory_sha256",
            optional=True,
        ),
        "audit_local_inventory_sha256": _hash(
            evidence.audit_local_inventory_sha256,
            field="readiness_evidence.audit_local_inventory_sha256",
            optional=True,
        ),
        "audit_online_content_sha256": _hash(
            evidence.audit_online_content_sha256,
            field="readiness_evidence.audit_online_content_sha256",
            optional=True,
        ),
        "audit_local_content_sha256": _hash(
            evidence.audit_local_content_sha256,
            field="readiness_evidence.audit_local_content_sha256",
            optional=True,
        ),
        "audit_online_evidence_sha256": _hash(
            evidence.audit_online_evidence_sha256,
            field="readiness_evidence.audit_online_evidence_sha256",
            optional=True,
        ),
        "audit_local_evidence_sha256": _hash(
            evidence.audit_local_evidence_sha256,
            field="readiness_evidence.audit_local_evidence_sha256",
            optional=True,
        ),
        "audit_online_evidence_count": _optional_integer(
            evidence.audit_online_evidence_count,
            field="readiness_evidence.audit_online_evidence_count",
        ),
        "audit_local_evidence_count": _optional_integer(
            evidence.audit_local_evidence_count,
            field="readiness_evidence.audit_local_evidence_count",
        ),
        "audit_depth_limit_reached": evidence.audit_depth_limit_reached,
        "reason_codes": sorted(
            {_plain_text(reason, field="readiness_evidence.reason_code") for reason in evidence.reason_codes}
        ),
    }


def _attachment_rows(inputs: Sequence[AttachmentInput]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for attachment in inputs:
        if not isinstance(attachment, AttachmentInput):
            raise TypeError("attachments must contain AttachmentInput values")
        status = _plain_text(attachment.integrity_status, field="integrity_status")
        if status not in _INTEGRITY_STATUSES:
            raise ValueError("integrity_status is not a supported content integrity state")
        container_path = tuple(
            _text(value, field="attachment.container_path")
            for value in attachment.container_path
        )
        if not container_path:
            raise ValueError("container topology must contain its source container")
        row = {
            "attachment_key": _text(attachment.attachment_key, field="attachment_key"),
            "file_role": _plain_text(attachment.file_role, field="file_role"),
            "original_filename": _plain_text(
                attachment.original_filename, field="original filename"
            ),
            "display_filename": _plain_text(
                attachment.display_filename, field="display filename"
            ),
            "source_container_key": _text(
                attachment.source_container_key, field="source_container_key"
            ),
            "parent_container_key": _optional_text_id(
                attachment.parent_container_key, field="parent_container_key"
            ),
            "container_path": container_path,
            "parent_attachment_key": _optional_text_id(
                attachment.parent_attachment_key, field="parent_attachment_key"
            ),
            "local_relpath": _relative_path(
                attachment.local_relpath, field="attachment.local_relpath"
            ),
            "size_bytes": _optional_integer(attachment.size_bytes, field="size_bytes"),
            "expected_size": _optional_integer(
                attachment.expected_size, field="expected_size"
            ),
            "sha256": _hash(attachment.sha256, field="attachment.sha256", optional=True),
            "content_sha256": _hash(
                attachment.content_sha256,
                field="attachment.content_sha256",
                optional=True,
            ),
            "integrity_status": status,
            "depth": _integer(attachment.depth, field="depth", minimum=1, maximum=10),
        }
        identity = (row["attachment_key"], row["file_role"])
        previous = by_identity.get(identity)
        if previous is not None and previous != row:
            raise ValueError("duplicate attachment identity has conflicting evidence")
        by_identity[identity] = row

    rows = [by_identity[identity] for identity in sorted(by_identity)]
    keys = [row["attachment_key"] for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("attachment parent topology has ambiguous attachment keys")
    key_set = set(keys)
    container_parents: dict[str, str | None] = {}
    for row in rows:
        path = row["container_path"]
        for index, container_key in enumerate(path):
            parent = path[index - 1] if index else None
            if container_key in container_parents and container_parents[container_key] != parent:
                raise ValueError("container topology declares conflicting parent paths")
            container_parents[container_key] = parent
    parent_by_key: dict[str, str | None] = {}
    for row in rows:
        path = row["container_path"]
        if (
            path[-1] != row["source_container_key"]
            or len(path) != row["depth"]
            or len(path) != len(set(path))
        ):
            raise ValueError("container topology is inconsistent")
        expected_parent = path[-2] if len(path) > 1 else None
        if (
            row["parent_container_key"] != expected_parent
            or container_parents[row["source_container_key"]] != expected_parent
        ):
            raise ValueError("container topology has an inconsistent parent")

        key = row["attachment_key"]
        parent = row["parent_attachment_key"]
        if parent is not None and (parent not in key_set or parent == key):
            raise ValueError("attachment parent topology is invalid")
        parent_by_key[key] = parent

    for key in parent_by_key:
        seen: set[str] = set()
        current: str | None = key
        while current is not None:
            if current in seen:
                raise ValueError("attachment parent topology contains a cycle")
            seen.add(current)
            current = parent_by_key[current]
    return rows


def _parse_rows(
    inputs: Sequence[ParseArtifactIdentity], *, package_content_hashes: set[str]
) -> list[dict[str, Any]]:
    by_identity: dict[
        tuple[str, str, str, str, str], tuple[dict[str, Any], str | None]
    ] = {}
    for artifact in inputs:
        if not isinstance(artifact, ParseArtifactIdentity):
            raise TypeError("used_parse_artifacts must contain ParseArtifactIdentity values")
        content_hash = _hash(
            artifact.content_sha256, field="parse_artifact.content_sha256"
        )
        if content_hash not in package_content_hashes:
            raise ValueError("parse artifact must reference package attachment content")
        row = {
            "content_sha256": content_hash,
            "parser_name": _plain_text(artifact.parser_name, field="parser_name"),
            "parser_version": _plain_text(
                artifact.parser_version, field="parser_version"
            ),
            "parse_profile_version": _plain_text(
                artifact.parse_profile_version, field="parse_profile_version"
            ),
            "parse_config_sha256": _hash(
                artifact.parse_config_sha256,
                field="parse_artifact.parse_config_sha256",
            ),
        }
        source_path = _relative_path(
            artifact.source_relpath, field="parse_artifact.source_relpath"
        )
        identity = tuple(row.values())
        previous = by_identity.get(identity)
        if previous is not None and previous[1] != source_path:
            raise ValueError("duplicate parse artifact identity has conflicting evidence")
        by_identity[identity] = (row, source_path)
    return [by_identity[identity][0] for identity in sorted(by_identity)]


def _relationship_rows(inputs: Sequence[Mapping[str, Any]]) -> list[Any]:
    by_identity: dict[tuple[str, object], Any] = {}
    for row in inputs:
        if not isinstance(row, Mapping):
            raise TypeError("transfer_evidence must contain mappings")
        normalized = _json_safe(row, field="transfer_evidence")
        if "ordinal" in normalized:
            ordinal = _integer(
                normalized["ordinal"], field="transfer_evidence.ordinal", minimum=1
            )
            identity: tuple[str, object] = ("ordinal", ordinal)
        else:
            identity = ("value", _canonical_bytes(normalized))
        previous = by_identity.get(identity)
        if previous is not None and previous != normalized:
            raise ValueError(
                "duplicate transfer relationship identity has conflicting evidence"
            )
        by_identity[identity] = normalized
    return sorted(by_identity.values(), key=_canonical_bytes)


_MISSING_REASONS = frozenset(
    {
        "MANIFEST_MISSING",
        "ARCHIVED_ITEM_MISSING",
        "DEPTH_LIMIT_REACHED",
        "ATTACHMENT_INVENTORY_CONFLICT",
        "ZERO_ATTACHMENT_AUDIT_CONFLICT",
        "ARCHIVE_FILE_MISSING",
        "AUDIT_INVENTORY_MISMATCH",
    }
)


def _validate_audit_bundle(readiness_row: Mapping[str, Any]) -> None:
    mode = readiness_row["audit_evidence_mode"]
    audit_status = readiness_row["audit_status"]
    comparison_reason = readiness_row["audit_comparison_reason"]
    raw_fields = (
        audit_status,
        comparison_reason,
        readiness_row["audit_recognized_attachments"],
        readiness_row["audit_database_attachments"],
        readiness_row["audit_downloaded_attachments"],
        readiness_row["audit_online_inventory_sha256"],
        readiness_row["audit_local_inventory_sha256"],
        readiness_row["audit_online_content_sha256"],
        readiness_row["audit_local_content_sha256"],
        readiness_row["audit_online_evidence_sha256"],
        readiness_row["audit_local_evidence_sha256"],
        readiness_row["audit_online_evidence_count"],
        readiness_row["audit_local_evidence_count"],
    )
    if mode == "none":
        if any(value is not None for value in raw_fields):
            raise ValueError("readiness evidence mode none contains audit facts")
        return
    if audit_status is None:
        raise ValueError("readiness evidence audit mode requires an audit status")

    hash_pairs = (
        (
            readiness_row["audit_online_inventory_sha256"],
            readiness_row["audit_local_inventory_sha256"],
        ),
        (
            readiness_row["audit_online_content_sha256"],
            readiness_row["audit_local_content_sha256"],
        ),
        (
            readiness_row["audit_online_evidence_sha256"],
            readiness_row["audit_local_evidence_sha256"],
        ),
    )
    if mode == "full":
        if comparison_reason == "evidence_unavailable":
            raise ValueError("readiness evidence full mode has count-only reason")
        if any((left is None) != (right is None) for left, right in hash_pairs):
            raise ValueError("readiness evidence full mode has one-sided hashes")
        if audit_status == "matched":
            counts = (
                readiness_row["audit_recognized_attachments"],
                readiness_row["audit_downloaded_attachments"],
                readiness_row["audit_online_evidence_count"],
                readiness_row["audit_local_evidence_count"],
            )
            if any(value is None for value in counts) or len(set(counts)) != 1:
                raise ValueError("readiness evidence matched counts contradict")
            if any(left is None or left != right for left, right in hash_pairs):
                raise ValueError("readiness evidence matched hashes contradict")
    else:
        if comparison_reason != "evidence_unavailable":
            raise ValueError("readiness evidence count-only mode has wrong reason")
        if (
            readiness_row["audit_online_inventory_sha256"] is not None
            or readiness_row["audit_online_content_sha256"] is not None
        ):
            raise ValueError("readiness evidence count-only mode has online hashes")
        if audit_status == "matched":
            recognized = readiness_row["audit_recognized_attachments"]
            downloaded = readiness_row["audit_downloaded_attachments"]
            if (
                recognized is None
                or recognized != downloaded
                or readiness_row["audit_online_evidence_count"] != 0
                or readiness_row["audit_local_evidence_count"] != downloaded
                or readiness_row["audit_online_evidence_sha256"] is None
                or readiness_row["audit_local_evidence_sha256"] is None
            ):
                raise ValueError("readiness evidence count-only facts contradict")


def _validate_readiness_consistency(
    content_integrity_status: str, readiness_row: Mapping[str, Any]
) -> None:
    _validate_audit_bundle(readiness_row)
    reasons = set(readiness_row["reason_codes"])
    if readiness_row["audit_depth_limit_reached"]:
        reasons.add("DEPTH_LIMIT_REACHED")
    manifest_status = readiness_row["manifest_processing_status"]
    if manifest_status == "depth_limit_reached":
        reasons.add("DEPTH_LIMIT_REACHED")
    elif manifest_status in {
        "failed",
        "error",
        "rejected_zero_byte",
        "rejected_error_page",
        "rejected_type_mismatch",
        "download_failed",
    }:
        reasons.add("DOWNLOAD_FAILED")

    if reasons.intersection(_MISSING_REASONS):
        expected = "missing"
    elif "DOWNLOAD_FAILED" in reasons:
        expected = "download_failed"
    elif "SIZE_MISMATCH" in reasons:
        expected = "size_mismatch"
    elif "SHA256_MISMATCH" in reasons:
        expected = "sha256_mismatch"
    elif reasons.intersection({"NOT_CHECKED", "AUDIT_EVIDENCE_INVALID"}):
        expected = "not_checked"
    elif "NO_ATTACHMENT_CONFIRMED" in reasons:
        expected = "no_attachment_confirmed"
    elif "VERIFIED_STORED_EVIDENCE" in reasons:
        expected = "ok"
    else:
        raise ValueError("readiness evidence has no deterministic terminal state")

    if content_integrity_status != expected:
        raise ValueError("content_integrity_status contradicts readiness evidence")
    if expected == "no_attachment_confirmed" and not (
        manifest_status == "no_attachment"
        and readiness_row["manifest_no_attachment_confirmed"] is True
    ):
        raise ValueError("readiness evidence contradicts no-attachment status")
    if expected == "ok" and readiness_row["manifest_no_attachment_confirmed"]:
        raise ValueError("readiness evidence contradicts verified attachment status")
    if expected in {"ok", "no_attachment_confirmed"} and readiness_row[
        "audit_status"
    ] not in {None, "matched"}:
        raise ValueError("readiness evidence terminal status contradicts audit status")


def readiness_evidence_from_assessment(assessment: object) -> ReadinessEvidenceInput:
    """Losslessly adapt the readiness service result for decision fingerprinting."""
    from oa_knowledge.classification.readiness import IntegrityAssessment

    if not isinstance(assessment, IntegrityAssessment):
        raise TypeError("assessment must be an IntegrityAssessment value")
    evidence = assessment.evidence
    return ReadinessEvidenceInput(
        manifest_processing_status=evidence.manifest_processing_status,
        manifest_no_attachment_confirmed=evidence.manifest_no_attachment_confirmed,
        audit_evidence_mode=evidence.audit_evidence_mode,
        audit_comparison_reason=evidence.audit_comparison_reason,
        audit_status=evidence.audit_status,
        audit_finished_at=evidence.audit_finished_at,
        audit_recognized_attachments=evidence.audit_recognized_attachments,
        audit_database_attachments=evidence.audit_database_attachments,
        audit_downloaded_attachments=evidence.audit_downloaded_attachments,
        audit_online_inventory_sha256=evidence.audit_online_inventory_sha256,
        audit_local_inventory_sha256=evidence.audit_local_inventory_sha256,
        audit_online_content_sha256=evidence.audit_online_content_sha256,
        audit_local_content_sha256=evidence.audit_local_content_sha256,
        audit_online_evidence_sha256=evidence.audit_online_evidence_sha256,
        audit_local_evidence_sha256=evidence.audit_local_evidence_sha256,
        audit_online_evidence_count=evidence.audit_online_evidence_count,
        audit_local_evidence_count=evidence.audit_local_evidence_count,
        audit_depth_limit_reached=evidence.audit_depth_limit_reached,
        reason_codes=assessment.reason_codes,
    )


def decision_input_sha256(inputs: DecisionInputs) -> str:
    """Hash exactly one OA package's normalized effective decision inputs."""
    if not isinstance(inputs, DecisionInputs):
        raise TypeError("inputs must be a DecisionInputs value")
    if not isinstance(inputs.excluded, bool):
        raise TypeError("excluded must be a boolean")
    integrity_status = _plain_text(
        inputs.content_integrity_status, field="content_integrity_status"
    )
    if integrity_status not in _INTEGRITY_STATUSES:
        raise ValueError("content_integrity_status is not supported")
    readiness_row = _readiness_row(inputs.readiness_evidence)
    manifest_status = _plain_text(inputs.manifest_status, field="manifest_status")
    if manifest_status != readiness_row["manifest_processing_status"]:
        raise ValueError("manifest status must match readiness evidence")
    _validate_readiness_consistency(integrity_status, readiness_row)
    if inputs.manual_decision_version is not None:
        _integer(
            inputs.manual_decision_version,
            field="manual_decision_version",
            minimum=1,
        )

    attachment_rows = _attachment_rows(inputs.attachments)
    package_content_hashes = {
        row["content_sha256"]
        for row in attachment_rows
        if row["content_sha256"] is not None
    }
    payload = {
        "oa_item_key": _text(inputs.oa_item_key, field="oa_item_key"),
        "normalized_metadata": _metadata_row(inputs.normalized_metadata),
        "manifest_status": manifest_status,
        "excluded": inputs.excluded,
        "exclusion_matches": sorted(
            {_text(value, field="exclusion_match") for value in inputs.exclusion_matches}
        ),
        "content_integrity_status": integrity_status,
        "readiness_evidence": readiness_row,
        "attachments": attachment_rows,
        "transfer_evidence": _relationship_rows(inputs.transfer_evidence),
        "used_parse_artifacts": _parse_rows(
            inputs.used_parse_artifacts,
            package_content_hashes=package_content_hashes,
        ),
        "manual_decision_version": inputs.manual_decision_version,
        "rule_version": _plain_text(inputs.rule_version, field="rule_version"),
        "schema_version": _plain_text(inputs.schema_version, field="schema_version"),
        "prompt_version": _plain_text(inputs.prompt_version, field="prompt_version"),
        "private_config_sha256": _hash(
            inputs.private_config_sha256, field="private_config_sha256"
        ),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()
