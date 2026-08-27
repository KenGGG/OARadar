"""Minimal vertical-slice helpers for a real OA candidate backfill."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import statistics
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.classification.evidence import (
    ClassificationItem,
    Evidence,
    RuleOutcome,
)
from oa_knowledge.classification.internal_classification import (
    LocalQwenInternalClassifier,
    classify_by_content,
    extract_document_type,
)
from oa_knowledge.classification.metadata_rules import find_configured_document_number
from oa_knowledge.classification.parse_cache import ParseCacheService, ParseRequest
from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.classification.service import (
    ClassificationService,
    CreateClassificationRun,
)
from oa_knowledge.config import Settings
from oa_knowledge.curation.canonical import sanitize_component
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    OAItem,
    OAManifestItem,
)
from oa_knowledge.enrich.provider import make_llm_client
from oa_knowledge.parsers.router import resolve_parser_version
from oa_knowledge.runtime_paths import resolve_cache_path, resolve_original_path
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES


@dataclass(frozen=True, slots=True)
class SampleItem:
    oa_item_key: str
    bucket: str
    reason: str


@dataclass(frozen=True, slots=True)
class BackfillMVPRequest:
    run_id: str
    sample_size: int = 100
    all_targets: bool = False
    target_keys: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class BackfillException:
    oa_item_key: str
    file_id: int | None
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class BackfillMVPResult:
    run_id: str
    output_root: Path
    selected: int
    processed: int
    packages: int
    classified: int
    needs_review: int
    attachments_attempted: int
    attachments_converted: int
    attachments_failed: int
    attachments_skipped: int
    exceptions: tuple[BackfillException, ...]


@dataclass(frozen=True, slots=True)
class CanonicalAttachment:
    """One candidate attachment plus every archive alias for the same file."""

    file: ArchivedFile
    aliases: tuple[ArchivedFile, ...]


_SPECIAL_PRIORITY = (
    "attachment_abnormal",
    "no_attachment",
    "multiple_attachments",
    "mixed_initiator",
    "file_transfer",
    "external_document_number",
    "internal_template",
    "no_document_number",
)


def _evenly_spaced(rows: list[SampleItem], count: int) -> list[SampleItem]:
    if count <= 0:
        return []
    if count >= len(rows):
        return rows
    return [rows[(index * len(rows)) // count] for index in range(count)]


def select_representative_items(
    session: Session,
    config: PrivateClassificationConfig,
    sample_size: int,
) -> tuple[SampleItem, ...]:
    """Select a stable ordinary-heavy sample using existing OA fields only."""
    if sample_size < 1:
        raise ValueError("sample_size must be positive")

    manifests = list(
        session.scalars(
            select(OAManifestItem)
            .where(
                OAManifestItem.processing_status != "skipped",
                func.coalesce(OAManifestItem.matched_exclusion_keyword, "") == "",
            )
            .order_by(OAManifestItem.oa_item_key)
        )
    )
    if sample_size > len(manifests):
        raise ValueError("sample_size exceeds available target OA items")

    keys = tuple(row.oa_item_key for row in manifests)
    items = {
        row.oa_item_key: row
        for row in session.scalars(
            select(OAItem).where(
                OAItem.source_channel == "done", OAItem.oa_item_key.in_(keys)
            )
        )
    }
    item_ids = tuple(row.id for row in items.values())
    file_stats = {
        item_id: (count, abnormal)
        for item_id, count, abnormal in session.execute(
            select(
                ArchivedFile.oa_item_id,
                func.count(ArchivedFile.id),
                func.sum(
                    case((ArchivedFile.download_status != "verified", 1), else_=0)
                ),
            )
            .where(ArchivedFile.oa_item_id.in_(item_ids))
            .group_by(ArchivedFile.oa_item_id)
        )
    } if item_ids else {}
    initiator_roles = {
        identifier.casefold(): profile.role
        for identifier, profile in config.initiators.items()
    }
    for profile in config.initiators.values():
        for alias in profile.aliases:
            initiator_roles[alias.casefold()] = profile.role

    by_bucket: dict[str, list[SampleItem]] = {
        bucket: [] for bucket in _SPECIAL_PRIORITY
    }
    ordinary: list[SampleItem] = []
    for manifest in manifests:
        item = items.get(manifest.oa_item_key)
        document_number = (
            item.document_number if item and item.document_number else None
        ) or find_configured_document_number(manifest.title or "", config)
        initiator = (manifest.sender or (item.sender if item else "") or "").strip()
        role = initiator_roles.get(initiator.casefold(), "unknown")
        file_count, abnormal_count = file_stats.get(item.id, (0, 0)) if item else (0, 0)
        title = manifest.title or ""

        bucket = "ordinary"
        reason = "stable ordinary target"
        if abnormal_count:
            bucket, reason = "attachment_abnormal", "attachment status is not verified"
        elif manifest.no_attachment_confirmed:
            bucket, reason = "no_attachment", "manifest confirms no attachment"
        elif file_count >= 3:
            bucket, reason = "multiple_attachments", f"attachment_count={file_count}"
        elif role == "mixed":
            bucket, reason = "mixed_initiator", "configured initiator role=mixed"
        elif re.search(r"(?:文件传阅|传阅件|传阅-|【传阅】)", title):
            bucket, reason = "file_transfer", "title contains a transfer marker"
        elif document_number and any(
            re.search(rule.pattern, document_number)
            for rule in config.document_number_issuers
        ):
            bucket, reason = "external_document_number", "document number matches issuer rule"
        elif any(re.search(rule.pattern, title) for rule in config.title_templates):
            bucket, reason = "internal_template", "title matches configured template"
        elif not document_number and role == "unknown":
            bucket, reason = "no_document_number", "unknown initiator and no document number"

        sample = SampleItem(manifest.oa_item_key, bucket, reason)
        if bucket == "ordinary":
            ordinary.append(sample)
        else:
            by_bucket[bucket].append(sample)

    special_limit = min(35, sample_size * 35 // 100)
    special: list[SampleItem] = []
    round_index = 0
    while len(special) < special_limit:
        added = False
        for bucket in _SPECIAL_PRIORITY:
            rows = by_bucket[bucket]
            if round_index < len(rows) and len(special) < special_limit:
                special.append(rows[round_index])
                added = True
        if not added:
            break
        round_index += 1

    ordinary_needed = sample_size - len(special)
    selected = [*_evenly_spaced(ordinary, ordinary_needed), *special]
    if len(selected) < sample_size:
        chosen = {row.oa_item_key for row in selected}
        remaining = [
            row
            for bucket in _SPECIAL_PRIORITY
            for row in by_bucket[bucket]
            if row.oa_item_key not in chosen
        ]
        selected.extend(remaining[: sample_size - len(selected)])
    if len(selected) != sample_size:
        raise ValueError("not enough target OA items for requested sample")
    return tuple(sorted(selected, key=lambda row: row.oa_item_key))


_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_DIRECT_TEXT = frozenset(
    {".md", ".markdown", ".txt", ".html", ".htm", ".csv", ".json", ".xml"}
)
_VISUAL_DOCUMENTS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"})
_OFFICE_DOCUMENTS = frozenset({".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"})
_PARSE_PROFILE = "backfill-mvp-v3"
_MIN_EFFECTIVE_CHARACTERS = 40
_MIN_QUALITY_SCORE = 0.5


def _is_truncated_filename(name: str) -> bool:
    return bool(re.search(r"(?:\.{3,}|…)(?:\.[^.]+)?$", name.strip()))


def _filename_aliases(left: str, right: str) -> bool:
    """Recognize OA's shortened display name without merging unrelated files."""
    if left == right:
        return True
    left_path, right_path = Path(left), Path(right)
    if left_path.suffix.lower() != right_path.suffix.lower():
        return False
    left_stem, right_stem = left_path.stem, right_path.stem
    if _is_truncated_filename(left):
        prefix = re.sub(r"(?:\.{3,}|…)$", "", left_stem)
        return len(prefix) >= 3 and right_stem.startswith(prefix)
    if _is_truncated_filename(right):
        prefix = re.sub(r"(?:\.{3,}|…)$", "", right_stem)
        return len(prefix) >= 3 and left_stem.startswith(prefix)
    return False


