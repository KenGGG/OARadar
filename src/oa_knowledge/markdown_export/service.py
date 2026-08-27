from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import ArchivedFile, MarkdownExport, OAItem
from oa_knowledge.markdown_export.paths import markdown_path_for_source
from oa_knowledge.markdown_export.publisher import IMAGE_LINK, publish_markdown
from oa_knowledge.markdown_export.render import (
    SCHEMA_VERSION,
    ExportMetadata,
    render_markdown,
)
from oa_knowledge.parsers.format_router import detect_format, parser_attempts
from oa_knowledge.parsers.router import parse_file

INVALID_INLINE_IMAGE = re.compile(r"!\[[^]]*\]\(data:image/[^;)]+;base64\.\.\.\)", re.IGNORECASE)

def sanitize_parser_markdown(content: str) -> str:
    return INVALID_INLINE_IMAGE.sub("[图片未嵌入：源文档包含转换器无法提取的图片]", content)

def terminal_source_error(exc: Exception) -> str | None:
    value = str(exc).strip().lower()
    return {"corrupted_file": "CORRUPTED_FILE", "encrypted_document": "ENCRYPTED_DOCUMENT"}.get(value)

def rewrite_parser_asset_links(content: str, destination: Path, assets_dir: Path) -> str:
    prefix = destination.name.removesuffix(".md") + ".assets"
    def replace(match: re.Match[str]) -> str:
        link = match.group(1)
        if link.startswith("data:"):
            return match.group(0)
        if not (assets_dir / link).is_file():
            return "[图片未包含在解析结果中]"
        return match.group(0).replace(f"({link})", f"({prefix}/{link})")
    return IMAGE_LINK.sub(replace, content)


@dataclass
class ConversionSummary:
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    unsupported: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def needs_conversion(
    previous: Mapping[str, object] | None,
    source_sha256: str,
    parse_engine: str,
    parse_engine_version: str,
    parse_config_hash: str,
    schema_version: str,
    *,
    force: bool = False,
) -> bool:
    if force or previous is None or previous.get("status") not in {"success", "unsupported"}:
        return True
    expected = (source_sha256, parse_engine, parse_engine_version, parse_config_hash, schema_version)
    actual = tuple(previous.get(key) for key in ("source_sha256", "parse_engine", "parse_engine_version", "parse_config_hash", "schema_version"))
    return actual != expected


def _config_hash(settings: Settings, engine: str) -> str:
    payload = {"engine": engine, "parser": settings.parser.model_dump(mode="json"), "mineru": settings.mineru.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _engine_hint(source: Path, settings: Settings) -> tuple[str, str]:
    decision = detect_format(source)
    if decision.is_direct_text:
        return "direct-text", "1"
    attempts = parser_attempts(decision, mineru_enabled=settings.mineru.enabled)
    if not attempts:
        return "none", "1"
    return attempts[0], "format-router-v3.2"


def _file_context(session: Session, settings: Settings, source: Path) -> tuple[ArchivedFile | None, OAItem | None]:
    relative_data = source.relative_to(settings.data_root).as_posix()
    relative_archive = source.relative_to(settings.archive_root).as_posix()
    file = session.scalar(select(ArchivedFile).where(ArchivedFile.local_relpath.in_((relative_data, relative_archive))))
    return (file, session.get(OAItem, file.oa_item_id) if file else None)


def convert_archive(settings: Settings, *, item: str | None = None, force: bool = False, rebuild: bool = False) -> dict[str, object]:
    settings.archive_root.mkdir(parents=True, exist_ok=True)
    settings.markdown_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "wiki").mkdir(parents=True, exist_ok=True)
    summary = ConversionSummary()
    sources = sorted(path for path in settings.archive_root.rglob("*") if path.is_file() and not any(part.startswith(".") for part in path.relative_to(settings.archive_root).parts))
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            for source in sources:
                relative = source.relative_to(settings.archive_root).as_posix()
                file, oa_item = _file_context(session, settings, source)
                if item and (not oa_item or item not in {oa_item.oa_item_key, oa_item.workitem_id_text}):
                    continue
                summary.total += 1
                _convert_one(session, settings, source, relative, file, oa_item, summary, force or rebuild or settings.conversion.force_rebuild)
                session.commit()
    finally:
        engine.dispose()
    return {**summary.as_dict(), "markdown_dir": str(settings.markdown_root), "generated_at": datetime.now(UTC).isoformat()}

