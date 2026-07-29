"""Validation for generated Obsidian-flavoured Markdown notes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml


_LINK_RE = re.compile(r"(!?)\[\[([^\]]+)\]\]")
_BLOCK_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class LintResult:
    valid: bool
    errors: tuple[str, ...]


def _frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n"):
        raise ValueError("missing YAML Properties")
    parts = content.split("---", 2)
    if len(parts) != 3:
        raise ValueError("unterminated YAML Properties")
    properties = yaml.safe_load(parts[1])
    if not isinstance(properties, dict):
        raise ValueError("YAML Properties must be a mapping")
    return properties, parts[2]


def _contains_absolute_path(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_absolute_path(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(child) for child in value)
    if not isinstance(value, str):
        return False
    return value.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _resolve_note(vault_root: Path, current: Path, target: str) -> Path | None:
    clean = target.strip().removesuffix(".md")
    if not clean:
        return current
    direct = vault_root / f"{clean}.md"
    if direct.is_file():
        return direct
    matches = [path for path in vault_root.rglob("*.md") if path.stem == Path(clean).name]
    return matches[0] if len(matches) == 1 else None


def lint_note(path: Path, vault_root: Path) -> LintResult:
    errors: list[str] = []
    try:
        content = path.read_text(encoding="utf-8")
        properties, body = _frontmatter(content)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        return LintResult(False, (str(exc),))

    if not isinstance(properties.get("title"), str) or not properties["title"].strip():
        errors.append("title must be a non-empty string")
    for field in ("aliases", "tags"):
        if not isinstance(properties.get(field), list) or not all(isinstance(v, str) for v in properties[field]):
            errors.append(f"{field} must be a string list")
    if _contains_absolute_path(properties):
        errors.append("Properties contain an absolute path")

    block_ids = _BLOCK_RE.findall(body)
    duplicates = sorted({block_id for block_id in block_ids if block_ids.count(block_id) > 1})
    for block_id in duplicates:
        errors.append(f"duplicate block ID: {block_id}")

    for embed_marker, raw_target in _LINK_RE.findall(body):
        target = raw_target.split("|", 1)[0]
        note_target, separator, anchor = target.partition("#")
        relative = PurePosixPath(note_target.replace("\\", "/")) if note_target else None
        if relative and (relative.is_absolute() or ".." in relative.parts):
            errors.append(f"unsafe internal path: {note_target}")
            continue
        if embed_marker:
            asset = vault_root.joinpath(*relative.parts) if relative else path
            if not asset.is_file():
                errors.append(f"missing embed: {note_target}")
            continue
        linked_note = _resolve_note(vault_root, path, note_target)
        if linked_note is None:
            errors.append(f"missing wikilink target: {note_target}")
            continue
        if anchor:
            linked_body = _frontmatter(linked_note.read_text(encoding="utf-8"))[1]
            if anchor.startswith("^"):
                if anchor[1:] not in _BLOCK_RE.findall(linked_body):
                    errors.append(f"missing block: {target}")
            else:
                headings = {heading.strip().rstrip("#").strip() for heading in _HEADING_RE.findall(linked_body)}
                if anchor not in headings:
                    errors.append(f"missing heading: {target}")

    return LintResult(not errors, tuple(errors))
