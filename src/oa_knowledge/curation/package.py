"""Stable, read-only package manifests over parsed OA sources."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import ArchivedFile, ContentObject, LogicalItem, MarkdownExport, OAItem, ParseArtifact


@dataclass(frozen=True)
class PackageSource:
    source_key: str
    title: str
    markdown_relpath: str
    content_sha256: str
    markdown_sha256: str
    text: str = field(repr=False)
    ordinal: int = 1
    role_hint: str = "attachment"
    source_file_id: int | None = None
    source_attachment_id: int | None = None
    archive_member_id: int | None = None
    parse_artifact_id: int | None = None
    depth: int = 1

    def manifest_row(self) -> dict:
        row = asdict(self)
        row.pop("text")
        return row


@dataclass(frozen=True)
class OAPackage:
    package_key: str
    title: str
    completed_at: str | None
    sources: tuple[PackageSource, ...]
    logical_item_id: int | None = None
    oa_item_ids: tuple[int, ...] = ()
    depth_limit_reached: bool = False

    @property
    def ordered_sources(self) -> tuple[PackageSource, ...]:
        return tuple(sorted(self.sources, key=lambda source: (source.ordinal, source.source_key)))

    @property
    def source_keys(self) -> frozenset[str]:
        return frozenset(source.source_key for source in self.sources)

    @property
    def completable(self) -> bool:
        return not self.depth_limit_reached

    def manifest(self) -> list[dict]:
        return [source.manifest_row() for source in self.ordered_sources]


def extract_source_document_body(markdown: str) -> str:
    """Strip OARadar's deterministic Source Markdown wrapper, not document content."""
    text = markdown
    if text.startswith("---\n"):
        closing = text.find("\n---\n", 4)
        if closing >= 0:
            text = text[closing + len("\n---\n"):]
    marker = "## 文档内容"
    start = text.find(marker)
    if start < 0:
        return text.strip()
    body = text[start + len(marker):].lstrip("\r\n")
    end = body.find("\n## 转换说明")
    if end >= 0:
        body = body[:end]
    return body.strip()


def package_signature(
    package: OAPackage,
    *,
    rules_version: str,
    prompt_version: str,
    schema_version: str,
    model: str,
    config_signature: str,
) -> str:
    payload = {
        "package_key": package.package_key,
        "sources": [
            {"source_key": source.source_key, "content_sha256": source.content_sha256, "markdown_sha256": source.markdown_sha256}
            for source in package.ordered_sources
        ],
        "depth_limit_reached": package.depth_limit_reached,
        "rules_version": rules_version,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "model": model,
        "config_signature": config_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_package(session: Session, settings: Settings, item: OAItem) -> OAPackage:
    """Build a package only from published, hash-verified Source Markdown."""
    items = [item]
    if item.logical_item_id is not None:
        items = list(session.scalars(
            select(OAItem).where(OAItem.logical_item_id == item.logical_item_id).order_by(OAItem.id)
        ))
    sources: list[PackageSource] = []
    ordinal = 0
    depth_limit_reached = False
    for package_item in items:
        files = session.scalars(
            select(ArchivedFile).where(ArchivedFile.oa_item_id == package_item.id).order_by(ArchivedFile.id)
        ).all()
        for source_file in files:
            depth_limit_reached = depth_limit_reached or source_file.depth >= 10
            if source_file.content_object_id is None:
                continue
            content = session.get(ContentObject, source_file.content_object_id)
            if content is None or content.active_parse_artifact_id is None:
                continue
            artifact = session.get(ParseArtifact, content.active_parse_artifact_id)
            if artifact is None or artifact.lifecycle_status != "valid":
                continue
            export = session.scalar(select(MarkdownExport).where(
                MarkdownExport.source_file_id == source_file.id,
                MarkdownExport.parse_artifact_id == artifact.id,
                MarkdownExport.status == "success",
            ).order_by(MarkdownExport.id.desc()).limit(1))
            if export is None or not export.markdown_sha256:
                continue
            relative = Path(export.markdown_relpath)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            path = (settings.workspace_root / relative).resolve()
            try:
                path.relative_to(settings.workspace_root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != export.markdown_sha256:
                continue
            ordinal += 1
            sources.append(PackageSource(
                source_key=f"file:{source_file.id}",
                title=source_file.original_name,
                markdown_relpath=path.relative_to(settings.workspace_root).as_posix(),
                content_sha256=content.sha256,
                markdown_sha256=export.markdown_sha256,
                text=extract_source_document_body(raw.decode("utf-8", errors="replace")),
                ordinal=ordinal,
                role_hint=source_file.file_role,
                source_file_id=source_file.id,
                parse_artifact_id=artifact.id,
                depth=source_file.depth,
            ))
    logical = session.get(LogicalItem, item.logical_item_id) if item.logical_item_id else None
    completed = item.completed_at.isoformat() if item.completed_at else None
    return OAPackage(
        package_key=logical.logical_key if logical else item.oa_item_key,
        title=logical.title if logical else item.title,
        completed_at=completed,
        sources=tuple(sources),
        logical_item_id=item.logical_item_id,
        oa_item_ids=tuple(package_item.id for package_item in items),
        depth_limit_reached=depth_limit_reached,
    )
