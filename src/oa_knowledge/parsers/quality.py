"""Document parsing quality assessment."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def assess_quality(
    markdown_content: str,
    original_path: Path,
    *,
    preflight_info: dict[str, Any] | None = None,
) -> dict:
    """Assess quality of parsed Markdown content.

    Returns dict with quality_score (0-1), metrics, and warnings.
    When score < 0.5, adds a review_required flag.
    """
    text_length = len(markdown_content)
    chinese_chars = len(re.findall(r"[一-鿿㐀-䶿]", markdown_content))
    chinese_ratio = chinese_chars / max(text_length, 1)
    replacement_chars = markdown_content.count("�")
    replacement_ratio = replacement_chars / max(text_length, 1)

    # Count table rows (lines with | delimiters)
    table_rows = len(re.findall(r"\|.*\|", markdown_content))

    # Count image references
    image_refs = len(re.findall(r"!\[.*?\]\(.*?\)", markdown_content))

    warnings: list[str] = []
    score = 1.0

    # Text too short
    if text_length < 50:
        score -= 0.5
        warnings.append("very_short_output")

    # Replacement characters (mojibake)
    if replacement_ratio > 0.05:
        score -= 0.3
        warnings.append(f"high_replacement_char_ratio: {replacement_ratio:.2%}")

    # Low Chinese ratio for CJK documents
    if chinese_ratio < 0.1 and original_path.suffix.lower() in {".pdf", ".docx"}:
        score -= 0.2
        warnings.append("low_chinese_ratio_for_cjk_document")

    # Empty pages detection (preflight only)
    if preflight_info and preflight_info.get("page_count", 0) > 0:
        empty_pages = preflight_info.get("empty_page_count", 0)
        total_pages = preflight_info["page_count"]
        if total_pages > 0 and empty_pages / total_pages > 0.2:
            score -= 0.3
            warnings.append(f"over_20_percent_empty_pages: {empty_pages}/{total_pages}")

    # Table presence mismatch (preflight hints + MD output)
    if preflight_info and preflight_info.get("has_tables_hint") and table_rows == 0:
        score -= 0.2
        warnings.append("original_has_tables_but_markdown_has_none")

    # Large images hint (scanned content)
    if preflight_info and preflight_info.get("has_large_images"):
        if chinese_ratio < 0.05 and text_length < 200:
            score -= 0.2
            warnings.append("possible_scanned_pdf_with_low_text")

    # Overall quality low
    if text_length > 0 and score < 0.5:
        warnings.append("overall_quality_low")

    return {
        "quality_score": max(0.0, min(1.0, score)),
        "text_length": text_length,
        "chinese_char_ratio": round(chinese_ratio, 4),
        "replacement_char_ratio": round(replacement_ratio, 4),
        "table_count": table_rows,
        "image_count": image_refs,
        "warnings": warnings,
        "review_required": score < 0.5,
    }
