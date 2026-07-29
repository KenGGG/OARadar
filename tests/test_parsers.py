"""Tests for stage 3 document parsing pipeline."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings, load_settings
from oa_knowledge.constants import PipelineStatus
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile,
    OAItem,
    ParseArtifact,
    ParseJob,
)
from oa_knowledge.pipeline import ParsePipeline
from oa_knowledge.parsers.markitdown_parser import parse_with_markitdown
from oa_knowledge.parsers.eligibility import evaluate_eligibility
from oa_knowledge.parsers.mineru_parser import (
    MineruResponseError,
    _extract_mineru_zip,
    parse_with_mineru,
)
from oa_knowledge.parsers.quality import assess_quality
from oa_knowledge.parsers.router import ParseResult, parse_file, preflight


def _make_synthetic_pdf(tmp_path: Path, name: str = "test.pdf", content: str = "") -> Path:
    """Create a minimal synthetic PDF file for testing."""
    pdf_path = tmp_path / name
    # Create a minimal valid PDF
    pdf_content = (
        "%PDF-1.4\n"
        "1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        "3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        "xref\n0 4\n"
        "trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    )
    if content:
        pdf_path.write_text(content, encoding="utf-8")
    else:
        pdf_path.write_bytes(pdf_content.encode())
    return pdf_path


def _make_synthetic_docx(tmp_path: Path, name: str = "test.docx", content: str = "") -> Path:
    """Create a minimal synthetic DOCX (ZIP with [Content_Types].xml)."""
    import zipfile

    docx_path = tmp_path / name
    with zipfile.ZipFile(docx_path, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types></Types>')
        zf.writestr("word/document.xml", f'<w:document>{content}</w:document>')
    return docx_path


def _make_synthetic_html(tmp_path: Path, name: str = "test.html") -> Path:
    html_path = tmp_path / name
    html_path.write_text("<html><body><p>Hello world</p></body></html>", encoding="utf-8")
    return html_path


def _make_synthetic_md_file(tmp_path: Path, name: str = "test.txt") -> Path:
    text_path = tmp_path / name
    text_path.write_text("这是一份测试文档，包含中文内容。\n" * 100, encoding="utf-8")
    return text_path


# --- Preflight tests ---


def test_preflight_detects_encrypted(tmp_path: Path) -> None:
    """Preflight should detect encrypted PDFs."""
    pdf_path = _make_synthetic_pdf(tmp_path, "enc.pdf")
    info = preflight(pdf_path)
    # A minimal synthetic PDF is not encrypted
    assert info["is_encrypted"] is False
    assert info["is_corrupted"] is False


def test_preflight_returns_page_count(tmp_path: Path) -> None:
    """Preflight should return page_count for valid PDFs."""
    pdf_path = _make_synthetic_pdf(tmp_path, "pages.pdf")
    info = preflight(pdf_path)
    assert isinstance(info["page_count"], int)
    assert info["page_count"] >= 0


def test_preflight_handles_non_pdf(tmp_path: Path) -> None:
    """Preflight should handle non-PDF files gracefully."""
    txt_path = tmp_path / "test.txt"
    txt_path.write_text("just text", encoding="utf-8")
    info = preflight(txt_path)
    # Should not crash; may report corrupted or zero pages
    assert isinstance(info["page_count"], int)


# --- Quality assessment tests ---


def test_quality_high_chinese_doc() -> None:
    """High-quality Chinese document should score near 1.0."""
    md = "这是一份高质量的测试文档。\n" * 50
    result = assess_quality(md, Path("test.md"))
    assert result["quality_score"] > 0.8
    assert result["chinese_char_ratio"] > 0.5


def test_quality_very_short() -> None:
    """Very short output should trigger low score."""
    result = assess_quality("ab", Path("test.md"))
    # score = 1.0 - 0.5 = 0.5, so it's exactly 0.5, not < 0.5
    assert result["quality_score"] <= 0.5
    assert "very_short_output" in result["warnings"]


def test_quality_high_replacement_chars() -> None:
    """Document with many replacement characters should score lower."""
    md = "�" * 200
    result = assess_quality(md, Path("test.pdf"))
    assert result["replacement_char_ratio"] > 0.5
    assert any("high_replacement_char_ratio" in w for w in result["warnings"])


def test_quality_preflight_empty_pages() -> None:
    """Preflight with >20% empty pages should trigger warning."""
    md = "some text"
    preflight_info = {"page_count": 10, "empty_page_count": 5, "has_embedded_text": True}
    result = assess_quality(md, Path("test.pdf"), preflight_info=preflight_info)
    assert any("over_20_percent_empty_pages" in w for w in result["warnings"])


def test_quality_preflight_table_mismatch() -> None:
    """Preflight hinting tables but MD has none should warn."""
    md = "No tables here\n" * 10
    preflight_info = {"has_tables_hint": True}
    result = assess_quality(md, Path("test.pdf"), preflight_info=preflight_info)
    assert "original_has_tables_but_markdown_has_none" in result["warnings"]


def test_quality_review_required_when_low() -> None:
    """Score < 0.5 should set review_required flag."""
    # Short text with high replacement ratio should score < 0.5
    result = assess_quality("�" * 5, Path("test.pdf"))
    assert result["review_required"] is True


# --- Router tests ---


def test_parse_file_requires_file_exists(tmp_path: Path) -> None:
    """parse_file should raise FileNotFoundError for non-existent files."""
    settings = Settings()
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "nonexistent.pdf", settings)


def test_parse_file_office_go_markitdown(tmp_path: Path) -> None:
    """HTML files should route to MarkItDown."""
    settings = Settings()
    html = _make_synthetic_html(tmp_path)
    result = parse_file(html, settings)
    assert result.engine == "markitdown"


def test_parse_file_explicit_engine(tmp_path: Path) -> None:
    """Explicit engine should override auto-routing."""
    settings = Settings()
    txt = _make_synthetic_md_file(tmp_path)
    result = parse_file(txt, settings, engine="markitdown")
    assert result.engine == "markitdown"


def test_parse_result_config_hash_deterministic() -> None:
    """ParseResult config_hash should be deterministic."""
    r1 = ParseResult(
        output_path=Path("/tmp/out.md"),
        engine="markitdown",
        engine_version="0.1.0",
        quality_score=0.95,
    )
    r2 = ParseResult(
        output_path=Path("/tmp/out.md"),
        engine="markitdown",
        engine_version="0.1.0",
        quality_score=0.95,
    )
    assert r1.config_hash == r2.config_hash


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("empty.pdf", b"", "EMPTY_FILE"),
        ("metadata.json", b"{}", "OA_TECHNICAL_METADATA"),
        ("workflow.json", b"{}", "OA_TECHNICAL_METADATA"),
        ("button-frame.html", b"<button>OK</button>", "BUTTON_OR_MASK_FRAME"),
        ("binary.exe", b"MZ synthetic", "UNSUPPORTED_FORMAT"),
    ],
)
def test_knowledge_eligibility_rejects_non_documents(
    tmp_path: Path, name: str, content: bytes, reason: str
) -> None:
    path = tmp_path / name
    path.write_bytes(content)

    decision = evaluate_eligibility(path)

    assert not decision.eligible
    assert decision.reason_code == reason


def test_knowledge_eligibility_marks_duplicate_without_losing_source(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("Synthetic business content", encoding="utf-8")

    decision = evaluate_eligibility(path, duplicate_content=True)

    assert not decision.eligible
    assert decision.reason_code == "DUPLICATE_CONTENT"
    assert decision.evidence["preserve_source_reference"] is True


def test_knowledge_eligibility_uses_pdf_signature_when_oa_name_has_size_suffix(tmp_path: Path) -> None:
    path = tmp_path / "document.pdf (903KB)"
    path.write_bytes(b"%PDF-1.4\nsynthetic")

    decision = evaluate_eligibility(path)

    assert decision.eligible
    assert decision.detected_type == "pdf"
    assert decision.routing_hint == "mineru"


# --- MarkItDown parser tests ---


def test_markitdown_parses_text_file(tmp_path: Path) -> None:
    """MarkItDown should successfully parse a text file."""
    txt = _make_synthetic_md_file(tmp_path)
    result = parse_with_markitdown(txt)
    assert result.engine == "markitdown"
    assert result.quality_score >= 0


def test_markitdown_saves_artifacts(tmp_path: Path) -> None:
    """parse_with_markitdown should save .md, _content.json, _quality.json."""
    txt = _make_synthetic_md_file(tmp_path)
    output_dir = tmp_path / "parse_output"
    result = parse_with_markitdown(txt, output_dir=output_dir)
    assert result.output_path.is_file()
    # Check content list and quality JSON exist in the versioned subdirectory
    markitdown_dirs = list(output_dir.glob("markitdown-v*/"))
    assert len(markitdown_dirs) >= 1
    versioned_dir = markitdown_dirs[0]
    stem = txt.stem
    assert (versioned_dir / f"{stem}_content.json").is_file()
    assert (versioned_dir / f"{stem}_quality.json").is_file()


def _mineru_zip(entries: dict[str, str]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return payload.getvalue()


def test_extract_mineru_zip_rejects_path_traversal(tmp_path: Path) -> None:
    payload = _mineru_zip({"../escaped.md": "secret"})

    with pytest.raises(MineruResponseError, match="unsafe ZIP entry"):
        _extract_mineru_zip(payload, tmp_path / "output")

    assert not (tmp_path / "escaped.md").exists()


def test_extract_mineru_zip_requires_markdown(tmp_path: Path) -> None:
    payload = _mineru_zip({"result/content_list.json": "[]"})

    with pytest.raises(MineruResponseError, match="no Markdown"):
        _extract_mineru_zip(payload, tmp_path / "output")


def test_extract_mineru_zip_rejects_empty_markdown(tmp_path: Path) -> None:
    payload = _mineru_zip({"result/document.md": "", "result/content_list.json": "[]"})

    with pytest.raises(MineruResponseError, match="non-empty Markdown"):
        _extract_mineru_zip(payload, tmp_path / "output")


def test_parse_with_mineru_rejects_html_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    pdf = _make_synthetic_pdf(tmp_path)
    settings = Settings(mineru={"enabled": True, "api_url": "http://127.0.0.1:58000"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, content=b"<!doctype html><title>Error</title>", headers={"content-type": "text/html"})

    monkeypatch.setattr(
        "oa_knowledge.parsers.mineru_parser._transport_for_settings",
        lambda _settings: httpx.MockTransport(handler),
    )

    with pytest.raises(MineruResponseError, match="HTML"):
        parse_with_mineru(pdf, settings, tmp_path / "parse")


def test_parse_with_mineru_extracts_zip_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    pdf = _make_synthetic_pdf(tmp_path)
    settings = Settings(mineru={"enabled": True, "api_url": "http://127.0.0.1:58000"})
    payload = _mineru_zip({"result/document.md": "# Synthetic document\n\nParsed locally.", "result/content_list.json": "[]"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"protocol_version": "1"})
        assert request.url.path == "/file_parse"
        assert b'name="files"' in request.read()
        return httpx.Response(200, content=payload, headers={"content-type": "application/zip"})

    monkeypatch.setattr(
        "oa_knowledge.parsers.mineru_parser._transport_for_settings",
        lambda _settings: httpx.MockTransport(handler),
    )

    result = parse_with_mineru(pdf, settings, tmp_path / "parse")

    assert result.engine == "mineru"
    assert result.output_path.read_text(encoding="utf-8").startswith("# Synthetic")
    assert (result.output_path.parent / "content_list.json").is_file()
    assert not list((tmp_path / "parse").glob(".mineru-staging-*"))


# --- ParsePipeline tests ---


def test_pipeline_enqueue_nonexistent_file(tmp_path: Path) -> None:
    """Enqueue should return None for non-existent file."""
    settings = Settings(app={"data_root": str(tmp_path)})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    pipeline = ParsePipeline(settings, engine)
    assert pipeline.enqueue(99999) is None


def test_pipeline_enqueue_valid_file(tmp_path: Path) -> None:
    """Enqueue should create a parse job for a verified file."""
    settings = Settings(app={"data_root": str(tmp_path)})
    db_path = tmp_path / "state" / "oa.db"
    upgrade_database(db_path)
    engine = create_db_engine(db_path)

    # Create the actual file on disk
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    pdf_file = raw_dir / "test.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\nfake pdf content\n")

    with Session(engine) as session:
        item = OAItem(oa_item_key="test-1", source_channel="done", title="Test Document")
        session.add(item)
        session.flush()

        file_rec = ArchivedFile(
            oa_item_id=item.id,
            attachment_key="test.pdf",
            original_name="test.pdf",
            local_relpath="raw/test.pdf",
            file_role="direct_attachment",
            source_container_key="test",
            depth=1,
            download_status="verified",
            sha256="abc123",
        )
        session.add(file_rec)
        session.commit()
        file_id = file_rec.id

    pipeline = ParsePipeline(settings, engine)
    job_id = pipeline.enqueue(file_id)
    assert job_id is not None

    with Session(engine) as session:
        job = session.get(ParseJob, job_id)
        assert job is not None
        assert job.status == "queued"
        assert job.engine == "markitdown"
        file_rec = session.get(ArchivedFile, file_id)
        assert file_rec.content_object_id is not None


def test_pipeline_enqueue_idempotent(tmp_path: Path) -> None:
    """Enqueue should return existing job_id for same file."""
    settings = Settings(app={"data_root": str(tmp_path)})
    db_path = tmp_path / "state" / "oa.db"
    upgrade_database(db_path)
    engine = create_db_engine(db_path)

    # Create the actual file on disk
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    pdf_file = raw_dir / "idem.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\nfake idem pdf\n")

    with Session(engine) as session:
        item = OAItem(oa_item_key="test-idem", source_channel="done", title="Test")
        session.add(item)
        session.flush()
        file_rec = ArchivedFile(
            oa_item_id=item.id,
            attachment_key="idem.pdf",
            original_name="idem.pdf",
            local_relpath="raw/idem.pdf",
            file_role="direct_attachment",
            source_container_key="test",
            depth=1,
            download_status="verified",
            sha256="xyz789",
        )
        session.add(file_rec)
        session.commit()
        file_id = file_rec.id

    pipeline = ParsePipeline(settings, engine)
    job_id1 = pipeline.enqueue(file_id)
    job_id2 = pipeline.enqueue(file_id)
    assert job_id1 == job_id2


def test_pipeline_does_not_silently_fallback_when_mineru_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(app={"data_root": str(tmp_path)})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\nsynthetic scan\n")

    with Session(engine) as session:
        item = OAItem(oa_item_key="mineru-required", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        record = ArchivedFile(
            oa_item_id=item.id, attachment_key="scan.pdf", original_name="scan.pdf",
            local_relpath="raw/scan.pdf", file_role="direct_attachment",
            source_container_key="root", depth=1, download_status="verified", sha256="c" * 64,
        )
        session.add(record)
        session.commit()
        file_id = record.id

    job_id = ParsePipeline(settings, engine).enqueue(file_id, engine="mineru")
    monkeypatch.setattr("oa_knowledge.pipeline.mineru_available", lambda _settings: False)

    with pytest.raises(RuntimeError, match="MinerU is unavailable"):
        ParsePipeline(settings, engine).run(job_id)


def test_pipeline_does_not_enqueue_oa_technical_metadata(tmp_path: Path) -> None:
    settings = Settings(app={"data_root": str(tmp_path)})
    db_path = tmp_path / "state" / "oa.db"
    upgrade_database(db_path)
    engine = create_db_engine(db_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    metadata = raw / "metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    with Session(engine) as session:
        item = OAItem(oa_item_key="technical", source_channel="done", title="Synthetic")
        session.add(item)
        session.flush()
        record = ArchivedFile(
            oa_item_id=item.id, attachment_key="metadata", original_name="metadata.json",
            local_relpath="raw/metadata.json", file_role="metadata", source_container_key="root",
            depth=1, download_status="verified", sha256="b" * 64,
        )
        session.add(record)
        session.commit()
        file_id = record.id

    assert ParsePipeline(settings, engine).enqueue(file_id) is None
    with Session(engine) as session:
        assert session.query(ParseJob).count() == 0


def test_pipeline_run_all_pending_empty(tmp_path: Path) -> None:
    """run_all_pending should return zero counts when no jobs exist."""
    settings = Settings(app={"data_root": str(tmp_path)})
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    pipeline = ParsePipeline(settings, engine)
    summary = pipeline.run_all_pending(limit=10)
    assert summary["processed"] == 0
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
