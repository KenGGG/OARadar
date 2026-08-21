"""Read-only, redacted acceptance gates for a single rebuild run."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.body_source import body_markdown_filename
from oa_knowledge.rebuild.markdown import (
    _attachment_markdown_filename,
    _publication_sha,
)
from oa_knowledge.rebuild.parser import _tree_sha256
from oa_knowledge.rebuild.paths import (
    archive_file_relpath,
    markdown_item_relpath,
    resolve_rebuild_path,
    resolve_rebuild_root,
)
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


@dataclass(frozen=True)
class ValidationCheck:
    """One redacted acceptance result; values are aggregate counts only."""

    code: str
    ok: bool
    expected: int | None
    actual: int | None


_LINK = re.compile(r"!?\[[^]]*\]\((?P<target>[^)]+)\)")
_ROOT_DIRS = frozenset({"archive", "parse", "markdown", "state", "runtime"})
_DERIVED_KINDS = frozenset({"body_markdown", "attachment_markdown", "item_index"})
_EVIDENCE_SCHEMA_VERSION = 2
_EVIDENCE_PRODUCER = "oaradar.rebuild.acceptance.v2"
_EVIDENCE_SIGNATURE_ALGORITHM = "hmac-sha256"
_EVIDENCE_CHECK_KEYS = frozenset({
    "automated_tests_passed",
    "build_passed",
    "external_sample_count",
    "frontend_check_passed",
    "internal_sample_count",
    "synthetic_smoke_passed",
    "webui_filter_contract",
})
_EVIDENCE_ENVELOPE_KEYS = frozenset({
    "checks",
    "fingerprint",
    "generated_at",
    "producer",
    "run_id",
    "schema_version",
    "signature",
    "signature_algorithm",
})
_MAX_EVIDENCE_AGE = timedelta(hours=24)


def _check(code: str, expected: int | None, actual: int | None) -> ValidationCheck:
    return ValidationCheck(code, expected == actual, expected, actual)


def _sha256_file(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _regular_files(root: Path) -> tuple[list[Path], bool]:
    """List only regular non-symlink files, while detecting unsafe entries."""
    if not root.exists():
        return [], True
    if not root.is_dir() or root.is_symlink():
        return [], False
    result: list[Path] = []
    valid = True

    def walk(directory: Path) -> None:
        nonlocal valid
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            valid = False
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    valid = False
                elif entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    result.append(Path(entry.path))
                else:
                    valid = False
            except OSError:
                valid = False

    walk(root)
    return result, valid


def _safe_target(settings: Settings, relpath: str) -> Path | None:
    try:
        return resolve_rebuild_path(settings, relpath)
    except ValueError:
        return None


def _hash_matches(settings: Settings, output: RebuildOutput, source: ArchivedFile) -> bool:
    target = _safe_target(settings, output.target_relpath)
    return bool(
        target
        and output.status == "success"
        and source.download_status == "verified"
        and output.sha256 == source.sha256
        and source.size_bytes is not None
        and source.sha256
        and target.is_file()
        and not target.is_symlink()
        and target.stat().st_size == source.size_bytes
        and _sha256_file(target) == source.sha256
    )


def _valid_original(
    settings: Settings, output: RebuildOutput, source: ArchivedFile, item: OAItem | None,
) -> bool:
    if item is None or output.oa_item_id != source.oa_item_id:
        return False
    try:
        deterministic_path = archive_file_relpath(item, source).as_posix()
    except ValueError:
        return False
    return output.target_relpath == deterministic_path and _hash_matches(settings, output, source)


def _valid_parse(settings: Settings, output: RebuildOutput, source: ArchivedFile) -> bool:
    target = _safe_target(settings, output.target_relpath)
    if not target or output.status != "success" or not output.sha256:
        return False
    try:
        manifest = json.loads((target / ".oaradar-parse.json").read_text(encoding="utf-8"))
        return (
            isinstance(manifest, dict)
            and bool(manifest.get("engine"))
            and bool(manifest.get("engine_version"))
            and manifest.get("source_file_id") == source.id
            and manifest.get("source_sha256") == source.sha256
            and _tree_sha256(target) == output.sha256
        )
    except (OSError, TypeError, ValueError):
        return False


def _valid_markdown(settings: Settings, output: RebuildOutput) -> bool:
    target = _safe_target(settings, output.target_relpath)
    if not target or output.status != "success" or not output.sha256:
        return False
    try:
        if output.kind == "item_index":
            return _sha256_file(target) == output.sha256
        assets = target.parent / "assets" / str(output.source_file_id)
        return _publication_sha(target, assets if assets.is_dir() and not assets.is_symlink() else None) == output.sha256
    except (OSError, ValueError):
        return False


def _frontmatter_matches(path: Path, item: OAItem) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.startswith("---\n"):
        return False
    closing = text.find("\n---\n", 4)
    if closing < 4:
        return False
    try:
        payload = yaml.safe_load(text[4:closing])
    except yaml.YAMLError:
        return False
    if not isinstance(payload, dict):
        return False
    effective_date = item.document_date or item.initiated_at or item.completed_at
    identifier = item.workitem_id_text or item.oa_item_key
    if hasattr(effective_date, "date"):
        effective_date = effective_date.date()
    if (
        payload.get("title") != item.title
        or payload.get("oa_item_id") != identifier
        or payload.get("document_number") != (item.document_number or None)
        or str(payload.get("effective_date")) != str(effective_date)
        or payload.get("source_type") != item.source_type
    ):
        return False
    return (
        item.source_type == "internal"
        and payload.get("internal_category") == item.internal_category
        and not payload.get("external_issuer")
    ) or (
        item.source_type == "external"
        and payload.get("external_issuer") == item.external_issuer
        and not payload.get("internal_category")
    )


def _link_target(path: Path, raw: str, root: Path) -> bool:
    target = raw.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing < 1:
            return False
        target = target[1:closing]
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or target.startswith(("/", "#")):
        return False
    target = unquote(target.split("#", 1)[0].split("?", 1)[0])
    if not target:
        return True
    candidate = (path.parent / target).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    return (
        bool(relative.parts)
        and relative.parts[0] in {"markdown", "archive"}
        and candidate.is_file()
        and not candidate.is_symlink()
    )


def _link_counts(paths: Iterable[Path], root: Path) -> tuple[int, int]:
    expected = actual = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _LINK.finditer(text):
            expected += 1
            actual += int(_link_target(path, match.group("target"), root))
    return expected, actual


def _expected_markdown_path(item: OAItem, output: RebuildOutput, source: ArchivedFile | None) -> str | None:
    try:
        item_path = markdown_item_relpath(item)
        if output.kind == "item_index":
            return (item_path / "_index.md").as_posix()
        if source is None:
            return None
        if output.kind == "body_markdown":
            filename = body_markdown_filename(item)
        elif output.kind == "attachment_markdown":
            filename = _attachment_markdown_filename(source)
        else:
            return None
        return (item_path / filename).as_posix() if filename else None
    except ValueError:
        return None


def _acceptance_evidence(settings: Settings) -> dict[str, object]:
    """Read aggregate operational evidence from a fixed local-only state file."""
    path = _safe_target(settings, "state/acceptance-evidence.json")
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_sha256(settings: Settings, output: RebuildOutput) -> str | None:
    """Return a local artifact digest without exposing its location or content."""
    target = _safe_target(settings, output.target_relpath)
    if target is None:
        return None
    try:
        if output.kind == "parse":
            return _tree_sha256(target)
        if output.kind in {"body_markdown", "attachment_markdown"}:
            assets = target.parent / "assets" / str(output.source_file_id)
            return _publication_sha(
                target, assets if assets.is_dir() and not assets.is_symlink() else None,
            )
        return _sha256_file(target)
    except (OSError, ValueError):
        return None


def _run_artifact_fingerprint(
    settings: Settings, run_id: int, outputs: Sequence[RebuildOutput],
) -> str:
    """Fingerprint every current-run output and its redacted ledger state."""
    records = [
        {
            "artifact_sha256": _artifact_sha256(settings, output),
            "error_code": output.error_code,
            "kind": output.kind,
            "ledger_sha256": output.sha256,
            "oa_item_id": output.oa_item_id,
            "output_id": output.id,
            "source_file_id": output.source_file_id,
            "status": output.status,
            "target_relpath": output.target_relpath,
        }
        for output in outputs
    ]
    payload = {
        "run_id": run_id,
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "outputs": sorted(
            records,
            key=lambda record: (
                int(record["output_id"]), str(record["kind"]), int(record["oa_item_id"]),
                -1 if record["source_file_id"] is None else int(record["source_file_id"]),
                str(record["target_relpath"]),
            ),
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _evidence_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_utc(parsed) if parsed.tzinfo is not None else None


def _evidence_signing_key(settings: Settings) -> bytes | None:
    value = os.environ.get(settings.rebuild.acceptance_evidence_key_env)
    if value is None:
        return None
    key = value.encode("utf-8")
    return key if len(key) >= 32 else None


def _canonical_unsigned_evidence(evidence: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in evidence.items() if key != "signature"}
    return json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _evidence_checks(evidence: dict[str, object]) -> dict[str, object]:
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or set(checks) != _EVIDENCE_CHECK_KEYS:
        return {}
    boolean_keys = _EVIDENCE_CHECK_KEYS - {
        "external_sample_count", "internal_sample_count",
    }
    if any(not isinstance(checks.get(key), bool) for key in boolean_keys):
        return {}
    for key in ("external_sample_count", "internal_sample_count"):
        value = checks.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return {}
    return checks


def _evidence_signature_valid(
    evidence: dict[str, object], settings: Settings,
) -> bool:
    key = _evidence_signing_key(settings)
    signature = evidence.get("signature")
    if (
        key is None
        or set(evidence) != _EVIDENCE_ENVELOPE_KEYS
        or evidence.get("signature_algorithm") != _EVIDENCE_SIGNATURE_ALGORITHM
        or not isinstance(signature, str)
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
    ):
        return False
    expected = hmac.new(
        key, _canonical_unsigned_evidence(evidence), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def write_acceptance_evidence(
    session: Session,
    settings: Settings,
    run_id: int,
    *,
    webui_filter_contract: bool,
    internal_sample_count: int,
    external_sample_count: int,
    automated_tests_passed: bool,
    frontend_check_passed: bool,
    build_passed: bool,
    synthetic_smoke_passed: bool,
    generated_at: datetime | None = None,
) -> Path:
    """Atomically write a configuration-bound, aggregate-only evidence envelope."""
    key = _evidence_signing_key(settings)
    if key is None:
        raise RuntimeError("ACCEPTANCE_EVIDENCE_SIGNING_KEY_UNAVAILABLE")
    if generated_at is None:
        generated = datetime.now(UTC)
    elif generated_at.tzinfo is None:
        raise ValueError("ACCEPTANCE_EVIDENCE_TIMESTAMP_MUST_BE_AWARE")
    else:
        generated = generated_at.astimezone(UTC)
    checks: dict[str, object] = {
        "automated_tests_passed": automated_tests_passed,
        "build_passed": build_passed,
        "external_sample_count": external_sample_count,
        "frontend_check_passed": frontend_check_passed,
        "internal_sample_count": internal_sample_count,
        "synthetic_smoke_passed": synthetic_smoke_passed,
        "webui_filter_contract": webui_filter_contract,
    }
    if not _evidence_checks({"checks": checks}):
        raise ValueError("ACCEPTANCE_EVIDENCE_CHECKS_INVALID")
    with session.no_autoflush:
        run = session.get(PipelineRun, run_id)
        outputs = list(session.scalars(
            select(RebuildOutput).where(RebuildOutput.run_id == run_id)
        ))
    if run is None or run.pipeline_type != "data_rebuild":
        raise ValueError("ACCEPTANCE_EVIDENCE_RUN_INVALID")
    evidence: dict[str, object] = {
        "checks": checks,
        "fingerprint": _run_artifact_fingerprint(settings, run_id, outputs),
        "generated_at": generated.isoformat(),
        "producer": _EVIDENCE_PRODUCER,
        "run_id": run_id,
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "signature_algorithm": _EVIDENCE_SIGNATURE_ALGORITHM,
    }
    evidence["signature"] = hmac.new(
        key, _canonical_unsigned_evidence(evidence), hashlib.sha256,
    ).hexdigest()
    destination = _safe_target(settings, "state/acceptance-evidence.json")
    if destination is None:
        raise ValueError("ACCEPTANCE_EVIDENCE_TARGET_INVALID")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".acceptance-evidence-", suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return destination


def _bound_evidence(
    evidence: dict[str, object], settings: Settings,
    run: PipelineRun | None, fingerprint: str,
) -> bool:
    """Accept only current, schema-bound evidence signed by local configuration."""
    if run is None or not isinstance(evidence.get("run_id"), int):
        return False
    finished_at = _as_utc(run.finished_at)
    generated_at = _evidence_timestamp(evidence.get("generated_at"))
    now = datetime.now(UTC)
    return (
        _evidence_signature_valid(evidence, settings)
        and bool(_evidence_checks(evidence))
        and evidence.get("schema_version") == _EVIDENCE_SCHEMA_VERSION
        and evidence.get("producer") == _EVIDENCE_PRODUCER
        and evidence["run_id"] == run.id
        and evidence.get("fingerprint") == fingerprint
        and finished_at is not None
        and generated_at is not None
        and finished_at <= generated_at <= now
        and now - generated_at <= _MAX_EVIDENCE_AGE
    )


def _retryable_failure_is_indexed(
    output: RebuildOutput | None, index_texts: dict[int, list[str]], source: ArchivedFile,
) -> bool:
    return bool(
        output
        and output.status == "failed"
        and output.error_code
        and _attachment_status_is_indexed(index_texts, source, "转换失败，等待重试")
    )


def _attachment_status_is_indexed(
    index_texts: dict[int, list[str]], source: ArchivedFile, status: str,
) -> bool:
    label = source.original_name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    expected = f"- {label}：{status}"
    return any(expected in text for text in index_texts.get(source.oa_item_id, ()))


def _failed_output_is_allowed(
    output: RebuildOutput,
    *,
    source_by_id: dict[int, ArchivedFile],
    ordinary_supported_ids: set[int],
    unsupported_ids: set[int],
    index_texts: dict[int, list[str]],
) -> bool:
    """Allow only indexed attachment-local failures defined by the rebuild spec."""
    source = source_by_id.get(output.source_file_id)
    if source is None or output.status != "failed" or not output.error_code:
        return False
    if output.kind == "parse":
        if source.id in unsupported_ids:
            return (
                output.error_code == "UNSUPPORTED_FORMAT"
                and _attachment_status_is_indexed(
                    index_texts, source, "暂不支持转换",
                )
            )
        return (
            source.id in ordinary_supported_ids
            and output.error_code != "UNSUPPORTED_FORMAT"
            and _attachment_status_is_indexed(
                index_texts, source, "转换失败，等待重试",
            )
        )
    return (
        output.kind == "attachment_markdown"
        and source.id in ordinary_supported_ids
        and _attachment_status_is_indexed(
            index_texts, source, "转换失败，等待重试",
        )
    )


def _evidence_count(evidence: dict[str, object], key: str) -> int:
    value = evidence.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def validate_rebuild(session: Session, settings: Settings, run_id: int) -> list[ValidationCheck]:
    """Validate a current rebuild run without mutating its session or filesystem.

    The result deliberately contains only stable codes and counts; no OA-derived
    metadata, filenames, or local paths can escape this function.
    """
    with session.no_autoflush:
        run = session.get(PipelineRun, run_id)
        outputs = list(session.scalars(select(RebuildOutput).where(RebuildOutput.run_id == run_id)))
        sources = list(session.scalars(select(ArchivedFile).join(OAItem).where(OAItem.source_channel == "done")))
        items = {item.id: item for item in session.scalars(select(OAItem).where(OAItem.source_channel == "done"))}
    source_by_id = {source.id: source for source in sources}
    originals = [output for output in outputs if output.kind == "original"]
    parses = [output for output in outputs if output.kind == "parse"]
    derived = [output for output in outputs if output.kind in _DERIVED_KINDS]
    valid_originals = [
        output for output in originals
        if (source := source_by_id.get(output.source_file_id)) is not None
        and _valid_original(settings, output, source, items.get(output.oa_item_id))
    ]
    baseline_source_ids = {output.source_file_id for output in originals}
    run_succeeded = bool(
        run
        and run.pipeline_type == "data_rebuild"
        and run.status == "completed"
        and run.total_tasks > 0
        and run.completed_tasks == run.total_tasks
        and run.failed_tasks == 0
        and run.finished_at is not None
    )
    originals_complete = bool(
        run_succeeded
        and originals
        and len(baseline_source_ids) == len(originals)
        and all(output.status == "success" for output in originals)
        and len(valid_originals) == len(originals)
        and {output.source_file_id for output in valid_originals} == baseline_source_ids
    )
    original_source_ids = {output.source_file_id for output in valid_originals}

    root = resolve_rebuild_root(settings)
    archive_files, archive_tree_safe = _regular_files(root / "archive")
    expected_original_paths = {
        _safe_target(settings, output.target_relpath)
        for output in valid_originals
    }
    expected_original_paths.discard(None)
    unknown_originals = sum(path.resolve() not in {value.resolve() for value in expected_original_paths} for path in archive_files)

    candidate_item_ids = {
        source.oa_item_id
        for source in source_by_id.values()
        if source.id in original_source_ids and source.oa_item_id in items
    }
    confirmed_candidate_ids = {item_id for item_id in candidate_item_ids if items[item_id].classification_state == "confirmed"}
    indexes = [output for output in derived if output.kind == "item_index" and output.status == "success"]
    bodies = [output for output in derived if output.kind == "body_markdown" and output.status == "success"]
    attachments = [output for output in derived if output.kind == "attachment_markdown" and output.status == "success"]
    valid_indexes = [output for output in indexes if _valid_markdown(settings, output)]
    indexes_by_item = {item_id: sum(output.oa_item_id == item_id for output in valid_indexes) for item_id in confirmed_candidate_ids}
    numbered = {item_id for item_id in confirmed_candidate_ids if body_markdown_filename(items[item_id]) is not None}
    valid_bodies = [output for output in bodies if _valid_markdown(settings, output)]
    unnumbered = confirmed_candidate_ids - numbered
    supported_sources = [
        source for source in source_by_id.values()
        if source.id in original_source_ids
        and Path(source.original_name).suffix.casefold() in settings.parser.supported_extensions
        and source.file_role in MARKDOWN_SOURCE_ROLES
    ]
    valid_parses = [output for output in parses if (source := source_by_id.get(output.source_file_id)) is not None and _valid_parse(settings, output, source)]
    selected_body_sources = {output.source_file_id for output in valid_bodies}
    ordinary_supported = [source for source in supported_sources if source.id not in selected_body_sources]
    valid_attachments = [output for output in attachments if _valid_markdown(settings, output)]
    index_texts: dict[int, list[str]] = {}
    for output in valid_indexes:
        path = _safe_target(settings, output.target_relpath)
        if path:
            try:
                index_texts.setdefault(output.oa_item_id, []).append(
                    path.read_text(encoding="utf-8")
                )
            except OSError:
                pass
    valid_parse_source_ids = {output.source_file_id for output in valid_parses}
    attachment_by_source = {output.source_file_id: output for output in valid_attachments}
    retry_by_source = {
        output.source_file_id: output
        for output in derived
        if output.kind == "attachment_markdown" and output.status == "failed"
    }
    parse_retry_by_source = {
        output.source_file_id: output
        for output in parses
        if (
            output.status == "failed"
            and output.error_code
            and output.error_code != "UNSUPPORTED_FORMAT"
        )
    }
    supported_accounted = sum(
        (
            source.id in valid_parse_source_ids
            and source.id in attachment_by_source
        ) or _retryable_failure_is_indexed(
            retry_by_source.get(source.id), index_texts, source,
        ) or _retryable_failure_is_indexed(
            parse_retry_by_source.get(source.id), index_texts, source,
        )
        for source in ordinary_supported
    )
    unsupported_sources = [
        source for source in source_by_id.values()
        if source.id in original_source_ids
        and source.file_role in MARKDOWN_SOURCE_ROLES
        and Path(source.original_name).suffix.casefold() not in settings.parser.supported_extensions
    ]
    unsupported_reported = sum(
        any("暂不支持转换" in text for text in index_texts.get(source.oa_item_id, ()))
        and any(
            output.source_file_id == source.id
            and output.status == "failed"
            and output.error_code == "UNSUPPORTED_FORMAT"
            for output in parses
        )
        for source in unsupported_sources
    )

    ordinary_supported_ids = {source.id for source in ordinary_supported}
    unsupported_ids = {source.id for source in unsupported_sources}
    outputs_terminal = all(
        output.status == "success" or _failed_output_is_allowed(
            output,
            source_by_id=source_by_id,
            ordinary_supported_ids=ordinary_supported_ids,
            unsupported_ids=unsupported_ids,
            index_texts=index_texts,
        )
        for output in outputs
    )
    originals_complete = originals_complete and outputs_terminal

    checks: list[ValidationCheck] = []
    baseline_count = len(originals)
    checks.append(_check(
        "ORIGINALS_COMPLETE", baseline_count, baseline_count if originals_complete else -1,
    ))
    checks.append(_check(
        "ORIGINAL_HASHES_MATCH", baseline_count, baseline_count if originals_complete else -1,
    ))
    checks.append(_check("NO_UNKNOWN_ORIGINALS", 0, unknown_originals if archive_tree_safe else unknown_originals + 1))
    checks.append(_check("CONFIRMED_OUTPUTS_ONLY", 0, sum(items.get(output.oa_item_id) is None or items[output.oa_item_id].classification_state != "confirmed" for output in derived)))
    checks.append(_check("INDEX_EXACTLY_ONE", len(confirmed_candidate_ids), sum(count == 1 for count in indexes_by_item.values())))
    checks.append(_check("NUMBERED_BODY_COMPLETE", len(numbered), sum(sum(output.oa_item_id == item_id for output in valid_bodies) == 1 for item_id in numbered)))
    checks.append(_check("UNNUMBERED_BODY_ABSENT", 0, sum(output.oa_item_id in unnumbered for output in bodies)))
    checks.append(_check(
        "SUPPORTED_ATTACHMENTS_ACCOUNTED", len(ordinary_supported), supported_accounted,
    ))
    checks.append(_check("UNSUPPORTED_ATTACHMENTS_INDEXED", len(unsupported_sources), unsupported_reported))

    markdown_files, markdown_tree_safe = _regular_files(root / "markdown")
    link_expected, link_actual = _link_counts([path for path in markdown_files if path.suffix.casefold() == ".md"], root)
    checks.append(_check("ALL_LINKS_RESOLVE", link_expected, link_actual if markdown_tree_safe else -1))

    published_outputs = [output for output in derived if output.status == "success"]
    searchable_outputs = sum(
        output.oa_item_id in items
        and _expected_markdown_path(
            items[output.oa_item_id], output, source_by_id.get(output.source_file_id)
        ) == output.target_relpath
        and (path := _safe_target(settings, output.target_relpath)) is not None
        and _frontmatter_matches(path, items[output.oa_item_id])
        for output in published_outputs
    )
    evidence = _acceptance_evidence(settings)
    evidence_is_bound = _bound_evidence(
        evidence, settings, run, _run_artifact_fingerprint(settings, run_id, outputs),
    )
    evidence_checks = _evidence_checks(evidence) if evidence_is_bound else {}
    index_by_item = {output.oa_item_id: output for output in valid_indexes}
    filterable_items = sum(
        (index := index_by_item.get(item_id)) is not None
        and _expected_markdown_path(items[item_id], index, None) is not None
        for item_id in confirmed_candidate_ids
    )
    checks.append(_check(
        "WEBUI_FILTER_CONTRACT",
        len(confirmed_candidate_ids),
        filterable_items if evidence_checks.get("webui_filter_contract") is True else 0,
    ))
    checks.append(_check("OBSIDIAN_SEARCH_FIELDS", len(published_outputs), searchable_outputs))
    checks.append(_check(
        "SAMPLE_EVIDENCE_COMPLETE", 200,
        (
            min(_evidence_count(evidence_checks, "internal_sample_count"), 100)
            + min(_evidence_count(evidence_checks, "external_sample_count"), 100)
            if evidence_checks else 0
        ),
    ))

    try:
        root_entries = list(root.iterdir()) if root.exists() else []
        unexpected_root_dirs = sum(entry.name not in _ROOT_DIRS or not entry.is_dir() or entry.is_symlink() for entry in root_entries)
    except OSError:
        unexpected_root_dirs = 1
    checks.append(_check("REBUILD_ROOT_LAYOUT_CLEAN", 0, unexpected_root_dirs))
    checks.append(_check(
        "AUTOMATED_GATES_CONFIRMED", 4,
        sum(
            evidence_checks.get(key) is True
            for key in (
                "automated_tests_passed", "frontend_check_passed", "build_passed",
                "synthetic_smoke_passed",
            )
        ) if evidence_checks else 0,
    ))
    return checks


def validation_passed(checks: Sequence[ValidationCheck]) -> bool:
    """Return true only for the complete, fixed set of passing acceptance gates."""
    return len(checks) == 15 and all(check.ok for check in checks)
