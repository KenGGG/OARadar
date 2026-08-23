"""把有效 ParseArtifact 确定性发布为唯一 Source Markdown。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ContentObject, MarkdownExport, OAItem, ParseArtifact
from oa_knowledge.markdown_export.publisher import publish_markdown
from oa_knowledge.markdown_export.render import ExportMetadata, SCHEMA_VERSION, render_markdown
from oa_knowledge.markdown_export.service import rewrite_parser_asset_links, sanitize_parser_markdown
from oa_knowledge.runtime_paths import resolve_cache_path


def _active_artifact(session: Session, source: ArchivedFile) -> ParseArtifact | None:
    content = session.get(ContentObject, source.content_object_id) if source.content_object_id else None
    if content and content.active_parse_artifact_id:
        artifact = session.get(ParseArtifact, content.active_parse_artifact_id)
        if artifact and artifact.lifecycle_status == "valid":
            return artifact
    if source.content_object_id is None:
        return None
    return session.scalar(
        select(ParseArtifact).where(
            ParseArtifact.content_object_id == source.content_object_id,
            ParseArtifact.lifecycle_status == "valid",
        ).order_by(ParseArtifact.quality_score.desc(), ParseArtifact.id.desc()).limit(1)
    )


def _artifact_path(settings: Settings, artifact: ParseArtifact) -> Path:
    return resolve_cache_path(settings, artifact.output_relpath)


def _source_tree(settings: Settings, local_relpath: str) -> PurePosixPath:
    relative = PurePosixPath(local_relpath)
    prefix = PurePosixPath("originals")
    try:
        tree = relative.relative_to(prefix)
    except ValueError as exc:
        raise ValueError("source file path is outside data/originals") from exc
    if not tree.parts:
        raise ValueError("source file path is not an original file")
    return tree


def _destination(settings: Settings, source: ArchivedFile) -> Path:
    if not source.local_relpath:
        raise FileNotFoundError("verified source path unavailable")
    tree = _source_tree(settings, source.local_relpath)
    return settings.markdown_root.joinpath(*tree.parts[:-1], f"{tree.name}.md")


def _existing_is_current(record: MarkdownExport, artifact: ParseArtifact, destination: Path) -> bool:
    return bool(
        record.status == "success"
        and record.parse_artifact_id == artifact.id
        and destination.is_file()
        and record.markdown_sha256
        and sha256_file(destination) == record.markdown_sha256
    )


def publish_active_artifact(
    session: Session,
    settings: Settings,
    source_file_id: int,
) -> MarkdownExport:
    """Publish one active parse product without reading/parsing the original file."""
    source = session.get(ArchivedFile, source_file_id)
    if source is None or source.download_status != "verified" or not source.local_relpath:
        raise FileNotFoundError("verified source unavailable")
    destination = _destination(settings, source)
    markdown_relpath = destination.relative_to(settings.workspace_root).as_posix()
    record = session.scalar(select(MarkdownExport).where(
        MarkdownExport.source_file_id == source.id,
        MarkdownExport.schema_version == SCHEMA_VERSION,
    ))
    artifact = _active_artifact(session, source)
    if artifact is not None and record is not None and _existing_is_current(record, artifact, destination):
        return record
    if artifact is None:
        raise FileNotFoundError("valid parse artifact unavailable")
    parsed_path = _artifact_path(settings, artifact)
    if not parsed_path.is_file():
        raise FileNotFoundError("valid parse artifact file unavailable")
    product_sha256 = sha256_file(parsed_path)
    if artifact.product_sha256 and artifact.product_sha256 != product_sha256:
        raise ValueError("parse artifact product hash mismatch")

    item = session.get(OAItem, source.oa_item_id)
    record = record or MarkdownExport(
        source_file_id=source.id,
        source_relpath=source.local_relpath,
        markdown_relpath=markdown_relpath,
        source_sha256=source.sha256 or artifact.source_sha256,
        parse_engine=artifact.engine,
        parse_engine_version=artifact.engine_version,
        parse_config_hash=artifact.config_hash,
        schema_version=SCHEMA_VERSION,
        status="pending",
    )
    if record.id is None:
        session.add(record)
    record.attempts = (record.attempts or 0) + 1
    record.source_file_id = source.id
    record.oa_item_id = source.oa_item_id
    record.document_kind = "attachment"
    record.content_object_id = source.content_object_id
    record.parse_artifact_id = artifact.id
    record.source_relpath = source.local_relpath
    record.markdown_relpath = markdown_relpath
    record.source_sha256 = source.sha256 or artifact.source_sha256
    record.parse_engine = artifact.engine
    record.parse_engine_version = artifact.engine_version
    record.parse_config_hash = artifact.config_hash
    record.quality_score = artifact.quality_score

    body = sanitize_parser_markdown(parsed_path.read_text(encoding="utf-8", errors="replace"))
    page_map = json.loads(artifact.page_map_json or "{}")
    metadata = ExportMetadata(
        source_relpath=source.local_relpath,
        source_filename=source.original_name or Path(source.local_relpath).name,
        source_sha256=record.source_sha256,
        source_size_bytes=source.size_bytes or 0,
        source_file_id=source.id,
        source_channel=item.source_channel if item else "done",
        oa_item_key=item.oa_item_key if item else None,
        logical_item_id=item.logical_item_id if item else None,
        parse_status="success",
        parse_engine=artifact.engine,
        parse_engine_version=artifact.engine_version,
        parse_artifact_id=artifact.id,
        parse_config_hash=artifact.config_hash,
        quality_score=artifact.quality_score,
        page_count=page_map.get("page_count") if isinstance(page_map, dict) else None,
        page_map_available=bool(page_map),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="oaradar-source-md-") as temp_name:
            assets = Path(temp_name)
            for child in parsed_path.parent.rglob("*"):
                if child.is_file() and child != parsed_path and not child.is_symlink():
                    target = assets / child.relative_to(parsed_path.parent)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child, target)
            has_assets = any(path.is_file() for path in assets.rglob("*"))
            if has_assets:
                body = rewrite_parser_asset_links(body, destination, assets)
            content = render_markdown(metadata, body)
            publish_markdown(destination, content, record.source_sha256, assets if has_assets else None)
        record.status = "success"
        record.markdown_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        record.assets_relpath = (
            destination.with_name(destination.name.removesuffix(".md") + ".assets")
            .relative_to(settings.workspace_root).as_posix()
            if has_assets else None
        )
        record.last_error_code = None
        record.last_error = None
        record.generated_at = datetime.now(timezone.utc)
        session.flush()
        work_root = settings.parse_work_root.resolve()
        artifact_dir = parsed_path.parent.resolve()
        if work_root not in artifact_dir.parents:
            raise ValueError("parse artifact directory is outside the parse work root")
        shutil.rmtree(artifact_dir)
        return record
    except Exception as exc:
        record.status = "failed"
        record.last_error_code = type(exc).__name__.upper()
        record.last_error = str(exc)[:2000]
        session.flush()
        raise
