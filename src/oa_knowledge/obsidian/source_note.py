"""Obsidian source note builder — generates FMD-compliant source notes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from oa_knowledge.enrich.rules import Classification


OBSIDIAN_PROFILE = "kepano/obsidian-skills/obsidian-markdown"
OBSIDIAN_PROFILE_REVISION = "a1dc48e68138490d522c04cbf5822214c6eb1202"


def build_frontmatter(
    item_id: int,
    title: str,
    classifications: list[Classification],
    source_channel: str = "done",
    issuer: str = "",
    issuing_department: str = "",
    document_number: str = "",
    document_date: str = "",
    received_at: str = "",
    effective_date: str = "",
    deadline: str = "",
    workflow_status: str = "completed",
    priority: str = "medium",
    source_pdf: str = "",
    source_sha256: str = "",
    parse_engine: str = "markitdown",
    parse_version: str = "unknown",
    quality_score: float = 1.0,
    business_domains: list[str] | None = None,
    human_review: bool = False,
    aliases: list[str] | None = None,
) -> str:
    """Generate YAML frontmatter for an Obsidian source note."""
    facets = {c.facet: c.value for c in classifications}

    # Build tags
    tags: list[str] = ["oa/source", f"oa/{facets.get('record_type', 'other')}"]
    if business_domains:
        for domain in business_domains:
            tags.append(f"domain/{domain}")

    properties: dict[str, object] = {
        "id": f"oa_{item_id:x}",
        "title": title,
        "aliases": list(aliases or []),
        "note_type": "source",
        "source_system": "oa",
        "source_channel": source_channel,
        "record_type": facets.get("record_type", "uncategorized"),
        "authority_level": facets.get("authority_level", "unknown"),
        "workflow_status": workflow_status,
        "validity_status": facets.get("validity_status", "unknown"),
        "priority": priority,
        "confidentiality": facets.get("confidentiality", "internal"),
        "business_domains": list(business_domains or []),
        "parse_engine": parse_engine,
        "parse_version": parse_version,
        "page_map_available": True,
        "quality_score": float(quality_score),
        "human_review": bool(human_review),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "obsidian_profile": OBSIDIAN_PROFILE,
        "obsidian_profile_revision": OBSIDIAN_PROFILE_REVISION,
        "tags": tags,
    }
    optional = {
        "issuer": issuer,
        "issuing_department": issuing_department,
        "document_number": document_number,
        "document_date": document_date,
        "received_at": received_at,
        "effective_date": effective_date,
        "deadline": deadline,
        "source_pdf": f"[[files/{Path(source_pdf).name}]]" if source_pdf else "",
        "source_sha256": source_sha256,
    }
    properties.update({key: value for key, value in optional.items() if value})
    payload = yaml.safe_dump(properties, allow_unicode=True, sort_keys=False).rstrip()
    return f"---\n{payload}\n---"


def build_source_note(
    title: str,
    summary: str = "",
    actions: str = "",
    content: str = "",
    page_content: str = "",
    source_channel: str = "done",
    issuer: str = "",
    document_number: str = "",
    received_at: str = "",
    attachments: list[str] | None = None,
) -> str:
    """Generate a complete Obsidian source note with frontmatter and body."""
    lines: list[str] = []

    # Source info callout
    lines.append("> [!info] 来源信息")
    lines.append(f"> - OA 渠道：{source_channel}")
    if issuer:
        lines.append(f"> - 发文单位：{issuer}")
    if document_number:
        lines.append(f"> - 文号：{document_number}")
    if received_at:
        lines.append(f"> - 收到时间：{received_at}")
    lines.append("")

    # Summary
    if summary:
        lines.append("## 系统摘要")
        lines.append(summary)
        lines.append("")

    # Actions
    if actions:
        lines.append("## 行动要求")
        lines.append(actions)
        lines.append("")

    # Core content
    if content:
        lines.append("## 核心要点")
        lines.append(content)
        lines.append("")

    # Original text
    if page_content:
        lines.append("## 原文")
        lines.append(page_content)
        lines.append("")

    # Attachments
    if attachments:
        lines.append("## 附件")
        for att in attachments:
            lines.append(f"- ![[files/{att}]]")
        lines.append("")

    return "\n".join(lines)
