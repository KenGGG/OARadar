"""Tests for stage 4 Obsidian publishing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from oa_knowledge.config import Settings
from oa_knowledge.enrich.rules import Classification
from oa_knowledge.obsidian.bases import _parse_frontmatter, generate_bases
from oa_knowledge.obsidian.links import WikilinkResolver
from oa_knowledge.obsidian.lint import lint_note
from oa_knowledge.obsidian.publisher import PublishPipeline
from oa_knowledge.obsidian.source_note import build_frontmatter, build_source_note


# --- Frontmatter tests ---


def test_build_frontmatter_contains_required_fields() -> None:
    """Frontmatter should include all required Obsidian fields."""
    cls = [
        Classification(facet="record_type", value="notice", source="rule", confidence=0.9, evidence="title_match"),
        Classification(facet="authority_level", value="company_department", source="rule", confidence=0.85, evidence="issuer_match"),
        Classification(facet="validity_status", value="effective", source="rule", confidence=0.5, evidence="default"),
        Classification(facet="confidentiality", value="internal", source="rule", confidence=0.3, evidence="default"),
    ]
    fm = build_frontmatter(
        item_id=42,
        title="Test Notice",
        classifications=cls,
        source_channel="done",
        issuer="Testing Dept",
        document_number="2026-001",
    )
    assert "id: oa_2a" in fm
    assert "title: Test Notice" in fm
    assert "note_type: source" in fm
    assert "source_system: oa" in fm
    assert "record_type: notice" in fm
    assert "tags:" in fm


def test_build_frontmatter_with_aliases() -> None:
    """Frontmatter should include aliases when provided."""
    cls = [Classification(facet="record_type", value="notice", source="rule", confidence=0.9)]
    fm = build_frontmatter(
        item_id=1,
        title="Test",
        classifications=cls,
        aliases=["Alternate Title", "Short Name"],
    )
    assert "aliases:" in fm
    assert "- Alternate Title" in fm


def test_build_frontmatter_escapes_special_chars() -> None:
    """Frontmatter should handle titles with colons and dashes."""
    cls = [Classification(facet="record_type", value="notice", source="rule", confidence=0.9)]
    fm = build_frontmatter(
        item_id=1,
        title="Notice: Budget - 2026",
        classifications=cls,
    )
    parsed = yaml.safe_load(fm.split("---", 2)[1])
    assert parsed["title"] == "Notice: Budget - 2026"
    assert parsed["aliases"] == []
    assert parsed["tags"] == ["oa/source", "oa/notice"]
    assert parsed["obsidian_profile"] == "kepano/obsidian-skills/obsidian-markdown"
    assert parsed["obsidian_profile_revision"] == "a1dc48e68138490d522c04cbf5822214c6eb1202"


# --- Source note tests ---


def test_build_source_note_has_sections() -> None:
    """Source note should contain all expected sections."""
    note = build_source_note(
        title="Test Document",
        summary="This is a summary.",
        source_channel="done",
        issuer="Test Dept",
        document_number="2026-001",
    )
    assert "> [!info] 来源信息" in note
    assert "## 系统摘要" in note
    assert "This is a summary." in note


def test_build_source_note_with_attachments() -> None:
    """Source note should include attachment embeds."""
    note = build_source_note(
        title="Doc",
        attachments=["attachment1.pdf", "attachment2.docx"],
    )
    assert "![[files/attachment1.pdf]]" in note
    assert "![[files/attachment2.docx]]" in note


# --- Wikilink resolver tests ---


def test_wikilink_resolve_alias() -> None:
    """Resolver should map aliases to canonical names."""
    resolver = WikilinkResolver()
    resolver._aliases["test_alias"] = "Test Entity"
    assert resolver.resolve("test_alias") == "[[Test Entity]]"


def test_wikilink_resolve_unknown() -> None:
    """Resolver should pass through unknown names."""
    resolver = WikilinkResolver()
    assert resolver.resolve("unknown_entity") == "[[unknown_entity]]"


def test_wikilink_get_related() -> None:
    """Resolver should return registered relationships."""
    resolver = WikilinkResolver()
    resolver.add_relationship("parent", ["child1", "child2"])
    assert resolver.get_related("parent") == ["child1", "child2"]


# --- Publisher tests ---


def test_publish_pipeline_creates_vault_structure(tmp_path: Path) -> None:
    """PublishPipeline should create vault directory structure."""
    settings = Settings(app={"data_root": str(tmp_path)})
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    pipeline = PublishPipeline(settings, vault_root=vault_root)

    # Create a classified item manually
    from oa_knowledge.db.engine import create_db_engine
    from oa_knowledge.db.migrate import upgrade_database
    from oa_knowledge.db.models import OAItem

    db_path = tmp_path / "state" / "oa.db"
    upgrade_database(db_path)
    engine = create_db_engine(db_path)

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        item = OAItem(
            oa_item_key="pub-test-1",
            source_channel="done",
            title="Published Test",
            pipeline_status="classified",
        )
        session.add(item)
        session.commit()
        item_id = item.id

    # Publish should work (may fail on CLASSIFIED check, but structure is created)
    summary = pipeline.publish_classified_items(limit=1)
    assert summary["published"] == 1
    published = list(vault_root.rglob("source.md"))
    assert len(published) == 1
    assert lint_note(published[0], vault_root).valid

    with Session(engine) as session:
        assert session.get(OAItem, item_id).pipeline_status == "published"


# --- Bases tests ---


def test_parse_frontmatter_valid() -> None:
    """_parse_frontmatter should parse valid YAML frontmatter."""
    content = "---\ntitle: Test\nrecord_type: notice\n---\nBody text"
    result = _parse_frontmatter(content)
    assert result is not None
    assert result["title"] == "Test"
    assert result["record_type"] == "notice"


def test_parse_frontmatter_invalid() -> None:
    """_parse_frontmatter should return None for non-frontmatter content."""
    assert _parse_frontmatter("No frontmatter here") is None
    assert _parse_frontmatter("---incomplete") is None


def test_generate_bases_creates_files(tmp_path: Path) -> None:
    """generate_bases should create .base files."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = generate_bases(vault)
    assert "generated" in result
    assert len(result["generated"]) > 0
    for base_path_str in result["generated"]:
        base_path = Path(base_path_str)
        assert base_path.is_file()
        data = yaml.safe_load(base_path.read_text(encoding="utf-8"))
        assert "filters" in data
        assert isinstance(data["views"], list)
        assert data["views"][0]["type"] in {"table", "cards", "list"}


