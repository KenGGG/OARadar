"""Generate valid Obsidian Bases YAML views."""

from __future__ import annotations

from pathlib import Path

import yaml


def _base_definition(name: str, filter_expression: str, order: list[str]) -> dict:
    properties = {field: {"displayName": field.replace("_", " ").title()} for field in order if not field.startswith("file.")}
    return {
        "filters": filter_expression,
        "properties": properties,
        "views": [{"type": "table", "name": name, "order": order}],
    }


def generate_bases(vault_root: Path, output_dir: Path | None = None) -> dict:
    if output_dir is None:
        output_dir = vault_root / ".bases"
    output_dir.mkdir(parents=True, exist_ok=True)
    definitions = {
        "全部知识文档": _base_definition(
            "全部知识文档", 'note_type == "source"',
            ["file.name", "issuer", "record_type", "business_domains", "validity_status", "quality_score"],
        ),
        "待人工审核": _base_definition(
            "待人工审核", 'note_type == "source" && (human_review == true || quality_score < 0.75)',
            ["file.name", "issuer", "record_type", "quality_score", "human_review"],
        ),
        "现行文件": _base_definition(
            "现行文件", 'note_type == "source" && validity_status == "effective"',
            ["file.name", "issuer", "document_number", "document_date", "business_domains"],
        ),
    }
    generated: list[str] = []
    for name, definition in definitions.items():
        path = output_dir / f"{name}.base"
        path.write_text(yaml.safe_dump(definition, allow_unicode=True, sort_keys=False), encoding="utf-8")
        generated.append(str(path))
    notes = sum(1 for _ in vault_root.rglob("*.md"))
    return {"generated": generated, "total_notes_scanned": notes}


def _parse_frontmatter(content: str) -> dict | None:
    if not content.startswith("---\n"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        result = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return result if isinstance(result, dict) else None
