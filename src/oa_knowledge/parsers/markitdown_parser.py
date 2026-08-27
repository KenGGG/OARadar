"""MarkItDown parser — converts files to Markdown with artifact tracking."""

from __future__ import annotations

import json
from pathlib import Path

from markitdown import MarkItDown

from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import ParseResult


def markitdown_engine_version() -> str:
    """Return the installed MarkItDown implementation version for cache keys."""
    try:
        import markitdown as md

        return getattr(md, "__version__", "unknown")
    except ImportError:
        return "unknown"


def parse_with_markitdown(
    file_path: Path, output_dir: Path | None = None
) -> ParseResult:
    """Convert a file to Markdown using MarkItDown.

    If output_dir is provided, writes:
    - document.md (the parsed Markdown)
    - content_list.json (page/content mapping)
    - quality.json (quality assessment)

    Returns ParseResult with all metrics.
    """
    md = MarkItDown()
    result = md.convert(str(file_path))
    text = result.text_content or ""

    quality = assess_quality(text, file_path)

    if output_dir is not None:
        safe_name = _safe_name(file_path)
        engine_ver = markitdown_engine_version()
        parse_dir = output_dir / f"markitdown-v{engine_ver}"
        parse_dir.mkdir(parents=True, exist_ok=True)

        md_path = parse_dir / f"{safe_name}.md"
        md_path.write_text(text, encoding="utf-8")

        # Content list (page mapping)
        content_list = []
        if hasattr(result, "metadata") and result.metadata:
            for key, val in result.metadata.items():
                content_list.append({"key": str(key), "value": str(val)})
        content_list_path = parse_dir / f"{safe_name}_content.json"
        content_list_path.write_text(
            json.dumps(content_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Quality report
        quality["output_relpath"] = str(md_path.relative_to(output_dir))
        quality_path = parse_dir / f"{safe_name}_quality.json"
        quality_path.write_text(
            json.dumps(quality, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        output_path = md_path
    else:
        output_path = file_path.with_suffix(".md")

    return ParseResult(
        output_path=output_path,
        engine="markitdown",
        engine_version=markitdown_engine_version(),
        quality_score=quality["quality_score"],
        warnings=quality["warnings"],
        text_length=quality["text_length"],
        chinese_char_ratio=quality["chinese_char_ratio"],
        replacement_char_ratio=quality["replacement_char_ratio"],
        table_count=quality["table_count"],
        image_count=quality["image_count"],
    )


def _safe_name(file_path: Path) -> str:
    """Create a filesystem-safe name from a file path."""
    stem = file_path.stem.replace(" ", "_")
    return stem
