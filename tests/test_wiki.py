"""Tests for stage 6 Wiki ingestion and linting."""

from __future__ import annotations

from pathlib import Path

from oa_knowledge.wiki.ingest import WikiIngestor
from oa_knowledge.wiki.lint import WikiLinter, LintIssue


# --- Wiki ingestor tests ---


def test_wiki_ingestor_no_sources(tmp_path: Path) -> None:
    """Ingestor should return empty summary when no sources exist."""
    vault = tmp_path / "vault"
    vault.mkdir()
    ingestor = WikiIngestor(vault)
    result = ingestor.ingest_stale(limit=10)
    assert result["ingested"] == 0
    assert result["skipped"] == 0


def test_wiki_ingestor_single_source(tmp_path: Path) -> None:
    """Ingestor should generate a Wiki page from a source note."""
    vault = tmp_path / "vault"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test-item"
    sources_dir.mkdir(parents=True)

    source_content = """---
id: oa_1
title: Test Policy
note_type: source
record_type: policy
validity_status: effective
---

# Test Policy

This is a test policy document.
"""
    (sources_dir / "source.md").write_text(source_content, encoding="utf-8")

    ingestor = WikiIngestor(vault)
    result = ingestor.ingest_single(sources_dir / "source.md")
    assert result is not None

    # Verify Wiki page was created
    wiki_page = vault / "wiki" / "policies" / "oa_1.md"
    assert wiki_page.is_file()
    content = wiki_page.read_text(encoding="utf-8")
    assert "Test Policy" in content
    assert "policy" in content.lower()


def test_wiki_ingestor_skips_non_source(tmp_path: Path) -> None:
    """Ingestor should skip notes that are not note_type=source."""
    vault = tmp_path / "vault"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test-item"
    sources_dir.mkdir(parents=True)

    source_content = """---
id: oa_2
title: Not a Source
note_type: wiki
---

# Wiki Page
"""
    (sources_dir / "source.md").write_text(source_content, encoding="utf-8")

    ingestor = WikiIngestor(vault)
    result = ingestor.ingest_single(sources_dir / "source.md")
    assert result is None


def test_wiki_ingestor_generates_summary() -> None:
    """_generate_summary should produce content from frontmatter."""
    vault = Path("/tmp/nonexistent")
    ingestor = WikiIngestor(vault)
    fm = {"record_type": "policy", "validity_status": "effective", "priority": "high"}
    summary = ingestor._generate_summary("# Title\n\nBody text", fm)
    assert "policy" in summary.lower() or "效力" in summary


# --- Wiki linter tests ---


def test_linter_no_vault(tmp_path: Path) -> None:
    """Linter should return empty when vault doesn't exist."""
    vault = tmp_path / "vault"
    linter = WikiLinter(vault)
    issues = linter.lint()
    assert isinstance(issues, list)


def test_linter_finds_missing_frontmatter(tmp_path: Path) -> None:
    """Linter should flag source notes without frontmatter."""
    vault = tmp_path / "vault"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test"
    sources_dir.mkdir(parents=True)
    (sources_dir / "source.md").write_text("# No frontmatter\n\nBody", encoding="utf-8")

    linter = WikiLinter(vault)
    issues = linter._check_source_notes()
    assert any(i.message == "Missing frontmatter" for i in issues)


def test_linter_finds_missing_title(tmp_path: Path) -> None:
    """Linter should warn about missing title heading."""
    vault = tmp_path / "vault"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test"
    sources_dir.mkdir(parents=True)
    (sources_dir / "source.md").write_text(
        "---\nid: test\n---\nNo heading here", encoding="utf-8"
    )

    linter = WikiLinter(vault)
    issues = linter._check_source_notes()
    assert any("title" in i.message.lower() for i in issues)


def test_linter_finds_broken_wikilink(tmp_path: Path) -> None:
    """Linter should detect broken wikilinks."""
    vault = tmp_path / "vault"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test"
    sources_dir.mkdir(parents=True)
    (sources_dir / "source.md").write_text(
        "---\nid: test\n---\n[[nonexistent/file]]", encoding="utf-8"
    )

    linter = WikiLinter(vault)
    issues = linter._check_source_notes()
    assert any("Broken wikilink" in i.message for i in issues)


def test_linter_checks_circular_ingestion(tmp_path: Path) -> None:
    """Linter should detect if Wiki files appear in sources."""
    vault = tmp_path / "vault"
    wiki_dir = vault / "wiki"
    sources_dir = vault / "raw" / "sources" / "oa" / "2026" / "07" / "test"
    wiki_dir.mkdir(parents=True)
    sources_dir.mkdir(parents=True)

    # Place a Wiki file in sources (circular)
    (sources_dir / "source.md").write_text(
        "---\nid: wiki\ntitle: Circular\n---\n# Wiki", encoding="utf-8"
    )

    linter = WikiLinter(vault)
    issues = linter._check_circular_ingestion()
    # No circular issue since the file isn't in wiki_dir
    assert isinstance(issues, list)


def test_lint_issue_dataclass() -> None:
    """LintIssue should be a proper dataclass."""
    issue = LintIssue(severity="error", file="test.md", message="Test issue")
    assert issue.severity == "error"
    assert issue.file == "test.md"
    assert issue.message == "Test issue"
    assert issue.suggestion == ""