def convert_file_id(session: Session, settings: Settings, file_id: int, *, engine: str | None = None, force: bool = False) -> ConversionSummary:
    file = session.get(ArchivedFile, file_id)
    if not file or file.download_status != "verified" or not file.local_relpath:
        raise FileNotFoundError("verified source unavailable")
    source = settings.data_root / file.local_relpath
    if not source.exists():
        candidate = settings.archive_root / file.local_relpath
        source = candidate if candidate.exists() else source
    if not source.is_file(): raise FileNotFoundError("verified source unavailable")
    item = session.get(OAItem, file.oa_item_id)
    summary=ConversionSummary(total=1)
    _convert_one(session, settings, source, source.relative_to(settings.archive_root).as_posix(), file, item, summary, force, engine_override=engine)
    return summary


def _convert_one(session: Session, settings: Settings, source: Path, relative: str, file: ArchivedFile | None, item: OAItem | None,
                 summary: ConversionSummary, force: bool, engine_override: str | None = None) -> None:
    source_hash = sha256_file(source)
    destination = markdown_path_for_source(source, settings.archive_root, settings.markdown_root)
    markdown_relpath = destination.relative_to(settings.workspace_root).as_posix()
    prior = session.scalar(select(MarkdownExport).where(MarkdownExport.markdown_relpath == markdown_relpath))
    engine_hint, version_hint = (engine_override, "explicit") if engine_override else _engine_hint(source, settings)
    config_hash = _config_hash(settings, engine_hint)
    prior_values = {key: getattr(prior, key) for key in ("source_sha256", "parse_engine", "parse_engine_version", "parse_config_hash", "schema_version", "status")} if prior else None
    if settings.conversion.incremental and not needs_conversion(prior_values, source_hash, engine_hint, version_hint, config_hash, SCHEMA_VERSION, force=force):
        summary.skipped += 1
        return
    record = prior or MarkdownExport(markdown_relpath=markdown_relpath, source_relpath=relative, source_sha256=source_hash,
                                     parse_engine=engine_hint, parse_engine_version=version_hint, parse_config_hash=config_hash,
                                     schema_version=SCHEMA_VERSION, status="pending")
    if prior is None:
        session.add(record)
    record.attempts = (record.attempts or 0) + 1
    record.source_file_id = file.id if file else None
    record.content_object_id = file.content_object_id if file else None
    record.source_sha256 = source_hash
    record.source_relpath = relative
    # Preserve the requested engine in diagnostics even when parsing fails.
    record.parse_engine = engine_hint
    record.parse_engine_version = version_hint
    record.parse_config_hash = config_hash
    try:
        with tempfile.TemporaryDirectory(prefix="oaradar-parse-", dir=settings.data_root / "parse" if (settings.data_root / "parse").exists() else None) as temp:
            assets: Path | None = None
            decision = detect_format(source)
            attempts = parser_attempts(decision, mineru_enabled=settings.mineru.enabled)
            if decision.is_direct_text:
                body = source.read_text(encoding="utf-8", errors="replace")
                status, parsed_engine, parsed_version, quality = "success", "direct-text", "1", 1.0
            elif attempts:
                last_error: Exception | None = None
                result = None
                for selected_engine in (engine_override,) if engine_override else attempts[:2]:
                    try:
                        result = parse_file(source, settings, engine=selected_engine, output_dir=Path(temp))
                        break
                    except Exception as exc:  # noqa: BLE001 - deliberate bounded fallback
                        last_error = exc
                if result is None:
                    raise last_error or RuntimeError("parse_failed")
                body = sanitize_parser_markdown(result.output_path.read_text(encoding="utf-8"))
                status, parsed_engine, parsed_version, quality = "success", result.engine, result.engine_version, result.quality_score
                assets_candidate = result.output_path.parent
                assets = assets_candidate if any(p.is_file() and p != result.output_path for p in assets_candidate.rglob("*")) else None
                if assets:
                    body = rewrite_parser_asset_links(body, destination, assets)
            else:
                body = "该文件不生成正文 Markdown。"
                status, parsed_engine, parsed_version, quality = "unsupported", "none", "1", None
            parsed_config_hash = _config_hash(settings, engine_hint)
            error_code = decision.status_code.upper() if status == "unsupported" else None
            metadata = ExportMetadata(source_relpath=relative, source_filename=source.name, source_sha256=source_hash,
                source_size_bytes=source.stat().st_size, source_file_id=file.id if file else None,
                source_channel=item.source_channel if item else (relative.split("/", 1)[0] if "/" in relative else "unknown"),
                oa_item_key=item.oa_item_key if item else None, logical_item_id=item.logical_item_id if item else None,
                actual_file_type=decision.actual_file_type, actual_file_type_source=decision.detection_source,
                parse_status=status, parse_engine=parsed_engine, parse_engine_version=parsed_version,
                parse_config_hash=parsed_config_hash, quality_score=quality, last_error_code=error_code)
            content = render_markdown(metadata, body)
            publish_markdown(destination, content, source_hash, assets)
        record.parse_engine, record.parse_engine_version, record.parse_config_hash = parsed_engine, parsed_version, config_hash
        record.status, record.quality_score, record.last_error_code, record.last_error = status, quality, error_code, None
        record.markdown_sha256 = hashlib.sha256(content.encode()).hexdigest()
        record.generated_at = datetime.now(UTC)
        setattr(summary, status, getattr(summary, status) + 1)
    except Exception as exc:  # noqa: BLE001 - export records the conversion exception
        terminal_code = terminal_source_error(exc)
        record.status = "unsupported" if terminal_code else "failed"
        record.last_error_code, record.last_error = terminal_code or type(exc).__name__.upper(), str(exc)[:2000]
        if terminal_code: summary.unsupported += 1
        else: summary.failed += 1
        if not destination.exists() and settings.markdown_export.generate_failure_stub:
            metadata = ExportMetadata(source_relpath=relative, source_filename=source.name, source_sha256=source_hash,
                source_size_bytes=source.stat().st_size, source_file_id=file.id if file else None, parse_status="failed",
                parse_engine=engine_hint, parse_engine_version=version_hint, parse_config_hash=config_hash,
                last_error_code=record.last_error_code)
            publish_markdown(destination, render_markdown(metadata, "该文件转换失败，请检查状态后重试。"), source_hash)


