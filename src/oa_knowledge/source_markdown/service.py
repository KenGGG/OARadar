"""把有效 ParseArtifact 确定性发布为唯一 Source Markdown。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
import yaml

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from oa_knowledge.archive.integrity import sha256_file
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    ContentObject,
    MarkdownExport,
    OAItem,
    ParseArtifact,
)
from oa_knowledge.markdown_export.publisher import IMAGE_LINK, publish_markdown
from oa_knowledge.markdown_export.render import (
    SCHEMA_VERSION,
    ExportMetadata,
    render_markdown,
)
from oa_knowledge.markdown_export.service import (
    rewrite_parser_asset_links,
    sanitize_parser_markdown,
)
from oa_knowledge.runtime_paths import resolve_cache_path, resolve_original_path


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


def _destination(settings: Settings, source: ArchivedFile, item: OAItem | None, *, library_decision=None) -> Path:
    if not source.local_relpath:
        raise FileNotFoundError("verified source path unavailable")
    tree = _source_tree(settings, source.local_relpath)
    from oa_knowledge.title_screening import safe_segment
    name = safe_segment(source.original_name or tree.name, 160)
    source_session=object_session(source)
    if source_session is not None:
        peers = source_session.scalars(select(ArchivedFile).where(
            ArchivedFile.oa_item_id == source.oa_item_id).order_by(ArchivedFile.id)).all()
        matches = [peer for peer in peers if safe_segment(peer.original_name or Path(peer.local_relpath or '').name,160).casefold() == name.casefold()]
        rank = next((n for n,peer in enumerate(matches,1) if peer.id == source.id),1)
        if rank > 1:
            name += f'_{rank}'
    from oa_knowledge.markdown_delivery import (
        _classification_directory,
        _item_leaf,
        _package_directory,
    )
    if item is None:
        raise FileNotFoundError("source OA item unavailable")
    session = object_session(item)
    from oa_knowledge.db.models import ArchiveClassificationPlan
    plan = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True))) if session else None
    if plan is not None:
        from oa_knowledge.db.models import OAManifestItem
        from oa_knowledge.source_library import source_directory, classify_library_item
        manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key))
        if manifest is not None:
            directory = settings.markdown_root / source_directory(library_decision or classify_library_item(session,settings,manifest,plan=plan))
            # Windows on this machine has no long-path support. Reserve space
            # for image asset subdirectories and disambiguate truncated names.
            budget = max(12, min(160, 210 - len(str(directory.resolve())) - 4))
            used = set()
            for peer in peers if source_session else [source]:
                base = safe_segment(peer.original_name or Path(peer.local_relpath or '').name,budget)
                candidate, number = base, 1
                while candidate.casefold() in used:
                    number += 1
                    candidate = f'{base}_{number}'
                used.add(candidate.casefold())
                if peer.id == source.id:
                    name = candidate
                    break
            return directory / f"{name}.md"
    decision = session.scalar(select(ClassificationDecision).where(
        ClassificationDecision.oa_item_key == item.oa_item_key,
        ClassificationDecision.is_current.is_(True),
    )) if session else None
    if (
        decision is not None
        and decision.classification_status == "classified"
        and decision.content_origin in {"internal", "external"}
    ):
        return settings.markdown_root / _package_directory(item, decision) / _item_leaf(item) / f"{name}.md"
    # Legacy/local utilities can publish pre-classification artifacts; normal
    # classified delivery always takes the frozen-decision path above.
    year = str((item.initiated_at or item.completed_at).year) if (item.initiated_at or item.completed_at) else "年份待确认"
    return settings.markdown_root / "05_待确认" / year / _item_leaf(item) / f"{name}.md"


def _document_classification(settings: Settings, source: ArchivedFile, item: OAItem | None):
    from oa_knowledge.markdown_classification import classify_document
    body = ""
    session = object_session(source)
    artifact = _active_artifact(session, source) if session else None
    if artifact is not None:
        product = _artifact_path(settings, artifact)
        if product.is_file() and sha256_file(product) == artifact.product_sha256:
            with product.open(encoding="utf-8") as stream:
                body = stream.read(4000)
    return classify_document(item.title if item else "", filename=source.original_name or "", body=body)


def _existing_is_current(record: MarkdownExport, artifact: ParseArtifact, destination: Path) -> bool:
    assets = destination.with_name(destination.stem + '.assets')
    if assets.is_dir() and any(assets.rglob('*.md')):
        return False  # Older exports copied unrelated sibling cache products.
    current = bool(
        record.status == "success"
        and record.parse_artifact_id == artifact.id
        and destination.is_file()
        and record.markdown_sha256
        and sha256_file(destination) == record.markdown_sha256
    )
    if not current:
        return False
    from oa_knowledge.markdown_classification import VERSION
    try:
        header = yaml.safe_load(destination.read_text(encoding="utf-8")[4:].split("\n---\n", 1)[0])
        from oa_knowledge.archive_classification import CLASSIFIER_REVISION
        from oa_knowledge.db.models import ArchiveClassificationPlan
        session=object_session(record)
        plan=session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True))) if session else None
        return (header.get("document_classification_version") == VERSION
                and (plan is None or header.get("classification_version")==plan.version)
                and (plan is None or "source_organizations" in header)
                and header.get("library_revision") == CLASSIFIER_REVISION)
    except (ValueError, IndexError, yaml.YAMLError, AttributeError):
        return False


def publish_active_artifact(
    session: Session,
    settings: Settings,
    source_file_id: int,
) -> MarkdownExport:
    """Publish one active parse product without reading/parsing the original file."""
    source = session.get(ArchivedFile, source_file_id)
    if source is None or source.download_status != "verified" or not source.local_relpath:
        raise FileNotFoundError("verified source unavailable")
    item = session.get(OAItem, source.oa_item_id)
    from oa_knowledge.title_screening import assert_attachment_eligible
    assert_attachment_eligible(session, item)
    if item is not None and item.source_type == "external":
        from oa_knowledge.markdown_delivery import resolve_external_document_metadata
        decision = session.scalar(select(ClassificationDecision).where(
            ClassificationDecision.oa_item_key == item.oa_item_key,
            ClassificationDecision.is_current.is_(True),
        ))
        if decision is not None and decision.classification_status == "classified":
            metadata = resolve_external_document_metadata(session, settings, item, decision)
            item.document_number = metadata.document_number
    destination = _destination(settings, source, item)
    markdown_relpath = destination.relative_to(settings.workspace_root).as_posix()
    record = session.scalar(select(MarkdownExport).where(
        MarkdownExport.source_file_id == source.id,
        MarkdownExport.schema_version == SCHEMA_VERSION,
    ))
    old_relpath = record.markdown_relpath if record else None
    old_sha256 = record.markdown_sha256 if record else None
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
    # Exported OA forms reference this server UI icon rather than an attachment
    # image. Preserve a textual marker; never fetch the server resource.
    import re
    body = re.sub(r'!\[[^\]]*\]\(/seeyon/common/content/images/checkbox_(checked_)?browse\.png(?:\?[^)]*)?\)', lambda match: '[已勾选]' if match.group(1) else '[未勾选]', body)
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
            for link in set(IMAGE_LINK.findall(body)):
                if link.startswith('data:'):
                    continue
                relative = Path(link)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('unsafe parser image path')
                child = (parsed_path.parent / relative).resolve()
                child.relative_to(parsed_path.parent.resolve())
                if child.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.tif', '.tiff'} or not child.is_file():
                    continue
                target = assets / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
            has_assets = any(path.is_file() for path in assets.rglob("*"))
            body = rewrite_parser_asset_links(body, destination, assets)
            missing_images = '[图片未包含在解析结果中]' in body or '[图片未嵌入：' in body
            if missing_images:
                metadata = replace(metadata, parse_status='failed', last_error_code='MISSING_IMAGE_ASSET')
            content = render_markdown(metadata, body)
            from oa_knowledge.markdown_classification import VERSION
            topic = _document_classification(settings, source, item)
            header, rest = content[4:].split("\n---\n", 1)
            frontmatter = yaml.safe_load(header)
            frontmatter.update(document_category=topic.category, document_type=topic.document_type,
                document_classification_rule=topic.rule_id, document_classification_version=VERSION,
                document_classification_needs_review=topic.needs_review)
            from oa_knowledge.archive_classification import CLASSIFIER_REVISION
            from oa_knowledge.db.models import OAManifestItem, ArchiveClassificationPlan
            frontmatter.update(tags=[topic.document_type] if not topic.needs_review else [], library_revision=CLASSIFIER_REVISION)
            plan = session.scalar(select(ArchiveClassificationPlan).where(ArchiveClassificationPlan.active.is_(True)))
            manifest = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == item.oa_item_key)) if item else None
            if item:
                frontmatter.update(oa_title=manifest.title if manifest else item.title,
                    oa_initiated_at=manifest.initiated_at.isoformat() if manifest and manifest.initiated_at else None,
                    oa_sender=manifest.sender if manifest else None)
            if plan is not None and manifest is not None:
                from oa_knowledge.source_library import classify_library_item
                origin = classify_library_item(session,settings,manifest,plan=plan)
                frontmatter.update({key:origin.get(key) for key in ("archive_category_id", "archive_organization",
                    "archive_series", "related_publication_series", "archive_region", "archive_agency", "source_organizations", "archive_year", "classification_version", "classification_source",
                    "classification_reason", "classification_status", "needs_review", "virtual_archive_path")})
            content = "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n" + rest
            original_link = quote(os.path.relpath(resolve_original_path(settings, source.local_relpath), destination.parent), safe="/")
            content = content.replace("## 文档内容\n", f"[查看原件]({original_link})\n\n## 文档内容\n", 1)
            publish_markdown(destination, content, record.source_sha256, assets if has_assets else None)
        record.status = "failed" if missing_images else "success"
        record.markdown_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        record.assets_relpath = (
            destination.with_name(destination.name.removesuffix(".md") + ".assets")
            .relative_to(settings.workspace_root).as_posix()
            if has_assets else None
        )
        record.last_error_code = 'MISSING_IMAGE_ASSET' if missing_images else None
        record.last_error = 'Parser image assets unavailable; delivery incomplete.' if missing_images else None
        record.generated_at = datetime.now(UTC)
        if record.status == "success" and old_relpath and old_sha256:
            from oa_knowledge.markdown_relocation import retire_rendered_output
            retire_rendered_output(settings,old_relpath,old_sha256,record.markdown_relpath,record.markdown_sha256)
        session.flush()
        # The valid artifact is also classification evidence and the parse
        # idempotency checkpoint. Publishing must not invalidate that cache.
        return record
    except Exception as exc:
        record.status = "failed"
        record.last_error_code = type(exc).__name__.upper()
        record.last_error = str(exc)[:2000]
        session.flush()
        raise
