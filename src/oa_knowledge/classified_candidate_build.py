"""Read-only selection for OA Markdown candidate builds.

This module deliberately owns no classification behavior.  A candidate build
starts by freezing the IDs returned here and subsequently renders exactly
those immutable decision versions.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.backfill_mvp import (
    BackfillException,
    BackfillMVPService,
    canonicalize_attachment_aliases,
)
from oa_knowledge.config import Settings
from oa_knowledge.curation.canonical import sanitize_component
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    OAItem,
    OAManifestItem,
)
from oa_knowledge.parsers.format_router import detect_format
from oa_knowledge.runtime_paths import resolve_original_path
from oa_knowledge.source_roles import MARKDOWN_SOURCE_ROLES

_INTERNAL_CATEGORIES = frozenset(
    {
        "01_公司治理与决策",
        "02_业务项目与投放租后",
        "03_风险合规审计法务",
        "04_财务资金与融资",
        "05_经营计划与绩效考核",
        "06_人力资源",
        "07_党建纪检与工会",
        "08_行政采购与信息化",
        "09_对外报送与监管反馈",
        "99_其他内部",
    }
)


@dataclass(frozen=True, slots=True)
class FrozenCandidateDecision:
    oa_item_key: str
    decision_id: int


@dataclass(frozen=True, slots=True)
class ClassifiedCandidateBuildResult:
    output_root: Path
    target_total: int
    package_success: int
    package_partial: int
    package_failed: int
    package_relpaths: dict[str, str]


@dataclass(frozen=True, slots=True)
class ClassifiedCandidateBuildProgress:
    target_total: int
    queued: int
    package_success: int
    package_partial: int
    package_failed: int


@dataclass(frozen=True, slots=True)
class ClassifiedCandidateBuildQA:
    passed: bool
    index_count: int
    errors: tuple[str, ...]


def _has_unresolved_conflict(value: str) -> bool:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return True
    conflicts = payload.get("conflict_codes", ()) if isinstance(payload, dict) else ()
    return bool(conflicts)


def _publishable(row: ClassificationDecision) -> bool:
    if row.classification_status != "classified":
        return False
    if row.content_integrity_status not in {"ok", "no_attachment_confirmed"}:
        return False
    if _has_unresolved_conflict(row.classification_reason_json):
        return False
    if row.content_origin == "internal":
        return row.business_category in _INTERNAL_CATEGORIES and not row.canonical_issuer
    if row.content_origin == "external":
        return bool((row.canonical_issuer or "").strip()) and not row.business_category
    return False


def freeze_publishable_snapshot(session: Session) -> tuple[FrozenCandidateDecision, ...]:
    """Return a deterministic, read-only snapshot of publishable decisions."""
    rows = session.scalars(
        select(ClassificationDecision)
        .where(ClassificationDecision.is_current.is_(True))
        .order_by(ClassificationDecision.oa_item_key)
    )
    return tuple(
        FrozenCandidateDecision(row.oa_item_key, row.id)
        for row in rows
        if _publishable(row)
    )


def _package_relpath(item: OAItem, decision: ClassificationDecision) -> Path:
    completed = item.completed_at or item.initiated_at or item.received_at
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
    if decision.content_origin == "internal":
        parent = sanitize_component(decision.business_category or "")
        return Path("internal", parent, year, month, leaf)
    parent = sanitize_component(decision.canonical_issuer or "")
    return Path("external", parent, year, month, leaf)


def _originals_snapshot(root: Path) -> list[dict[str, object]]:
    if not root.exists():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "relpath": path.relative_to(root).as_posix(),
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
            }
        )
    return rows


def run_attachment_worker(
    command: list[str], *, timeout_seconds: int
) -> tuple[str, str | None, tuple[str, str] | None]:
    """Run exactly one attachment conversion in an independently killable process."""
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return "failed", None, ("attachment_worker_timeout", f"{timeout_seconds} seconds")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "worker exited without detail").strip()
        return "failed", None, ("attachment_worker_failed", detail[-2000:])
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        outcome = payload["outcome"]
        filename = payload.get("filename")
        problem = payload.get("problem")
        if outcome not in {"converted", "skipped", "failed"}:
            raise ValueError("invalid outcome")
        if filename is not None and not isinstance(filename, str):
            raise ValueError("invalid filename")
        if problem is not None and (
            not isinstance(problem, list)
            or len(problem) != 2
            or not all(isinstance(value, str) for value in problem)
        ):
            raise ValueError("invalid problem")
        return outcome, filename, tuple(problem) if problem is not None else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return "failed", None, ("attachment_worker_protocol_error", "invalid worker response")


class ClassifiedCandidateBuildService:
    """Render a candidate strictly from a read-only frozen decision snapshot."""

    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        *,
        config_path: Path = Path("config.yaml"),
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._config_path = config_path.resolve()

    def _builds_root(self) -> Path:
        return self._settings.data_root / "markdown" / ".builds"

    def _work_root(self, run_id: str) -> Path:
        return self._builds_root() / f".{run_id}.work"

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def _load_manifest(self, run_id: str) -> tuple[Path, dict[str, object]]:
        root = self._work_root(run_id)
        manifest_path = root / "build_manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"candidate build does not exist: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Convert the first pre-checkpoint manifest format without discarding
        # its already-frozen baseline.  Subsequent package checkpoints should
        # never rewrite a multi-megabyte originals inventory.
        legacy_baseline = manifest.pop("originals_baseline", None)
        if legacy_baseline is not None:
            self._write_json(root / "originals_baseline.json", legacy_baseline)
            manifest["originals_baseline_file"] = "originals_baseline.json"
            self._write_json(manifest_path, manifest)
        return root, manifest

    @staticmethod
    def _baseline(root: Path, manifest: dict[str, object]) -> object:
        filename = manifest.get("originals_baseline_file")
        if not isinstance(filename, str):
            raise TypeError("candidate build originals baseline is missing")
        path = root / filename
        if not path.is_file():
            raise RuntimeError("candidate build originals baseline file is missing")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _progress(manifest: dict[str, object]) -> ClassifiedCandidateBuildProgress:
        rows = manifest["items"]
        assert isinstance(rows, list)
        statuses = [row["status"] for row in rows if isinstance(row, dict)]
        return ClassifiedCandidateBuildProgress(
            target_total=len(statuses),
            queued=statuses.count("queued"),
            package_success=statuses.count("package_success"),
            package_partial=statuses.count("package_partial"),
            package_failed=statuses.count("package_failed"),
        )

    def start(self, run_id: str) -> ClassifiedCandidateBuildProgress:
        final_root = self._builds_root() / run_id
        work_root = self._work_root(run_id)
        if final_root.exists() or work_root.exists():
            raise ValueError(f"candidate build already exists: {run_id}")
        with self._sessions() as session:
            snapshot = freeze_publishable_snapshot(session)
            decision_count = session.scalar(select(func.count()).select_from(ClassificationDecision))
        work_root.mkdir(parents=True)
        baseline = _originals_snapshot(self._settings.data_root / "originals")
        self._write_json(work_root / "originals_baseline.json", baseline)
        manifest: dict[str, object] = {
            "run_id": run_id,
            "classification_decision_count_before": decision_count,
            "originals_baseline_file": "originals_baseline.json",
            "items": [
                {
                    "oa_item_key": row.oa_item_key,
                    "decision_id": row.decision_id,
                    "status": "queued",
                }
                for row in snapshot
            ],
        }
        self._write_json(work_root / "build_manifest.json", manifest)
        return self._progress(manifest)

    def _render_item(self, root: Path, row: dict[str, object]) -> tuple[str, str | None]:
        key = str(row["oa_item_key"])
        decision_id = int(row["decision_id"])
        with self._sessions() as session:
            decision = session.get(ClassificationDecision, decision_id)
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == key))
            manifest = session.scalar(
                select(OAManifestItem).where(OAManifestItem.oa_item_key == key)
            )
            if decision is None or item is None or manifest is None:
                raise RuntimeError(f"frozen candidate input missing: {key}")
            relpath = _package_relpath(item, decision)
            packages = root / "packages"
            packages.mkdir(exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=".package.", dir=packages))
            try:
                source_files = list(
                    session.scalars(
                        select(ArchivedFile)
                        .where(
                            ArchivedFile.oa_item_id == item.id,
                            ArchivedFile.file_role.in_(MARKDOWN_SOURCE_ROLES),
                        )
                        .order_by(ArchivedFile.id)
                    )
                )
                links: list[tuple[str, str]] = []
                exceptions: list[BackfillException] = []
                attachment_details: list[dict[str, object]] = []
                for ordinal, attachment in enumerate(
                    canonicalize_attachment_aliases(source_files), 1
                ):
                    outcome, filename, problem = run_attachment_worker(
                        [
                            sys.executable,
                            "-m",
                            "oa_knowledge.candidate_attachment_worker",
                            "--config",
                            str(self._config_path),
                            "--file-id",
                            str(attachment.file.id),
                            "--alias-ids",
                            json.dumps([value.id for value in attachment.aliases]),
                            "--package",
                            str(temporary),
                            "--ordinal",
                            str(ordinal),
                        ],
                        timeout_seconds=120,
                    )
                    if outcome == "converted" and filename:
                        links.append((filename, attachment.file.original_name))
                    elif problem is not None:
                        actual_file_type: str | None = None
                        if attachment.file.local_relpath:
                            try:
                                actual_file_type = detect_format(
                                    resolve_original_path(
                                        self._settings, attachment.file.local_relpath
                                    )
                                ).actual_file_type
                            except (OSError, ValueError):
                                pass
                        exceptions.append(
                            BackfillException(key, attachment.file.id, problem[0], problem[1])
                        )
                        attachment_details.append(
                            {
                                "file_id": attachment.file.id,
                                "original_name": attachment.file.original_name,
                                "local_relpath": attachment.file.local_relpath,
                                "sha256": attachment.file.sha256,
                                "actual_file_type": actual_file_type,
                                "conversion_status": outcome,
                                "code": problem[0],
                                "detail": problem[1],
                            }
                        )
                if not source_files and not manifest.no_attachment_confirmed:
                    exceptions.append(
                        BackfillException(
                            key,
                            None,
                            "attachment_inventory_empty",
                            "no eligible source attachment was recorded",
                        )
                    )
                BackfillMVPService._write_index(
                    temporary, item, manifest, decision, links, exceptions
                )
                destination = packages / relpath
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, destination)
                row["package_relpath"] = relpath.as_posix()
                row["exceptions"] = [
                    *attachment_details,
                    *(
                        {
                            "file_id": value.file_id,
                            "code": value.code,
                            "detail": value.detail,
                        }
                        for value in exceptions
                        if value.file_id is None
                    ),
                ]
                return ("package_partial" if exceptions else "package_success"), None
            except Exception as exc:  # noqa: BLE001 - package failures must be durable
                return "package_failed", f"{type(exc).__name__}: {exc}"
            finally:
                if temporary.exists():
                    for path in sorted(temporary.rglob("*"), reverse=True):
                        if path.is_file():
                            path.unlink()
                        elif path.is_dir():
                            path.rmdir()
                    temporary.rmdir()

    def process(self, run_id: str, *, limit: int = 25) -> ClassifiedCandidateBuildProgress:
        if limit < 1:
            raise ValueError("limit must be positive")
        root, manifest = self._load_manifest(run_id)
        rows = manifest["items"]
        assert isinstance(rows, list)
        processed = 0
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != "queued":
                continue
            status, error = self._render_item(root, row)
            row["status"] = status
            if error:
                row["error"] = error
            self._write_json(root / "build_manifest.json", manifest)
            processed += 1
            if processed >= limit:
                break
        return self._progress(manifest)

    def finalize(self, run_id: str) -> ClassifiedCandidateBuildResult:
        root, manifest = self._load_manifest(run_id)
        progress = self._progress(manifest)
        if progress.queued:
            raise ValueError("candidate build still has queued packages")
        baseline = self._baseline(root, manifest)
        if baseline != _originals_snapshot(self._settings.data_root / "originals"):
            raise RuntimeError("originals changed during candidate build")
        final_root = self._builds_root() / run_id
        if final_root.exists():
            raise ValueError(f"candidate build already exists: {run_id}")
        os.replace(root, final_root)
        with (final_root / "exceptions.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=(
                    "oa_item_key",
                    "package_status",
                    "file_id",
                    "original_name",
                    "local_relpath",
                    "sha256",
                    "actual_file_type",
                    "conversion_status",
                    "code",
                    "detail",
                    "error",
                ),
            )
            writer.writeheader()
            for row in manifest["items"]:
                if not isinstance(row, dict):
                    continue
                for exception in row.get("exceptions", []):
                    if not isinstance(exception, dict):
                        continue
                    writer.writerow(
                        {
                            "oa_item_key": row.get("oa_item_key"),
                            "package_status": row.get("status"),
                            **exception,
                            "error": row.get("error"),
                        }
                    )
                if row.get("status") == "package_failed":
                    writer.writerow(
                        {
                            "oa_item_key": row.get("oa_item_key"),
                            "package_status": row.get("status"),
                            "error": row.get("error"),
                        }
                    )
        relpaths = {
            str(row["oa_item_key"]): str(row["package_relpath"])
            for row in manifest["items"]
            if isinstance(row, dict) and row.get("package_relpath")
        }
        return ClassifiedCandidateBuildResult(
            output_root=final_root,
            target_total=progress.target_total,
            package_success=progress.package_success,
            package_partial=progress.package_partial,
            package_failed=progress.package_failed,
            package_relpaths=relpaths,
        )

    def validate(self, run_id: str) -> ClassifiedCandidateBuildQA:
        root = self._builds_root() / run_id
        manifest_path = root / "build_manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"completed candidate build does not exist: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = manifest.get("items", [])
        errors: list[str] = []
        if not isinstance(rows, list):
            errors.append("manifest_items_invalid")
            rows = []
        statuses = [row.get("status") for row in rows if isinstance(row, dict)]
        target_total = len(rows)
        success = statuses.count("package_success")
        partial = statuses.count("package_partial")
        failed = statuses.count("package_failed")
        if target_total != success + partial + failed:
            errors.append("target_status_equation_failed")
        packages = root / "packages"
        indexes = sorted(packages.rglob("_index.md")) if packages.is_dir() else []
        if len(indexes) != success + partial:
            errors.append("index_count_equation_failed")
        observed_relpaths: set[str] = set()
        observed_keys: set[str] = set()
        for index in indexes:
            relpath = index.parent.relative_to(packages).as_posix()
            if relpath in observed_relpaths:
                errors.append(f"duplicate_package_path:{relpath}")
            observed_relpaths.add(relpath)
            body = index.read_text(encoding="utf-8")
            match = re.match(r"\A---\n(.*?)\n---\n", body, flags=re.DOTALL)
            if match is None:
                errors.append(f"frontmatter_missing:{relpath}")
                continue
            try:
                frontmatter = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                errors.append(f"frontmatter_invalid:{relpath}")
                continue
            key = frontmatter.get("oa_item_key") if isinstance(frontmatter, dict) else None
            if not isinstance(key, str) or not key:
                errors.append(f"oa_item_key_missing:{relpath}")
            elif key in observed_keys:
                errors.append(f"duplicate_oa_item_key:{key}")
            else:
                observed_keys.add(key)
            if not isinstance(frontmatter, dict) or frontmatter.get("classification_status") != "classified":
                errors.append(f"nonclassified_package:{relpath}")
            for linked in re.findall(r"\]\(<([^>]+)>\)", body):
                target = (index.parent / linked).resolve()
                if not target.is_file() or index.parent.resolve() not in target.parents:
                    errors.append(f"broken_or_unsafe_link:{relpath}:{linked}")
        expected_keys = {
            str(row["oa_item_key"])
            for row in rows
            if isinstance(row, dict) and row.get("status") in {"package_success", "package_partial"}
        }
        if observed_keys != expected_keys:
            errors.append("package_key_mapping_failed")
        if any("needs_review" in path.parts for path in packages.rglob("*") if packages.exists()):
            errors.append("needs_review_package_present")
        baseline_path = root / str(manifest.get("originals_baseline_file", ""))
        baseline = (
            json.loads(baseline_path.read_text(encoding="utf-8"))
            if baseline_path.is_file()
            else None
        )
        if baseline != _originals_snapshot(self._settings.data_root / "originals"):
            errors.append("originals_changed")
        with self._sessions() as session:
            decision_count = session.scalar(select(func.count()).select_from(ClassificationDecision))
        if decision_count != manifest.get("classification_decision_count_before"):
            errors.append("classification_decision_count_changed")
        report = {
            "run_id": run_id,
            "passed": not errors,
            "target_total": target_total,
            "package_success": success,
            "package_partial": partial,
            "package_failed": failed,
            "index_count": len(indexes),
            "errors": errors,
        }
        self._write_json(root / "qa_report.json", report)
        return ClassifiedCandidateBuildQA(not errors, len(indexes), tuple(errors))

    def build(self, run_id: str) -> ClassifiedCandidateBuildResult:
        self.start(run_id)
        while self.process(run_id, limit=25).queued:
            pass
        return self.finalize(run_id)
