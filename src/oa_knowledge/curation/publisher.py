"""Atomic deterministic publication from validated Source Markdown."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

import yaml

from oa_knowledge.curation.canonical import publication_relpath, sanitize_component
from oa_knowledge.curation.package import PackageSource
from oa_knowledge.curation.schemas import DocumentDecision


@dataclass(frozen=True)
class PublicationResult:
    relpath: str
    files: tuple[str, ...]


def _frontmatter(payload: dict) -> str:
    return "---\n" + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n"


def _output_bytes(source: PackageSource, metadata: dict) -> bytes:
    return (_frontmatter(metadata) + source.text.rstrip("\n") + "\n").encode("utf-8")


def publish_document(
    data_root: Path,
    decision: DocumentDecision,
    sources: dict[str, PackageSource],
    *,
    canonical_id: str,
    decision_version: int,
    fallback_date: str,
) -> PublicationResult:
    selected_keys = [edge.source_key for edge in decision.sources]
    if any(key not in sources for key in selected_keys):
        raise ValueError("decision references a source outside the current package")
    relpath = publication_relpath(decision, fallback_date=fallback_date, collision_key=canonical_id[-8:])
    target = data_root / Path(relpath.as_posix())
    resolved_root = data_root.resolve()
    if resolved_root not in target.resolve().parents:
        raise ValueError("curated path escapes data_root")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix=".curation-stage-", dir=target.parent))
    stage = stage_parent / target.name
    stage.mkdir()
    files: list[str] = []
    manifest_sources: list[dict] = []
    attachment_number = 0
    try:
        for ordinal, edge in enumerate(decision.sources, 1):
            source = sources[edge.source_key]
            if edge.role == "body":
                filename = "正文.md" if "正文.md" not in files else f"正文{ordinal:02d}.md"
            else:
                attachment_number += 1
                stem = sanitize_component(Path(source.title).stem, collision_key=source.content_sha256[-8:])
                filename = f"附件{attachment_number:02d}_{stem}.md"
            metadata = {
                "managed_by": "oaradar-curation",
                "canonical_id": canonical_id,
                "decision_version": decision_version,
                "source_key": source.source_key,
                "source_content_sha256": source.content_sha256,
                "source_markdown_sha256": source.markdown_sha256,
                "source_markdown_relpath": source.markdown_relpath,
                "source_role": edge.role,
                "source_ordinal": ordinal,
            }
            content = _output_bytes(source, metadata)
            (stage / filename).write_bytes(content)
            files.append(filename)
            manifest_sources.append({
                **metadata,
                "filename": filename,
                "output_sha256": hashlib.sha256(content).hexdigest(),
            })
        index_lines = [f"# {decision.normalized_title}", "", *[f"- [{name}](<{name}>)" for name in files], ""]
        (stage / "_index.md").write_text("\n".join(index_lines), encoding="utf-8")
        manifest = {
            "schema_version": "curated-manifest-v1",
            "canonical_id": canonical_id,
            "decision_version": decision_version,
            "document": decision.model_dump(mode="json"),
            "sources": manifest_sources,
        }
        (stage / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

        backup = target.parent / f".{target.name}.previous"
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage_parent.exists():
            shutil.rmtree(stage_parent)
    return PublicationResult(relpath.as_posix(), tuple(files))


def validate_publication(data_root: Path, relpath: str) -> list[str]:
    root = data_root / relpath
    issues: list[str] = []
    try:
        manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return ["manifest_invalid"]
    for source in manifest.get("sources", []):
        filename = source.get("filename", "")
        path = root / filename
        if not path.is_file():
            issues.append(f"missing:{filename}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != source.get("output_sha256"):
            issues.append(f"hash_mismatch:{filename}")
    return issues


def remove_managed_publication(data_root: Path, relpath: str, *, canonical_id: str) -> bool:
    """Remove only a validated OARadar-managed derived directory."""
    root = data_root / relpath
    resolved_root = data_root.resolve()
    if resolved_root not in root.resolve().parents or not root.is_dir():
        return False
    try:
        manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    if manifest.get("schema_version") != "curated-manifest-v1" or manifest.get("canonical_id") != canonical_id:
        return False
    shutil.rmtree(root)
    return True