def test_lint_note_rejects_absolute_paths_and_missing_embeds(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "知识文档" / "invalid.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Invalid\naliases: []\ntags: []\nsource_relative_path: /private/raw.pdf\n---\n\n![[files/missing.pdf]]\n",
        encoding="utf-8",
    )

    result = lint_note(note, vault)

    assert not result.valid
    assert any("absolute path" in error for error in result.errors)
    assert any("missing embed" in error for error in result.errors)


def test_lint_note_detects_duplicate_blocks_and_broken_heading(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = vault / "知识文档" / "broken.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Broken\naliases: []\ntags: []\n---\n\n## Existing\nOne ^evidence-1\nTwo ^evidence-1\n[[broken#Missing]]\n",
        encoding="utf-8",
    )

    result = lint_note(note, vault)

    assert not result.valid
    assert any("duplicate block ID" in error for error in result.errors)
    assert any("missing heading" in error for error in result.errors)


def test_lint_note_accepts_obsidian_markdown(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    asset = vault / "files" / "source.pdf"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"synthetic")
    note = vault / "知识文档" / "valid.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Valid\naliases: []\ntags: [oa/source]\n---\n\n## Evidence\nText ^evidence-1\n\n[[valid#Evidence]]\n![[files/source.pdf]]\n",
        encoding="utf-8",
    )

    assert lint_note(note, vault).valid
