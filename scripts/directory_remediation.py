"""Bounded, local-only directory remediation for existing Markdown exports.

It never accesses OA, downloads originals, or runs classification models.  The
two commands are intentionally separate: normalize only writes versioned
decisions with an already-valid normalized issuer; map is read-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.classification.metadata_rules import (
    resolve_configured_document_issuer,
    resolve_issuer_from_text,
)
from oa_knowledge.classification.per_item_classifier import normalize_canonical_issuer
from oa_knowledge.classification.private_config import (
    load_private_classification_config,
)
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    ClassificationEvidence,
    ClassificationRun,
    MarkdownExport,
    OAItem,
)
from oa_knowledge.markdown_delivery import (
    _extract_current_document_number,
    _item_leaf,
    _normalize_document_number,
    _package_directory,
)

_ISSUER_POLLUTION = (
    "以此为准", "正文", "盖章", "明电", "implied by content", "文件处理表", "文件分送表", "会议听取", "传达学习",
)
_GENERIC_ISSUERS = frozenset({
    "人民政府办公室", "财政局", "总工会", "市政府办公厅", "市委金融办", "区财政局", "区总工会",
})
_INTERNAL_STYLE_ISSUER = re.compile(r"(?:风险控制中心|(?:^|[、，])(?:[\u4e00-\u9fff]{2,12})(?:部|中心))$")
_NUMBERED_OR_SUBJECT = re.compile(r"^(?:[（(]?[一二三四五六七八九十\d]+[）).、]?|支持|关于)")
_TRUNCATED_ISSUER = re.compile(r"^(?:产监督管理局|化广电旅游局|团有限公司)$")
_CATEGORY_HINTS = (
    ("01_公司治理与决策", ("董事会", "公司章程", "股东", "监事会")),
    ("02_业务项目与投放租后", ("项目立项", "租后", "租金", "售后回租", "直接租赁")),
    ("03_风险合规审计法务", ("审计", "法律", "法务", "风险", "合规")),
    ("04_财务资金与融资", ("银行", "账户", "资金", "预算", "授信")),
    ("05_经营计划与绩效考核", ("经营计划", "工作简报", "绩效", "经营分析")),
    ("06_人力资源", ("人事", "薪酬", "招聘", "员工", "岗位")),
    ("07_党建纪检与工会", ("党建", "纪检", "工会", "党委", "党支部")),
    ("08_行政采购与信息化", ("采购", "软件", "信息系统", "信息化")),
    ("09_对外报送与监管反馈", ("报送", "监管反馈", "监管报送", "回复监管")),
)


def _run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"


def _copy_decision(session: Session, current: ClassificationDecision, issuer: str, run: ClassificationRun) -> int:
    version = session.scalar(select(func.max(ClassificationDecision.version)).where(
        ClassificationDecision.oa_item_key == current.oa_item_key
    )) or 0
    reason = json.loads(current.classification_reason_json or "{}")
    reason["issuer_canonicalization"] = {
        "from": current.canonical_issuer,
        "to": issuer,
        "source": "existing_current_decision",
    }
    clone = ClassificationDecision(
        classification_run_id=run.id, oa_item_key=current.oa_item_key, version=version + 1,
        is_current=True, decision_input_sha256=current.decision_input_sha256,
        decision_source=current.decision_source, classification_status=current.classification_status,
        content_integrity_status=current.content_integrity_status, content_origin=current.content_origin,
        flow_type=current.flow_type, initiator=current.initiator, initiator_type=current.initiator_type,
        relay_from=current.relay_from, transfer_chain_json=current.transfer_chain_json,
        issuer=current.issuer, canonical_issuer=issuer, business_category=current.business_category,
        document_number=current.document_number, document_type=current.document_type,
        normalized_title=current.normalized_title, classification_confidence=current.classification_confidence,
        classification_reason_json=json.dumps(reason, ensure_ascii=False, sort_keys=True),
        rule_version=current.rule_version, private_config_sha256=current.private_config_sha256,
        manual_locked=current.manual_locked, actor=current.actor, supersedes_decision_id=current.id,
    )
    current.is_current = False
    session.add(clone)
    session.flush()
    for evidence in session.scalars(select(ClassificationEvidence).where(
        ClassificationEvidence.classification_decision_id == current.id
    )).all():
        session.add(ClassificationEvidence(
            classification_decision_id=clone.id, sequence=evidence.sequence,
            evidence_type=evidence.evidence_type, evidence_scope=evidence.evidence_scope,
            value_json=evidence.value_json, confidence=evidence.confidence,
            source_file_id=evidence.source_file_id, parse_artifact_id=evidence.parse_artifact_id,
        ))
    return clone.id


def normalize(settings, *, output: Path, apply: bool) -> dict[str, int]:
    loaded = load_private_classification_config(
        settings.classification_private_dir or Path("private/classification")
    )
    config = loaded.config
    engine = create_db_engine(settings.database_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = {"scanned": 0, "unchanged": 0, "normalized": 0, "invalid_requires_review": 0}
    changes: list[dict[str, str]] = []
    with Session(engine) as session:
        rows = session.scalars(select(ClassificationDecision).where(
            ClassificationDecision.is_current.is_(True),
            ClassificationDecision.classification_status == "classified",
            ClassificationDecision.content_origin == "external",
        ).order_by(ClassificationDecision.oa_item_key)).all()
        run = None
        if apply:
            run = ClassificationRun(
                run_id=_run_id("issuer-normalization"), run_kind="incremental", status="running",
                input_signature=hashlib.sha256(b"issuer-normalization").hexdigest(),
                manifest_sha256="0" * 64, exclusion_policy_sha256="0" * 64,
                rule_version="issuer-normalization-v1", schema_version="v1", prompt_version="none",
                model_name="none", private_config_sha256=loaded.config_sha256,
            )
            session.add(run); session.flush()
        for row in rows:
            counts["scanned"] += 1
            normalized = normalize_canonical_issuer(row.canonical_issuer or row.issuer, config)
            if normalized is None:
                counts["invalid_requires_review"] += 1
                changes.append({"oa_item_key": row.oa_item_key, "status": "invalid_requires_review", "old": row.canonical_issuer or "", "new": ""})
                continue
            if normalized == row.canonical_issuer:
                counts["unchanged"] += 1; continue
            counts["normalized"] += 1
            changes.append({"oa_item_key": row.oa_item_key, "status": "normalized", "old": row.canonical_issuer or "", "new": normalized})
            if run is not None:
                _copy_decision(session, row, normalized, run)
        if run is not None:
            run.target_count = counts["normalized"]
            run.status = "completed"; run.finished_at = datetime.now(UTC)
            session.commit()
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["oa_item_key", "status", "old", "new"])
        writer.writeheader(); writer.writerows(changes)
    return counts


def _issuer_status(value: str | None, config) -> tuple[str, str | None]:
    if not value or any(token.casefold() in value.casefold() for token in _ISSUER_POLLUTION):
        return "invalid", None
    normalized = resolve_issuer_from_text(value, (), config) or normalize_canonical_issuer(value, config)
    if normalized is None:
        return "invalid", None
    if _NUMBERED_OR_SUBJECT.match(normalized) or _TRUNCATED_ISSUER.fullmatch(normalized):
        return "invalid", None
    if normalized in _GENERIC_ISSUERS or _INTERNAL_STYLE_ISSUER.search(normalized):
        return "invalid", None
    return "valid", normalized


def _document_status(item: OAItem, decision: ClassificationDecision, config) -> tuple[str, str | None, str]:
    title = _extract_current_document_number(item.title or "")
    stored = _normalize_document_number(decision.document_number)
    if title is None:
        return ("candidate" if stored else "missing", None, "title_has_no_confirmed_primary_document_number")
    if stored and stored != title.normalized:
        return "candidate", None, "decision_document_number_differs_from_title"
    configured = resolve_configured_document_issuer(title.normalized, config)
    if configured is not None and configured[1] != decision.canonical_issuer:
        return "candidate", None, "issuer_mismatch"
    return "confirmed", title.normalized, ""


def _category_warning(title: str, category: str | None) -> str:
    hits = [expected for expected, words in _CATEGORY_HINTS if any(word in title for word in words)]
    return "" if not hits or category in hits else f"title_signals_{hits[0]}"


def migration_map(settings, *, output: Path) -> dict[str, int]:
    loaded = load_private_classification_config(settings.classification_private_dir or Path("private/classification"))
    config = loaded.config
    engine = create_db_engine(settings.database_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    counts = {"ready": 0, "hold_invalid_issuer": 0, "hold_category_warning": 0, "hold_path_conflict": 0, "confirmed": 0, "candidate": 0, "missing": 0, "issuer_mismatch": 0}
    targets: set[str] = set()
    with Session(engine) as session:
        items = session.execute(select(OAItem, ClassificationDecision).join(
            ClassificationDecision, ClassificationDecision.oa_item_key == OAItem.oa_item_key
        ).where(
            ClassificationDecision.is_current.is_(True),
            ClassificationDecision.classification_status == "classified",
        ).order_by(OAItem.oa_item_key)).all()
        exports_by_item: dict[int, list[MarkdownExport]] = {}
        for export in session.scalars(select(MarkdownExport)).all():
            if export.oa_item_id is not None:
                exports_by_item.setdefault(export.oa_item_id, []).append(export)
        for item, decision in items:
            exports = exports_by_item.get(item.id, [])
            old = sorted({
                str((settings.markdown_root / export.markdown_relpath).relative_to(settings.workspace_root).parent)
                if Path(export.markdown_relpath).parts[:2] != ("data", "markdown")
                else str(Path(export.markdown_relpath).parent)
                for export in exports
            })
            issuer_status, issuer = _issuer_status(decision.canonical_issuer, config) if decision.content_origin == "external" else ("not_applicable", None)
            number_status, number, number_warning = _document_status(item, decision, config)
            counts[number_status] += 1
            if number_warning == "issuer_mismatch":
                counts["issuer_mismatch"] += 1
            warning = _category_warning(item.title or "", decision.business_category) if decision.content_origin == "internal" else ""
            status = "ready"
            reason = ""
            if decision.content_origin == "external" and issuer_status != "valid":
                status, reason = "hold_invalid_issuer", "current_decision_canonical_issuer_failed_validator"
            elif warning:
                status, reason = "hold_category_warning", warning
            display = SimpleNamespace(
                oa_item_key=item.oa_item_key, title=item.title, source_type=decision.content_origin,
                document_number=number, external_issuer=issuer,
            )
            path_decision = SimpleNamespace(
                content_origin=decision.content_origin, business_category=decision.business_category,
                canonical_issuer=issuer,
            )
            target = settings.markdown_root.relative_to(settings.workspace_root) / _package_directory(item, path_decision) / _item_leaf(display)
            target_text = target.as_posix()
            if status == "ready" and target_text in targets:
                status, reason = "hold_path_conflict", "ready_target_collision"
            if status == "ready":
                targets.add(target_text); counts["ready"] += 1
            else:
                counts[status] += 1
                target_text = ""
            rows.append({
                "oa_item_key": item.oa_item_key, "decision_id": str(decision.id),
                "old_paths": json.dumps(old, ensure_ascii=False), "new_path": target_text,
                "canonical_issuer": issuer or decision.canonical_issuer or "", "canonical_issuer_status": issuer_status,
                "doc_number": number or "", "doc_number_status": number_status,
                "doc_number_warning": number_warning, "category": decision.business_category or "",
                "category_sanity_warning": warning, "migration_status": status, "reason": reason,
            })
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["oa_item_key", "decision_id", "old_paths", "new_path", "canonical_issuer", "canonical_issuer_status", "doc_number", "doc_number_status", "doc_number_warning", "category", "category_sanity_warning", "migration_status", "reason"])
        writer.writeheader(); writer.writerows(rows)
    return counts


def write_ready_sample(input_path: Path, output: Path) -> None:
    with input_path.open(encoding="utf-8-sig", newline="") as stream:
        ready = [row for row in csv.DictReader(stream) if row["migration_status"] == "ready"]
    selected: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(predicate, limit: int = 1) -> None:
        for row in ready:
            if row["oa_item_key"] in seen or not predicate(row):
                continue
            selected.append(row); seen.add(row["oa_item_key"])
            if sum(predicate(value) for value in selected) >= limit:
                return

    for category, _ in _CATEGORY_HINTS:
        add(lambda row, value=category: row["category"] == value)
    add(lambda row: "、" in row["canonical_issuer"], 2)
    add(lambda row: "__issuer_" in row["new_path"], 2)
    for status in ("confirmed", "candidate", "missing"):
        add(lambda row, value=status: row["doc_number_status"] == value, 4)
    add(lambda row: "转发" in row["new_path"], 3)
    for row in ready:
        if len(selected) >= 30:
            break
        if row["oa_item_key"] not in seen:
            selected.append(row); seen.add(row["oa_item_key"])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(ready[0]))
        writer.writeheader(); writer.writerows(selected)


def _package_fingerprint(path: Path) -> dict[str, object]:
    index = path / "_index.md"
    index_text = index.read_text(encoding="utf-8", errors="replace") if index.is_file() else ""
    key = re.search(r'^oa_item_key:\s*"?([^"\n]+)', index_text, re.MULTILINE)
    attachment_rows = []
    assets = []
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file == index:
            continue
        digest = sha256_file(file)
        if file.suffix.lower() == ".md":
            text = file.read_text(encoding="utf-8", errors="replace")
            source_id = re.search(r'^source_file_id:\s*(\d+)', text, re.MULTILINE)
            source_sha = re.search(r'^source_sha256:\s*"?([0-9a-f]{64})', text, re.MULTILINE)
            attachment_rows.append((source_id.group(1) if source_id else file.name, source_sha.group(1) if source_sha else "", digest))
        else:
            assets.append((file.relative_to(path).as_posix(), digest))
    return {"oa_item_key": key.group(1) if key else "", "attachments": sorted(attachment_rows), "assets": sorted(assets), "index_sha256": sha256_file(index) if index.is_file() else ""}


def _source_resolution(paths: list[Path], key: str) -> tuple[str, Path | None, dict[str, object]]:
    existing = [path for path in paths if path.is_dir()]
    if len(existing) == 1:
        return "single_existing", existing[0], {"paths": [existing[0].as_posix()]}
    if not existing:
        return "conflict", None, {"reason": "no_old_package_exists"}
    fingerprints = {path: _package_fingerprint(path) for path in existing}
    detail_rows = [{"path": path.as_posix(), **value} for path, value in fingerprints.items()]
    if any(value["oa_item_key"] not in {"", key} for value in fingerprints.values()):
        return "conflict", None, {"reason": "index_oa_item_key_mismatch", "fingerprints": detail_rows}
    cores = {(tuple(value["attachments"]), tuple(value["assets"])) for value in fingerprints.values()}
    if len(cores) == 1:
        authoritative = max(existing, key=lambda path: (len(fingerprints[path]["attachments"]), len(fingerprints[path]["assets"])))
        return "duplicate_equivalent", authoritative, {"fingerprints": detail_rows}
    attachment_sets = {path: set(value["attachments"]) | set(value["assets"]) for path, value in fingerprints.items()}
    winners = [path for path, values in attachment_sets.items() if all(other <= values for other in attachment_sets.values())]
    if (
        len(winners) == 1
        and (winners[0] / "_index.md").is_file()
        and all(attachment_sets[other] < attachment_sets[winners[0]] for other in attachment_sets if other != winners[0])
    ):
        return "superset", winners[0], {"fingerprints": detail_rows}
    return "conflict", None, {"reason": "core_content_conflict", "fingerprints": detail_rows}


def preflight_sources(settings, *, input_path: Path, output: Path, journal_path: Path) -> dict[str, int]:
    with input_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    done = set()
    if journal_path.is_file():
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if payload.get("status") == "migrated":
                done.add(payload["oa_item_key"])
    counts = {"migrated": 0, "single_existing": 0, "duplicate_equivalent": 0, "superset": 0, "hold_multi_source_conflict": 0}
    engine = create_db_engine(settings.database_path)
    allowed_files: dict[str, set[str]] = {}
    with Session(engine) as session:
        for key, file_id in session.execute(select(OAItem.oa_item_key, ArchivedFile.id).join(ArchivedFile, ArchivedFile.oa_item_id == OAItem.id)):
            allowed_files.setdefault(key, set()).add(str(file_id))
    for row in rows:
        if row["migration_status"] != "ready":
            row["source_resolution"] = "not_applicable"; row["source_path"] = ""; continue
        if row["oa_item_key"] in done:
            row["source_resolution"] = "migrated"; row["source_path"] = ""; row["migration_status"] = "migrated"; counts["migrated"] += 1; continue
        old = [settings.markdown_root / value for value in json.loads(row["old_paths"])]
        resolution, source, detail = _source_resolution(old, row["oa_item_key"])
        fingerprints = detail.get("fingerprints", []) if isinstance(detail, dict) else []
        observed_ids = {
            identity for fingerprint in fingerprints for identity, _source_sha, _markdown_sha in fingerprint.get("attachments", [])
            if identity.isdigit()
        }
        if observed_ids and not observed_ids <= allowed_files.get(row["oa_item_key"], set()):
            resolution, source = "conflict", None
            detail = {"reason": "attachment_file_id_not_owned_by_oa", "observed": sorted(observed_ids), "allowed": sorted(allowed_files.get(row["oa_item_key"], set()))}
        row["source_resolution"] = resolution
        row["source_path"] = source.relative_to(settings.markdown_root).as_posix() if source else ""
        row["source_detail"] = json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
        if resolution == "conflict":
            row["migration_status"] = "hold_multi_source_conflict"; row["new_path"] = ""; row["reason"] = "multi_source_core_content_conflict"; counts["hold_multi_source_conflict"] += 1
        else:
            counts[resolution] += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) + ["source_resolution", "source_path", "source_detail"]
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return counts


def _rewrite_index(path: Path, row: dict[str, str]) -> str:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError("index_frontmatter_missing")
    head, body = content.split("---\n", 2)[1:]
    values = {
        "canonical_issuer": row["canonical_issuer"] or None,
        "internal_category": row["category"] or None,
        "markdown_package_path": row["new_path"],
    }
    lines = head.splitlines()
    present = set()
    for index, line in enumerate(lines):
        key = line.partition(":")[0]
        if key in values:
            lines[index] = f"{key}: {json.dumps(values[key], ensure_ascii=False)}"
            present.add(key)
    lines.extend(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in values.items() if key not in present)
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def migrate(settings, *, input_path: Path, journal_path: Path) -> dict[str, int]:
    with input_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row["migration_status"] == "ready"]
    engine = create_db_engine(settings.database_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    migrated = 0
    with Session(engine) as session, journal_path.open("a", encoding="utf-8") as journal:
        for row in rows:
            old_paths = json.loads(row["old_paths"])
            source_path = row.get("source_path") or (old_paths[0] if len(old_paths) == 1 else "")
            if not source_path:
                raise RuntimeError(f"source_resolution_missing:{row['oa_item_key']}")
            src = settings.markdown_root / source_path
            dst = settings.markdown_root / row["new_path"]
            result = {"oa_item_key": row["oa_item_key"], "old_path": source_path, "new_path": row["new_path"], "status": "failed"}
            try:
                if not src.is_dir() or dst.exists() or any(path.is_symlink() for path in src.rglob("*")):
                    raise RuntimeError("source_missing_destination_exists_or_symlink")
                item = session.scalar(select(OAItem).where(OAItem.oa_item_key == row["oa_item_key"]))
                exports = session.scalars(select(MarkdownExport).where(MarkdownExport.oa_item_id == item.id)).all() if item else []
                if item is None or not exports:
                    raise RuntimeError("missing_item_or_exports")
                attachment_hashes = {path.relative_to(src).as_posix(): sha256_file(path) for path in src.rglob("*") if path.is_file() and path.name != "_index.md"}
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
                try:
                    for export in exports:
                        old = Path(export.markdown_relpath)
                        export.markdown_relpath = (Path(row["new_path"]) / old.name).as_posix()
                        if export.assets_relpath:
                            export.assets_relpath = (Path(row["new_path"]) / Path(export.assets_relpath).name).as_posix()
                    content = _rewrite_index(dst / "_index.md", row)
                    (dst / "_index.md").write_text(content, encoding="utf-8")
                    index = next(export for export in exports if export.document_kind == "item_index")
                    index.markdown_sha256 = sha256_file(dst / "_index.md")
                    after = {path.relative_to(dst).as_posix(): sha256_file(path) for path in dst.rglob("*") if path.is_file() and path.name != "_index.md"}
                    if attachment_hashes != after:
                        raise RuntimeError("attachment_hash_changed")
                    session.commit()
                except BaseException:
                    session.rollback()
                    if dst.exists() and not src.exists():
                        os.replace(dst, src)
                    raise
                result["status"] = "migrated"; migrated += 1
            except BaseException as exc:
                result["error"] = str(exc)
                journal.write(json.dumps(result, ensure_ascii=False) + "\n"); journal.flush(); os.fsync(journal.fileno())
                raise
            journal.write(json.dumps(result, ensure_ascii=False) + "\n"); journal.flush(); os.fsync(journal.fileno())
    return {"ready_expected": len(rows), "ready_migrated": migrated}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("normalize", "map", "preflight", "migrate"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-output", type=Path)
    args = parser.parse_args()
    settings = load_settings(Path("config.yaml"))
    if args.command == "normalize":
        result = normalize(settings, output=args.output, apply=args.apply)
    elif args.command == "map":
        result = migration_map(settings, output=args.output)
    elif args.command == "preflight":
        result = preflight_sources(
            settings, input_path=Path("data/runs/directory-remediation-20260909/migration-map-v2.1.csv"),
            output=args.output,
            journal_path=Path("data/runs/directory-remediation-20260909/migration-journal-v2.1.jsonl"),
        )
    else:
        result = migrate(settings, input_path=args.output, journal_path=args.output.with_name("migration-journal-v2.1.jsonl"))
    if args.sample_output is not None and args.command == "map":
        write_ready_sample(args.output, args.sample_output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