def _attachment_preference(file: ArchivedFile) -> tuple[int, int, int]:
    """Choose the readable CAP4 attachment when two UI surfaces are aliases."""
    return (
        0 if _is_truncated_filename(file.original_name) else 1,
        1 if file.file_role == "official_attachment" else 0,
        len(file.original_name),
    )


def canonicalize_attachment_aliases(
    files: list[ArchivedFile],
) -> tuple[CanonicalAttachment, ...]:
    """Collapse only known CAP4/panel duplicate aliases within one container."""
    groups: list[list[ArchivedFile]] = []
    for file in files:
        for group in groups:
            representative = group[0]
            compatible_roles = {
                representative.file_role,
                file.file_role,
            } <= {"direct_attachment", "official_attachment"}
            if (
                compatible_roles
                and representative.source_container_key == file.source_container_key
                and representative.sha256
                and representative.sha256 == file.sha256
                and _filename_aliases(representative.original_name, file.original_name)
            ):
                group.append(file)
                break
        else:
            groups.append([file])
    return tuple(
        CanonicalAttachment(
            file=max(group, key=_attachment_preference),
            aliases=tuple(sorted(group, key=lambda file: file.id)),
        )
        for group in groups
    )


def _parse_engines(suffix: str, *, mineru_enabled: bool) -> tuple[str, ...]:
    if suffix in _VISUAL_DOCUMENTS:
        return ("mineru", "markitdown") if mineru_enabled else ("markitdown",)
    if suffix == ".doc":
        return ("markitdown", "wv")
    if suffix in _OFFICE_DOCUMENTS or suffix in {".html", ".htm"}:
        return ("markitdown",)
    return ()


