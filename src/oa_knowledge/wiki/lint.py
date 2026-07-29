"""Wiki lint — checks for common issues in the LLM Wiki."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class LintIssue:
    severity: str  # "error", "warning", "info"
    file: str
    message: str
    suggestion: str = ""


class WikiLinter:
    """Checks Wiki and source notes for common issues."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self.wiki_dir = vault_root / "wiki"
        self.sources_dir = vault_root / "raw" / "sources"

    def lint(self) -> list[LintIssue]:
        """Run all lint checks and return issues found."""
        issues: list[LintIssue] = []

        if self.sources_dir.is_dir():
            issues.extend(self._check_source_notes())
        if self.wiki_dir.is_dir():
            issues.extend(self._check_wiki_pages())

        # Cross-checks
        issues.extend(self._check_circular_ingestion())
        issues.extend(self._check_orphaned_wiki())

        return issues

    def _check_source_notes(self) -> list[LintIssue]:
        """Check source notes for issues."""
        issues: list[LintIssue] = []

        for source_md in self.sources_dir.rglob("source.md"):
            try:
                content = source_md.read_text(encoding="utf-8")
            except OSError:
                issues.append(LintIssue("error", str(source_md), "Cannot read file"))
                continue

            # Check for frontmatter
            if not content.startswith("---"):
                issues.append(LintIssue("error", str(source_md), "Missing frontmatter"))
                continue

            # Check for title
            if not re.search(r"^# .+", content, re.MULTILINE):
                issues.append(LintIssue("warning", str(source_md), "No title heading found"))

            # Check for broken wikilinks
            wikilinks = re.findall(r"\[\[([^\]]+)\]\]", content)
            for wl in wikilinks:
                if wl.startswith("!"):
                    continue  # Embed, not a link
                # Check if referenced file exists relative to vault
                ref_path = self.vault_root / wl
                if not ref_path.exists() and not ref_path.with_suffix(".md").exists():
                    issues.append(LintIssue("warning", str(source_md), f"Broken wikilink: {wl}"))

            # Check for missing source_hash
            if "source_sha256" not in content and "source_hash" not in content:
                issues.append(LintIssue("info", str(source_md), "Missing source hash in frontmatter"))

        return issues

    def _check_wiki_pages(self) -> list[LintIssue]:
        """Check Wiki pages for issues."""
        issues: list[LintIssue] = []

        for wiki_md in self.wiki_dir.rglob("*.md"):
            try:
                content = wiki_md.read_text(encoding="utf-8")
            except OSError:
                issues.append(LintIssue("error", str(wiki_md), "Cannot read file"))
                continue

            # Check for frontmatter
            if not content.startswith("---"):
                issues.append(LintIssue("error", str(wiki_md), "Missing frontmatter"))
                continue

            # Check for sources
            if "sources:" not in content and "source:" not in content:
                issues.append(LintIssue("warning", str(wiki_md), "Missing source traceability"))

            # Check for stale status without review
            if "review_status: pending" not in content:
                if "source_hash" not in content.lower() and "sources:" not in content:
                    issues.append(LintIssue("info", str(wiki_md), "Wiki page lacks source tracking"))

            # Check for broken wikilinks
            wikilinks = re.findall(r"\[\[([^\]]+)\]\]", content)
            for wl in wikilinks:
                ref_path = self.vault_root / wl
                if not ref_path.exists() and not ref_path.with_suffix(".md").exists():
                    issues.append(LintIssue("warning", str(wiki_md), f"Broken wikilink in Wiki: {wl}"))

        return issues

    def _check_circular_ingestion(self) -> list[LintIssue]:
        """Ensure Wiki pages are not ingested as new sources."""
        issues: list[LintIssue] = []
        if not self.wiki_dir.is_dir():
            return issues

        wiki_files = set()
        for md in self.wiki_dir.rglob("*.md"):
            wiki_files.add(md.relative_to(self.vault_root))

        if self.sources_dir.is_dir():
            for source_md in self.sources_dir.rglob("source.md"):
                rel = source_md.relative_to(self.vault_root)
                if rel in wiki_files:
                    issues.append(LintIssue(
                        "error", str(source_md),
                        "Circular ingestion: Wiki file found in sources",
                        "Move Wiki files out of sources directory",
                    ))

        return issues

    def _check_orphaned_wiki(self) -> list[LintIssue]:
        """Check for Wiki pages with no corresponding source."""
        issues: list[LintIssue] = []
        if not self.wiki_dir.is_dir():
            return issues

        for wiki_md in self.wiki_dir.rglob("*.md"):
            try:
                content = wiki_md.read_text(encoding="utf-8")
                # Look for source references
                if "source:" not in content.lower() or "[[../raw/sources" not in content:
                    issues.append(LintIssue(
                        "warning", str(wiki_md),
                        "Wiki page may be orphaned (no source reference)",
                    ))
            except OSError:
                pass

        return issues
