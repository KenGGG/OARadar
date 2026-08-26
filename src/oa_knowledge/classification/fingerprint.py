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
_TEXT_ID_KEYS = frozenset(
    {
        "oa_item_key",
        "attachment_key",
        "source_container_key",
        "parent_attachment_key",
        "from",
        "to",
        "initiator",
        "relay_from",
        "person_id",
        "identifier",
    }
)


@dataclass(frozen=True)
class AttachmentInput:
    attachment_key: str | int
    file_role: str
    source_container_key: str | int
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
    normalized_metadata: Mapping[str, Any]
    manifest_status: str
    excluded: bool
    exclusion_matches: Sequence[str | int]
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


def _plain_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.strip():
        raise ValueError(f"{field} must not be empty")
    return normalized


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
    if value is None:
        return None
    return _integer(value, field=field)


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
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _relative_path(value: str | Path | None, *, field: str) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must not be absolute or traverse parents")
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
            if normalized_key in _TEXT_ID_KEYS and child is not None:
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


def _attachment_rows(inputs: Sequence[AttachmentInput]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for attachment in inputs:
        if not isinstance(attachment, AttachmentInput):
            raise TypeError("attachments must contain AttachmentInput values")
        status = _plain_text(attachment.integrity_status, field="integrity_status")
        if status not in _INTEGRITY_STATUSES:
            raise ValueError("integrity_status is not a supported content integrity state")
        row = {
            "attachment_key": _text(attachment.attachment_key, field="attachment_key"),
            "file_role": _plain_text(attachment.file_role, field="file_role"),
            "source_container_key": _text(
                attachment.source_container_key, field="source_container_key"
            ),
            "parent_attachment_key": (
                _text(attachment.parent_attachment_key, field="parent_attachment_key")
                if attachment.parent_attachment_key is not None
                else None
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
                attachment.content_sha256, field="attachment.content_sha256", optional=True
            ),
            "integrity_status": status,
            "depth": _integer(
                attachment.depth, field="depth", minimum=1, maximum=10
            ),
        }
        identity = (row["attachment_key"], row["file_role"])
        previous = by_identity.get(identity)
        if previous is not None and previous != row:
            raise ValueError("duplicate attachment identity has conflicting evidence")
        by_identity[identity] = row
    return [by_identity[identity] for identity in sorted(by_identity)]


def _parse_rows(inputs: Sequence[ParseArtifactIdentity]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, str, str, str, str], tuple[dict[str, Any], str | None]] = {}
    for artifact in inputs:
        if not isinstance(artifact, ParseArtifactIdentity):
            raise TypeError("used_parse_artifacts must contain ParseArtifactIdentity values")
        row = {
            "content_sha256": _hash(
                artifact.content_sha256, field="parse_artifact.content_sha256"
            ),
            "parser_name": _plain_text(artifact.parser_name, field="parser_name"),
            "parser_version": _plain_text(artifact.parser_version, field="parser_version"),
            "parse_profile_version": _plain_text(
                artifact.parse_profile_version, field="parse_profile_version"
            ),
            "parse_config_sha256": _hash(
                artifact.parse_config_sha256, field="parse_artifact.parse_config_sha256"
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
            raise ValueError("duplicate transfer relationship identity has conflicting evidence")
        by_identity[identity] = normalized
    return sorted(by_identity.values(), key=_canonical_bytes)


def decision_input_sha256(inputs: DecisionInputs) -> str:
    """Hash exactly one OA package's normalized effective decision inputs."""
    if not isinstance(inputs, DecisionInputs):
        raise TypeError("inputs must be a DecisionInputs value")
    if not isinstance(inputs.normalized_metadata, Mapping):
        raise TypeError("normalized_metadata must be a mapping")
    if not isinstance(inputs.excluded, bool):
        raise TypeError("excluded must be a boolean")
    if inputs.manual_decision_version is not None:
        _integer(
            inputs.manual_decision_version,
            field="manual_decision_version",
            minimum=1,
        )

    payload = {
        "oa_item_key": _text(inputs.oa_item_key, field="oa_item_key"),
        "normalized_metadata": _json_safe(
            inputs.normalized_metadata, field="normalized_metadata"
        ),
        "manifest_status": _plain_text(inputs.manifest_status, field="manifest_status"),
        "excluded": inputs.excluded,
        "exclusion_matches": sorted(
            {_text(value, field="exclusion_match") for value in inputs.exclusion_matches}
        ),
        "attachments": _attachment_rows(inputs.attachments),
        "transfer_evidence": _relationship_rows(inputs.transfer_evidence),
        "used_parse_artifacts": _parse_rows(inputs.used_parse_artifacts),
        "manual_decision_version": inputs.manual_decision_version,
        "rule_version": _plain_text(inputs.rule_version, field="rule_version"),
        "schema_version": _plain_text(inputs.schema_version, field="schema_version"),
        "prompt_version": _plain_text(inputs.prompt_version, field="prompt_version"),
        "private_config_sha256": _hash(
            inputs.private_config_sha256, field="private_config_sha256"
        ),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()