def markdown_status(settings: Settings) -> dict[str, object]:
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            statuses = dict(session.execute(select(MarkdownExport.status, func.count()).group_by(MarkdownExport.status)).all())
            latest = session.scalar(select(func.max(MarkdownExport.generated_at)))
            recent = session.scalars(select(MarkdownExport).order_by(MarkdownExport.updated_at.desc(), MarkdownExport.id.desc()).limit(20)).all()
            converted = sum(statuses.values())
            total = sum(1 for path in settings.archive_root.rglob("*") if path.is_file()) if settings.archive_root.exists() else 0
            return {"raw_total": total, "success": statuses.get("success", 0), "failed": statuses.get("failed", 0),
                    "unsupported": statuses.get("unsupported", 0), "pending": max(total - converted, 0), "skipped": 0,
                    "low_quality": session.scalar(select(func.count()).select_from(MarkdownExport).where(MarkdownExport.quality_score < 0.5)) or 0,
                    "markdown_dir": str(settings.markdown_root), "latest_generated_at": latest.isoformat() if latest else None,
                    "recent_exports": [{"source_relpath": row.source_relpath, "markdown_relpath": row.markdown_relpath,
                        "parse_engine": row.parse_engine, "status": row.status, "quality_score": row.quality_score,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None} for row in recent]}
    finally:
        engine.dispose()
