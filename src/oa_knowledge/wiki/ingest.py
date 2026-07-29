"""Wiki ingestion — incremental LLM Wiki generation from source notes."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class WikiIngestor:
    """Incrementally ingests source notes into the LLM Wiki."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self.raw_sources = vault_root / "raw" / "sources" / "oa"
        self.wiki_dir = vault_root / "wiki"

    def ingest_single(self, source_path: Path) -> str | None:
        """Ingest a single source note, generating the corresponding Wiki page.

        Returns the path to the generated Wiki page, or None if no page was generated.
        """
        if not source_path.is_file():
            return None

        content = source_path.read_text(encoding="utf-8")
        fm = self._parse_frontmatter(content)
        if not fm:
            return None

        note_type = fm.get("note_type", "")
        if note_type != "source":
            return None

        # Determine Wiki category
        record_type = fm.get("record_type", "other")
        categories = {
            "official_document": ("policies", "制度页"),
            "policy": ("policies", "制度页"),
            "notice": ("topics", "主题页"),
            "report": ("topics", "主题页"),
            "contract": ("projects", "项目页"),
            "minutes": ("processes", "流程页"),
        }
        category, label = categories.get(record_type, ("topics", "主题页"))

        # Generate Wiki page
        wiki_page_dir = self.wiki_dir / category
        wiki_page_dir.mkdir(parents=True, exist_ok=True)

        item_key = fm.get("id", source_path.stem)
        wiki_page_path = wiki_page_dir / f"{item_key}.md"

        # Build Wiki page content
        title = fm.get("title", source_path.stem)
        issuer = fm.get("issuer", "")
        doc_number = fm.get("document_number", "")
        doc_date = fm.get("document_date", "")

        wiki_content = f"""# {title}

> [!info] 制度信息
> - 发文单位：{issuer}
> - 文号：{doc_number}
> - 发布日期：{doc_date}
> - 效力状态：{fm.get("validity_status", "effective")}
> - 保密级别：{fm.get("confidentiality", "internal")}

## 摘要

{self._generate_summary(content, fm)}

## 来源

- 来源笔记：[[../raw/sources/oa/{source_path.parent.name}/{source_path.stem}]]
- 来源哈希：{self._sha256(source_path)}

---
source:
  - "{source_path.relative_to(self.vault_root)}"
source_hash:
  - "{self._sha256(source_path)}"
generated_by: local_pipeline
prompt_version: wiki-ingest-v1
review_status: pending
"""
        wiki_page_path.write_text(wiki_content, encoding="utf-8")
        logger.info("Wiki page generated: %s", wiki_page_path)
        return str(wiki_page_path)

    def ingest_stale(self, limit: int = 20) -> dict:
        """Ingest all source notes marked as stale or not yet ingested.

        Returns summary with ingested, skipped, failed counts.
        """
        summary = {"ingested": 0, "skipped": 0, "failed": 0, "errors": []}

        if not self.raw_sources.is_dir():
            return summary

        source_count = 0
        for source_md in self.raw_sources.rglob("source.md"):
            source_count += 1
            if source_count > limit:
                break

            try:
                result = self.ingest_single(source_md)
                if result:
                    summary["ingested"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["errors"].append(f"{source_md}: {exc}")

        return summary

    def _parse_frontmatter(self, content: str) -> dict | None:
        """Parse YAML frontmatter from Markdown content."""
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        try:
            import yaml
            return yaml.safe_load(parts[1])
        except Exception:
            return None

    def _sha256(self, path: Path) -> str:
        """Compute SHA-256 of a file."""
        h = hashlib.sha256()
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"missing")
        return h.hexdigest()

    def _generate_summary(self, content: str, fm: dict[str, Any]) -> str:
        """Generate a deterministic summary from the source note content.

        When LLM is unavailable, produces a rule-based summary.
        """
        # Extract key fields for summary
        sections = []
        if fm.get("record_type"):
            sections.append(f"**文种**: {fm['record_type']}")
        if fm.get("validity_status"):
            sections.append(f"**效力**: {fm['validity_status']}")
        if fm.get("priority"):
            sections.append(f"**优先级**: {fm['priority']}")
        if fm.get("business_domains"):
            domains = fm["business_domains"]
            if isinstance(domains, list):
                sections.append(f"**领域**: {', '.join(domains)}")

        # Take first meaningful paragraph from body
        body = content.split("---", 2)[-1] if "---" in content else content
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#") and not p.strip().startswith(">")]
        if paragraphs:
            sections.append(paragraphs[0][:300])

        return "\n\n".join(sections) if sections else "(无摘要)"
