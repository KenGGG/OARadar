from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from oa_knowledge.parsers import format_router
from oa_knowledge.parsers.format_router import detect_format, parser_attempts

_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512


def test_detector_uses_signature_for_decorated_pdf_name(tmp_path: Path) -> None:
    source = tmp_path / "通知.PDF (1M)"
    source.write_bytes(b"%PDF-1.7\nsynthetic")

    decision = detect_format(source)

    assert decision.actual_file_type == "pdf"
    assert decision.detection_source == "file_signature"
    assert decision.filename_normalized is True
    assert parser_attempts(decision, mineru_enabled=True) == ("mineru", "markitdown")


@pytest.mark.parametrize(
    ("stream_names", "actual_type", "attempts"),
    [
        ({"WordDocument"}, "doc", ("markitdown", "wv")),
        ({"Workbook"}, "xls", ("markitdown", "libreoffice")),
        ({"Book"}, "xls", ("markitdown", "libreoffice")),
        ({"PowerPoint Document"}, "ppt", ("markitdown",)),
        (set(), "unknown_ole", ()),
    ],
)
def test_detector_distinguishes_ole_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_names: set[str],
    actual_type: str,
    attempts: tuple[str, ...],
) -> None:
    source = tmp_path / "附件.doc (6M)"
    source.write_bytes(_OLE_HEADER)
    monkeypatch.setattr(format_router, "_ole_stream_names", lambda _path: stream_names)

    decision = detect_format(source)

    assert decision.actual_file_type == actual_type
    assert decision.detection_source == "ole_stream"
    assert parser_attempts(decision, mineru_enabled=True) == attempts


def test_detector_identifies_docx_despite_target_suffix(tmp_path: Path) -> None:
    source = tmp_path / "附件.docx_ target="
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")

    decision = detect_format(source)

    assert decision.actual_file_type == "docx"
    assert decision.detection_source == "office_container"
    assert decision.filename_normalized is True
    assert parser_attempts(decision, mineru_enabled=True) == ("markitdown",)


@pytest.mark.parametrize(
    ("name", "actual_type", "status_code"),
    [
        ("附件.rar", "rar", "archive_container_unsupported"),
        ("附件.7z", "7z", "archive_container_unsupported"),
        ("附件.ofd", "ofd", "unsupported_file_type"),
        ("附件.mp4", "mp4", "metadata_only"),
    ],
)
def test_detector_reports_precise_non_document_statuses(
    tmp_path: Path, name: str, actual_type: str, status_code: str
) -> None:
    source = tmp_path / name
    source.write_bytes(b"synthetic")

    decision = detect_format(source)

    assert decision.actual_file_type == actual_type
    assert decision.status_code == status_code


def test_bmp_is_a_mineru_first_visual_format(tmp_path: Path) -> None:
    source = tmp_path / "图片.bmp"
    source.write_bytes(b"BM" + b"\x00" * 20)

    decision = detect_format(source)

    assert decision.actual_file_type == "bmp"
    assert parser_attempts(decision, mineru_enabled=True) == ("mineru", "markitdown")
