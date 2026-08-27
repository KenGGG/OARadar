"""Parse file router — selects the best engine for a given file."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from oa_knowledge.archive.integrity import sha256_text
from oa_knowledge.parsers.format_router import detect_format, parser_attempts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseResult:
    output_path: Path
    engine: str
    engine_version: str
    quality_score: float
    warnings: list[str] = field(default_factory=list)
    text_length: int = 0
    chinese_char_ratio: float = 0.0
    replacement_char_ratio: float = 0.0
    table_count: int = 0
    image_count: int = 0
    profile_version: str = "legacy"

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            {
                "engine": self.engine,
                "engine_version": self.engine_version,
                "quality_score": self.quality_score,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256_text(payload)


def resolve_parser_version(engine: str, settings) -> str:
    """Return a selected parser's implementation identity for a cache key.

    The caller records this value before dispatching a versioned parse request;
    it prevents a parser upgrade from silently reusing an older derivative.
    """
    if engine == "markitdown":
        from oa_knowledge.parsers.markitdown_parser import markitdown_engine_version

        return markitdown_engine_version()
    if engine == "mineru":
        from oa_knowledge.parsers.mineru_parser import mineru_engine_version

        return mineru_engine_version(settings)
    if engine == "wv":
        from oa_knowledge.parsers.wv_parser import wv_engine_version

        return wv_engine_version(settings)
    if engine == "libreoffice":
        from oa_knowledge.parsers.libreoffice_parser import libreoffice_engine_version

        return libreoffice_engine_version()
    raise ValueError(f"Unknown engine: {engine}")


def preflight(file_path: Path) -> dict:
    """Analyse a PDF file using pymupdf without full parsing.

    Returns a dict with keys:
    - page_count: int
    - text_chars_per_page: float (average)
    - image_area_ratio: float (0-1)
    - font_count: int
    - has_embedded_text: bool
    - has_tables_hint: bool
    - has_large_images: bool
    - is_encrypted: bool
    - is_corrupted: bool
    - empty_page_count: int
    """
    info: dict[str, object] = {
        "page_count": 0,
        "text_chars_per_page": 0.0,
        "image_area_ratio": 0.0,
        "font_count": 0,
        "has_embedded_text": False,
        "has_tables_hint": False,
        "has_large_images": False,
        "is_encrypted": False,
        "is_corrupted": False,
        "empty_page_count": 0,
    }
    try:
        import fitz  # pymupdf

        with fitz.open(str(file_path)) as doc:
            info["page_count"] = doc.page_count
            info["is_encrypted"] = doc.is_encrypted

            total_chars = 0
            total_image_area = 0.0
            total_page_area = 0.0
            fonts: set[str] = set()
            empty_pages = 0

            for page in doc:
                text = page.get_text("text")
                chars_on_page = len(text.strip())
                total_chars += chars_on_page
                if chars_on_page == 0:
                    empty_pages += 1

                # Collect fonts from blocks
                blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
                for block in blocks.get("blocks", []):
                    if block.get("type") == 0:  # text block
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                font_name = span.get("font", "")
                                if font_name:
                                    fonts.add(font_name)

                # Image area estimation
                images = page.get_images(full=True)
                page_area = page.rect.width * page.rect.height
                total_page_area += page_area
                for img in images:
                    try:
                        rect = page.get_image_rects(img[0])
                        for r in rect if isinstance(rect, list) else [rect]:
                            if isinstance(r, fitz.Rect):
                                total_image_area += r.width * r.height
                    except Exception:
                        logger.debug("failed to measure image rects", exc_info=True)

            info["font_count"] = len(fonts)
            info["has_embedded_text"] = total_chars > 0
            info["empty_page_count"] = empty_pages

            if total_page_area > 0:
                info["image_area_ratio"] = round(total_image_area / total_page_area, 4)
            if doc.page_count > 0:
                info["text_chars_per_page"] = round(total_chars / doc.page_count, 1)

            # Heuristic hints
            info["has_tables_hint"] = total_chars > 100 and _detect_table_pattern(
                file_path
            )
            info["has_large_images"] = (
                info["image_area_ratio"] > 0.15 and info["has_embedded_text"] is False
            )

    except Exception:
        logger.debug("preflight analysis failed; marking as corrupted", exc_info=True)
        info["is_corrupted"] = True

    return info


def _detect_table_pattern(file_path: Path) -> bool:
    """Quick heuristic: look for pipe/table-like patterns in extracted text."""
    try:
        import fitz

        with fitz.open(str(file_path)) as doc:
            for page in doc:
                text = page.get_text("text")
                # Lines with repeated separators suggest tables
                pipe_lines = sum(1 for line in text.split("\n") if line.count("|") >= 3)
                if pipe_lines > 0:
                    return True
                # Alternating dash patterns
                dash_lines = sum(
                    1
                    for line in text.split("\n")
                    if re.search(r"[+-]+\s+\|[+-]+\s+\|", line)
                )
                if dash_lines > 0:
                    return True
            return False
    except Exception:
        logger.debug("table pattern detection failed", exc_info=True)
        return False


def parse_file(
    file_path: Path,
    settings,
    engine: str | None = None,
    output_dir: Path | None = None,
    profile_version: str = "legacy",
) -> ParseResult:
    """Route a file to the best available parser engine.

    Routing logic:
    - If engine is explicitly specified, use it directly.
    - Preflight analysis determines the best automatic route:
        - Encrypted/corrupted -> raises RuntimeError (should be caught by caller)
        - Digital PDF (high text density, low image ratio) -> MarkItDown
        - Scanned PDF / complex layout / tables / stamps -> MinerU
        - Office files (DOCX/PPTX/XLSX) -> MarkItDown
        - Other -> MarkItDown
    """
    from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown
    from oa_knowledge.parsers.mineru_parser import mineru_available, parse_with_mineru

    file_path = Path(file_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    decision = detect_format(file_path)

    # Explicit engine override
    if engine:
        if engine == "markitdown":
            return parse_with_markitdown(
                file_path, output_dir, profile_version=profile_version
            )
        if engine == "mineru":
            if decision.actual_file_type == "pdf" and preflight(file_path).get(
                "is_encrypted"
            ):
                raise RuntimeError("encrypted_document")
            # An explicit campaign request is authoritative. parse_with_mineru
            # performs its own retried health check; a separate probe here used
            # to reject healthy work whenever the GPU service was briefly busy.
            return parse_with_mineru(
                file_path, settings, output_dir, profile_version=profile_version
            )
        if engine == "wv":
            from oa_knowledge.parsers.wv_parser import parse_with_wv

            return parse_with_wv(
                file_path, settings, output_dir, profile_version=profile_version
            )
        if engine == "libreoffice":
            from oa_knowledge.parsers.libreoffice_parser import parse_with_libreoffice

            return parse_with_libreoffice(
                file_path, output_dir, profile_version=profile_version
            )
        raise ValueError(f"Unknown engine: {engine}")

    if decision.status_code != "parseable":
        raise RuntimeError(decision.status_code)
    if decision.is_direct_text:
        return parse_with_markitdown(file_path, output_dir, profile_version=profile_version)
    engines = parser_attempts(decision, mineru_enabled=mineru_available(settings))
    if not engines:
        raise RuntimeError("unsupported_file_type")
    primary = engines[0]
    if primary == "mineru":
        if decision.actual_file_type == "pdf":
            info = preflight(file_path)
            if info.get("is_encrypted"):
                raise RuntimeError("encrypted_document")
            if info.get("is_corrupted"):
                raise RuntimeError("corrupted_file")
        return parse_with_mineru(file_path, settings, output_dir, profile_version=profile_version)
    return parse_with_markitdown(file_path, output_dir, profile_version=profile_version)