def _parse_quality_reasons(body: str, quality_score: float | None) -> tuple[str, ...]:
    reasons: list[str] = []
    stripped = body.strip()
    if not stripped:
        reasons.append("empty_body")
    else:
        effective = re.findall(r"[A-Za-z0-9_\u3400-\u9fff]", stripped)
        if len(effective) < _MIN_EFFECTIVE_CHARACTERS:
            reasons.append("too_few_effective_characters")
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if (
            len(lines) >= 20
            and statistics.median(len(line) for line in lines) <= 28
            and sum(len(line) <= 32 for line in lines) / len(lines) >= 0.8
        ):
            reasons.append("fragmented_lines")
    if quality_score is None or quality_score < _MIN_QUALITY_SCORE:
        reasons.append("low_quality_score")
    return tuple(reasons)


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BackfillMVPService:
    """Connect existing classification and parsing into one candidate build."""

    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        config: PrivateClassificationConfig,
        *,
        private_config_sha256: str,
        qwen_client: object | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = session_factory
        self._config = config
        self._private_config_sha256 = private_config_sha256
        self._parse_cache = ParseCacheService(session_factory, settings)
        self._qwen_client = qwen_client
        self._qwen_outcomes: Counter[str] = Counter()

    def run(self, request: BackfillMVPRequest) -> BackfillMVPResult:
        if not _RUN_ID.fullmatch(request.run_id):
            raise ValueError("run_id must be a safe local identifier")
        self._qwen_outcomes = Counter()
        selected = self._selected(request)
        target_keys = tuple(row.oa_item_key for row in selected)
        manifest_sha, exclusion_sha = self._input_hashes()
        input_sha = self._run_input_sha(selected, manifest_sha, exclusion_sha)
        builds = self._settings.markdown_root / ".builds"
        builds.mkdir(parents=True, exist_ok=True)
        final = builds / request.run_id
        if final.exists():
            return self._load_existing(final, input_sha)
        classification = ClassificationService(
            self._sessions, self._config, outcome_hook=self._resolve_internal_outcome
        )
        run = classification.create_run(
            CreateClassificationRun(
                run_id=request.run_id,
                run_kind="full" if request.all_targets else "incremental",
                manifest_sha256=manifest_sha,
                exclusion_policy_sha256=exclusion_sha,
                rule_version="backfill-mvp-v3.1",
                schema_version="classification-v1",
                prompt_version="internal-business-v3.1",
                model_name=self._settings.llm.model,
                private_config_sha256=self._private_config_sha256,
                target_keys=target_keys,
            )
        )
        classification.process_next(
            run.run_id, limit=run.target_count + run.excluded_count
        )
        classification.complete(run.run_id)

        stage = Path(tempfile.mkdtemp(prefix=f".{request.run_id}.", dir=builds))
        exceptions: list[BackfillException] = []
        classification_rows: list[dict[str, object]] = []
        classified = needs_review = converted = failed = skipped = attempted = 0
        attachment_source_records = duplicate_aliases = 0
        packages = 0
        try:
            for sample in selected:
                with self._sessions() as session:
                    item = session.scalar(
                        select(OAItem).where(
                            OAItem.oa_item_key == sample.oa_item_key,
                            OAItem.source_channel == "done",
                        )
                    )
                    decision = session.scalar(
                        select(ClassificationDecision).where(
                            ClassificationDecision.oa_item_key == sample.oa_item_key,
                            ClassificationDecision.is_current.is_(True),
                        )
                    )
                    manifest = session.scalar(
                        select(OAManifestItem).where(
                            OAManifestItem.oa_item_key == sample.oa_item_key
                        )
                    )
                    files = (
                        list(
                            session.scalars(
                                select(ArchivedFile)
                                .where(
                                    ArchivedFile.oa_item_id == item.id,
                                    ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
                                )
                                .order_by(ArchivedFile.id)
                            )
                        )
                        if item is not None
                        else []
                    )
                    if item is None or decision is None or manifest is None:
                        raise RuntimeError("selected OA classification snapshot is missing")
                    session.expunge(item)
                    session.expunge(decision)
                    session.expunge(manifest)
                    for file in files:
                        session.expunge(file)

                package = stage / "packages" / self._package_relpath(item, decision)
                package.mkdir(parents=True, exist_ok=False)
                packages += 1
                if decision.classification_status == "classified":
                    classified += 1
                else:
                    needs_review += 1
                classification_rows.append(
                    {
                        "oa_item_key": item.oa_item_key,
                        "classification_status": decision.classification_status,
                        "content_origin": decision.content_origin or "",
                        "business_category": decision.business_category or "",
                        "document_type": decision.document_type or "",
                        "canonical_issuer": decision.canonical_issuer or "",
                        "content_integrity_status": decision.content_integrity_status,
                        "confidence": decision.classification_confidence,
                        "decision_source": decision.decision_source,
                        "reason": decision.classification_reason_json,
                    }
                )

                canonical_files = canonicalize_attachment_aliases(files)
                attachment_source_records += len(files)
                duplicate_aliases += len(files) - len(canonical_files)
                links: list[tuple[str, str]] = []
                item_exceptions: list[BackfillException] = []
                for ordinal, attachment in enumerate(canonical_files, 1):
                    file = attachment.file
                    attempted += 1
                    outcome, filename, problem = self._convert_attachment(
                        package, attachment, ordinal
                    )
                    if outcome == "converted" and filename is not None:
                        converted += 1
                        links.append((filename, file.original_name))
                    elif outcome == "skipped":
                        skipped += 1
                    else:
                        failed += 1
                    if problem is not None:
                        item_exceptions.append(
                            BackfillException(
                                sample.oa_item_key,
                                file.id,
                                problem[0],
                                problem[1],
                            )
                        )
                exceptions.extend(item_exceptions)
                self._write_index(
                    package,
                    item,
                    manifest,
                    decision,
                    links,
                    item_exceptions,
                )
            self._write_reports(
                stage,
                request,
                selected,
                classification_rows,
                exceptions,
                input_sha=input_sha,
                classified=classified,
                needs_review=needs_review,
                packages=packages,
                attachments_attempted=attempted,
                attachments_converted=converted,
                attachments_failed=failed,
                attachments_skipped=skipped,
                attachment_source_records=attachment_source_records,
                attachment_duplicate_aliases=duplicate_aliases,
                qwen_outcomes=dict(sorted(self._qwen_outcomes.items())),
            )
            os.replace(stage, final)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

        return BackfillMVPResult(
            run_id=request.run_id,
            output_root=final,
            selected=len(selected),
            processed=len(selected),
            packages=packages,
            classified=classified,
            needs_review=needs_review,
            attachments_attempted=attempted,
            attachments_converted=converted,
            attachments_failed=failed,
            attachments_skipped=skipped,
            exceptions=tuple(exceptions),
        )

    def _run_input_sha(
        self,
        selected: tuple[SampleItem, ...],
        manifest_sha: str,
        exclusion_sha: str,
    ) -> str:
        keys = tuple(row.oa_item_key for row in selected)
        with self._sessions() as session:
            items = {
                row.oa_item_key: row
                for row in session.scalars(
                    select(OAItem).where(OAItem.oa_item_key.in_(keys))
                )
            }
            item_ids = tuple(row.id for row in items.values())
            files = list(
                session.scalars(
                    select(ArchivedFile)
                    .where(ArchivedFile.oa_item_id.in_(item_ids))
                    .order_by(ArchivedFile.oa_item_id, ArchivedFile.id)
                )
            ) if item_ids else []
        file_rows = [
            {
                "oa_item_key": next(
                    key for key, item in items.items() if item.id == file.oa_item_id
                ),
                "file_id": file.id,
                "role": file.file_role,
                "path": file.local_relpath,
                "size": file.size_bytes,
                "sha256": file.sha256,
                "status": file.download_status,
            }
            for file in files
        ]
        return _json_sha256(
            {
                "manifest_sha256": manifest_sha,
                "exclusion_sha256": exclusion_sha,
                "private_config_sha256": self._private_config_sha256,
                "selected": [
                    {
                        "oa_item_key": row.oa_item_key,
                        "bucket": row.bucket,
                        "reason": row.reason,
                    }
                    for row in selected
                ],
                "files": file_rows,
                "profile": _PARSE_PROFILE,
            }
        )

    def _selected(self, request: BackfillMVPRequest) -> tuple[SampleItem, ...]:
        if request.target_keys is not None:
            if not request.target_keys:
                raise ValueError("target_keys must not be empty")
            if len(set(request.target_keys)) != len(request.target_keys):
                raise ValueError("target_keys must be unique")
            return tuple(
                SampleItem(key, "explicit", "explicit MVP target")
                for key in sorted(request.target_keys)
            )
        with self._sessions() as session:
            if request.all_targets:
                keys = tuple(
                    session.scalars(
                        select(OAManifestItem.oa_item_key)
                        .where(
                            OAManifestItem.processing_status != "skipped",
                            func.coalesce(
                                OAManifestItem.matched_exclusion_keyword, ""
                            )
                            == "",
                        )
                        .order_by(OAManifestItem.oa_item_key)
                    )
                )
                return tuple(
                    SampleItem(key, "all_targets", "full target set") for key in keys
                )
            return select_representative_items(
                session, self._config, request.sample_size
            )

    def _input_hashes(self) -> tuple[str, str]:
        with self._sessions() as session:
            rows = list(
                session.execute(
                    select(
                        OAManifestItem.oa_item_key,
                        OAManifestItem.title,
                        OAManifestItem.sender,
                        OAManifestItem.completed_at,
                        OAManifestItem.processing_status,
                        OAManifestItem.matched_exclusion_keyword,
                        OAManifestItem.no_attachment_confirmed,
                    ).order_by(OAManifestItem.oa_item_key)
                )
            )
        manifest = [dict(row._mapping) for row in rows]
        excluded = [
            row["oa_item_key"]
            for row in manifest
            if row["processing_status"] == "skipped"
            or (row["matched_exclusion_keyword"] or "").strip()
        ]
        return _json_sha256(manifest), _json_sha256(excluded)

    def _baseline_counts(self) -> tuple[int, int, int]:
        with self._sessions() as session:
            total = session.scalar(
                select(func.count()).select_from(OAManifestItem)
            ) or 0
            excluded = session.scalar(
                select(func.count())
                .select_from(OAManifestItem)
                .where(
                    (OAManifestItem.processing_status == "skipped")
                    | (func.coalesce(OAManifestItem.matched_exclusion_keyword, "") != "")
                )
            ) or 0
        return total, excluded, total - excluded

    @staticmethod
    def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _write_reports(
        self,
        stage: Path,
        request: BackfillMVPRequest,
        selected: tuple[SampleItem, ...],
        classification_rows: list[dict[str, object]],
        exceptions: list[BackfillException],
        *,
        input_sha: str,
        classified: int,
        needs_review: int,
        packages: int,
        attachments_attempted: int,
        attachments_converted: int,
        attachments_failed: int,
        attachments_skipped: int,
        attachment_source_records: int,
        attachment_duplicate_aliases: int,
        qwen_outcomes: dict[str, int],
    ) -> None:
        self._write_csv(
            stage / "sample.csv",
            ("oa_item_key", "bucket", "reason"),
            [
                {
                    "oa_item_key": row.oa_item_key,
                    "bucket": row.bucket,
                    "reason": row.reason,
                }
                for row in selected
            ],
        )
        self._write_csv(
            stage / "classification.csv",
            (
                "oa_item_key",
                "classification_status",
                "content_origin",
                "business_category",
                "document_type",
                "canonical_issuer",
                "content_integrity_status",
                "confidence",
                "decision_source",
                "reason",
            ),
            sorted(classification_rows, key=lambda row: str(row["oa_item_key"])),
        )
        exception_rows = [
            {
                "oa_item_key": row.oa_item_key,
                "file_id": row.file_id if row.file_id is not None else "",
                "code": row.code,
                "detail": row.detail,
            }
            for row in exceptions
        ]
        self._write_csv(
            stage / "exceptions.csv",
            ("oa_item_key", "file_id", "code", "detail"),
            exception_rows,
        )
        total, excluded, target = self._baseline_counts()
        counts = {
            "manifest_total": total,
            "excluded": excluded,
            "target": target,
            "selected": len(selected),
            "processed": len(selected),
            "classified": classified,
            "needs_review": needs_review,
            "packages": packages,
            "attachments_source_records": attachment_source_records,
            "attachments_duplicate_aliases": attachment_duplicate_aliases,
            "attachments_attempted": attachments_attempted,
            "attachments_converted": attachments_converted,
            "attachments_failed": attachments_failed,
            "attachments_skipped": attachments_skipped,
            "exceptions": len(exceptions),
        }
        selected_equation = (
            counts["selected"]
            == counts["packages"]
            == counts["classified"] + counts["needs_review"]
        )
        attachment_equation = counts["attachments_attempted"] == (
            counts["attachments_converted"]
            + counts["attachments_failed"]
            + counts["attachments_skipped"]
        )
        source_record_equation = counts["attachments_source_records"] == (
            counts["attachments_attempted"]
            + counts["attachments_duplicate_aliases"]
        )
        if not selected_equation or not attachment_equation or not source_record_equation:
            raise RuntimeError("backfill MVP counts do not reconcile")
        files = [
            {
                "path": path.relative_to(stage).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in sorted(stage.rglob("*"))
            if path.is_file() and path.name != "build_manifest.json"
        ]
        manifest = {
            "schema_version": _PARSE_PROFILE,
            "run_id": request.run_id,
            "input_sha256": input_sha,
            "counts": counts,
            "exception_codes": dict(sorted(Counter(row.code for row in exceptions).items())),
            "qwen_outcomes": qwen_outcomes,
            "parser_environment": self._parser_environment(),
            "reconciliation": {
                "ok": True,
                "selected_equation": selected_equation,
                "attachment_equation": attachment_equation,
                "source_record_equation": source_record_equation,
            },
            "files": files,
            "note": "files lists every candidate payload except this self-describing manifest",
        }
        (stage / "build_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _parser_environment(self) -> dict[str, str]:
        from oa_knowledge.parsers.antiword_parser import antiword_engine_version
        from oa_knowledge.parsers.wv_parser import wv_engine_version

        environment: dict[str, str] = {}
        for name, resolver in (("antiword", antiword_engine_version), ("wv", wv_engine_version)):
            try:
                environment[name] = resolver(self._settings)
            except (OSError, RuntimeError):
                environment[name] = "unavailable"
        return environment


    def _load_existing(self, root: Path, input_sha: str) -> BackfillMVPResult:
        try:
            manifest = json.loads(
                (root / "build_manifest.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise ValueError("existing candidate run has no valid manifest") from exc
        if manifest.get("input_sha256") != input_sha:
            raise ValueError("existing candidate run has different inputs")
        if not manifest.get("reconciliation", {}).get("ok"):
            raise ValueError("existing candidate run is not reconciled")
        for row in manifest.get("files", []):
            path = root / row["path"]
            if (
                not path.is_file()
                or path.stat().st_size != row["size"]
                or sha256_file(path) != row["sha256"]
            ):
                raise ValueError("existing candidate file verification failed")
        exceptions: list[BackfillException] = []
        with (root / "exceptions.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            for row in csv.DictReader(stream):
                exceptions.append(
                    BackfillException(
                        row["oa_item_key"],
                        int(row["file_id"]) if row["file_id"] else None,
                        row["code"],
                        row["detail"],
                    )
                )
        counts = manifest["counts"]
        return BackfillMVPResult(
            run_id=manifest["run_id"],
            output_root=root,
            selected=counts["selected"],
            processed=counts["processed"],
            packages=counts["packages"],
            classified=counts["classified"],
            needs_review=counts["needs_review"],
            attachments_attempted=counts["attachments_attempted"],
            attachments_converted=counts["attachments_converted"],
            attachments_failed=counts["attachments_failed"],
            attachments_skipped=counts["attachments_skipped"],
            exceptions=tuple(exceptions),
        )

    def _package_relpath(
        self, item: OAItem, decision: ClassificationDecision
    ) -> Path:
        completed = item.completed_at
        year = f"{completed.year:04d}" if completed else "unknown-year"
        month = f"{completed.month:02d}" if completed else "unknown-month"
        day = completed.strftime("%Y%m%d") if completed else "unknown-date"
        suffix = hashlib.sha256(item.oa_item_key.encode("utf-8")).hexdigest()[:12]
        title = sanitize_component(
            decision.normalized_title or item.title,
            collision_key=suffix,
            max_length=100,
        )
        leaf = sanitize_component(
            f"{day}-{title}--oa_{suffix}", collision_key=suffix, max_length=140
        )
        if decision.classification_status != "classified":
            return Path("needs_review", year, month, leaf)
        if decision.content_origin == "internal":
            category = sanitize_component(decision.business_category or "99_其他内部")
            return Path("internal", category, year, month, leaf)
        issuer = sanitize_component(decision.canonical_issuer or "未识别发文单位")
        return Path("external", issuer, year, month, leaf)

    def _convert_attachment(
        self, package: Path, attachment: CanonicalAttachment, ordinal: int
    ) -> tuple[str, str | None, tuple[str, str] | None]:
        file = attachment.file
        if file.download_status != "verified":
            return "failed", None, ("content_not_verified", file.download_status)
        if not file.local_relpath:
            return "failed", None, ("file_missing", "source path is absent")
        try:
            source = resolve_original_path(self._settings, file.local_relpath)
        except ValueError:
            return "failed", None, ("unsafe_source_path", "source is outside originals")
        if not source.is_file():
            return "failed", None, ("file_missing", "source file is absent")
        if file.size_bytes is not None and source.stat().st_size != file.size_bytes:
            return "failed", None, ("size_mismatch", "source size changed")
        source_sha = sha256_file(source)
        if not file.sha256 or source_sha != file.sha256:
            return "failed", None, ("sha256_mismatch", "source hash changed")

        suffix = source.suffix.lower()
        if suffix in _DIRECT_TEXT:
            body = source.read_text(encoding="utf-8", errors="replace")
            parse_engine = "direct-text"
            parse_engine_version = "1"
            parse_quality_score = 1.0
            fallback_reasons: list[str] = []
        elif suffix in set(self._settings.parser.supported_extensions):
            attempts: list[dict[str, object]] = []
            body = ""
            parse_engine = ""
            parse_engine_version = ""
            parse_quality_score: float | None = None
            fallback_reasons = []
            engines = _parse_engines(
                suffix, mineru_enabled=self._settings.mineru.enabled
            )
            for parser_name in engines[:2]:
                parser_version = resolve_parser_version(parser_name, self._settings)
                config_sha = _json_sha256(
                    {
                        "engine": parser_name,
                        "parser": self._settings.parser.model_dump(mode="json"),
                        "mineru": self._settings.mineru.model_dump(mode="json"),
                        "profile": _PARSE_PROFILE,
                        "quality_policy": {
                            "minimum_effective_characters": _MIN_EFFECTIVE_CHARACTERS,
                            "minimum_quality_score": _MIN_QUALITY_SCORE,
                            "fragmented_line_median_max": 28,
                            "fragmented_short_line_ratio": 0.8,
                        },
                    }
                )
                parsed = self._parse_cache.get_or_parse(
                    ParseRequest(
                        file_id=file.id,
                        content_sha256=file.sha256,
                        parser_name=parser_name,
                        parser_version=parser_version,
                        parse_profile_version=_PARSE_PROFILE,
                        parse_config_sha256=config_sha,
                        metadata_unresolved=False,
                        purpose="candidate_markdown",
                    )
                )
                if parsed.status != "parsed" or not parsed.output_relpath:
                    reasons = (parsed.error_code or "parse_failed",)
                else:
                    product = resolve_cache_path(self._settings, parsed.output_relpath)
                    if not product.is_file():
                        reasons = ("parse_output_missing",)
                    else:
                        candidate = product.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        reasons = _parse_quality_reasons(
                            candidate, parsed.quality_score
                        )
                        if not reasons:
                            body = candidate
                            parse_engine = parsed.engine or parser_name
                            parse_engine_version = (
                                parsed.engine_version or parser_version
                            )
                            parse_quality_score = parsed.quality_score
                            break
                attempts.append(
                    {"engine": parser_name, "reasons": list(reasons)}
                )
                fallback_reasons.extend(reasons)
            if not body:
                return (
                    "failed",
                    None,
                    (
                        "parse_quality_failed",
                        json.dumps(attempts, ensure_ascii=False, sort_keys=True),
                    ),
                )
        else:
            return "skipped", None, ("unsupported_file_type", suffix or "no suffix")

        stem = sanitize_component(Path(file.original_name).stem, collision_key=source_sha)
        prefix = "正文" if file.file_role == "official_body" else f"附件{ordinal:02d}"
        filename = f"{prefix}_{stem}.md" if prefix != "正文" else "正文.md"
        metadata = yaml.safe_dump(
            {
                "source_sha256": source_sha,
                "conversion_profile": _PARSE_PROFILE,
                "parse_engine": parse_engine,
                "parse_engine_version": parse_engine_version,
                "parse_quality_score": parse_quality_score,
                "fallback_reasons": fallback_reasons,
                "source_file_id": file.id,
                "source_attachment_aliases": [
                    {
                        "file_id": alias.id,
                        "attachment_key": alias.attachment_key,
                        "original_name": alias.original_name,
                        "file_role": alias.file_role,
                        "local_relpath": alias.local_relpath,
                    }
                    for alias in attachment.aliases
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        )
        (package / filename).write_text(
            f"---\n{metadata}---\n\n{body.rstrip()}\n", encoding="utf-8"
        )
        return "converted", filename, None

    @staticmethod
    def _write_index(
        package: Path,
        item: OAItem,
        manifest: OAManifestItem,
        decision: ClassificationDecision,
        links: list[tuple[str, str]],
        exceptions: list[BackfillException],
    ) -> None:
        frontmatter = yaml.safe_dump(
            {
                "oa_item_key": item.oa_item_key,
                "title": item.title,
                "initiator": item.sender,
                "completed_at": item.completed_at.isoformat()
                if item.completed_at
                else None,
                "classification_status": decision.classification_status,
                "content_integrity_status": decision.content_integrity_status,
                "content_origin": decision.content_origin,
                "business_category": decision.business_category,
                "canonical_issuer": decision.canonical_issuer,
                "document_number": decision.document_number,
                "document_type": decision.document_type,
                "classification_confidence": decision.classification_confidence,
                "decision_source": decision.decision_source,
            },
            allow_unicode=True,
            sort_keys=False,
        )
        lines = [
            "---",
            frontmatter.rstrip(),
            "---",
            "",
            f"# {item.title}",
            "",
            "## 附件",
            "",
        ]
        if links:
            lines.extend(f"- [{label}](<{filename}>)" for filename, label in links)
        elif manifest.no_attachment_confirmed:
            lines.append("- 无附件（OA 清单已确认）。")
        else:
            lines.append("- 没有成功生成的附件 Markdown。")
        if exceptions:
            lines.extend(["", "## 候选构建异常", ""])
            lines.extend(f"- `{row.code}`：{row.detail}" for row in exceptions)
        (package / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _resolve_internal_outcome(
        self,
        item: ClassificationItem,
        evidence: list[Evidence],
        outcome: RuleOutcome,
    ) -> RuleOutcome:
        if (
            outcome.content_origin != "internal"
            or outcome.business_category is not None
            or outcome.classification_status != "needs_review"
        ):
            return outcome

        document_type = outcome.document_type or extract_document_type(item.title)
        resolved = classify_by_content(item.title, ())
        bodies: tuple[str, ...] = ()
        source_file_id: int | None = None
        if resolved is None and item.attachments:
            parsed_bodies: list[str] = []
            with self._sessions() as session:
                files = [
                    session.get(ArchivedFile, attachment.source_file_id)
                    for attachment in item.attachments
                    if attachment.source_file_id is not None
                ]
                detached = [file for file in files if file is not None]
                for file in detached:
                    session.expunge(file)
            with tempfile.TemporaryDirectory(prefix="oaradar-classification-v2-") as root:
                package = Path(root)
                for ordinal, attachment in enumerate(
                    canonicalize_attachment_aliases(detached), 1
                ):
                    file = attachment.file
                    status, filename, _problem = self._convert_attachment(
                        package, attachment, ordinal
                    )
                    if status != "converted" or filename is None:
                        continue
                    rendered = (package / filename).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    parts = rendered.split("---", 2)
                    body = parts[2].strip() if len(parts) == 3 else rendered.strip()
                    if body:
                        parsed_bodies.append(body)
                        source_file_id = source_file_id or file.id
            bodies = tuple(parsed_bodies)
            resolved = classify_by_content(item.title, bodies)

        if resolved is None and bodies:
            client = self._qwen_client
            if client is None:
                client = make_llm_client(
                    self._settings.llm,
                    max_retries=self._settings.llm.max_retries,
                    max_tokens=512,
                )
            classifier = LocalQwenInternalClassifier(
                client,
                confidence_threshold=self._settings.curation.confidence_threshold,
            )
            resolved = classifier.classify(item.title, bodies, document_type)
            self._qwen_outcomes[
                "accepted" if resolved is not None else classifier.last_rejection_code or "rejected"
            ] += 1

        if resolved is None:
            return replace(outcome, document_type=document_type)

        evidence.append(
            Evidence(
                evidence_scope="attachment" if bodies else "package",
                code=resolved.decision_source,
                priority=10,
                confidence=resolved.confidence,
                decision_source=resolved.decision_source,
                content_origin="internal",
                business_category=resolved.business_category,
                document_type=resolved.document_type or document_type,
                evidence_excerpt=resolved.evidence,
                source_file_id=source_file_id,
            )
        )
        return replace(
            outcome,
            classification_status="classified",
            business_category=resolved.business_category,
            document_type=resolved.document_type or document_type,
            decision_source=resolved.decision_source,
            confidence=resolved.confidence,
            escalation_action="resolved",
        )
