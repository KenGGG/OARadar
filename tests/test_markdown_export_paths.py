from pathlib import Path, PureWindowsPath

import pytest

from oa_knowledge.markdown_export.paths import markdown_path_for_source


@pytest.mark.parametrize("name", ["报告.pdf", "报告.docx", "有 空格#1.xlsx"])
def test_markdown_path_mirrors_unicode_source_and_appends_md(tmp_path: Path, name: str) -> None:
    raw = tmp_path / "archive" / "raw" / "oa"
    source = raw / "done" / "2026" / "07" / "OA-1" / "attachments" / name
    target = markdown_path_for_source(source, raw, tmp_path / "workspace" / "raw" / "sources" / "oa")
    assert target.relative_to(tmp_path).as_posix() == (
        f"workspace/raw/sources/oa/done/2026/07/OA-1/attachments/{name}.md"
    )


def test_same_stem_different_extensions_do_not_collide(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    markdown = tmp_path / "md"
    assert markdown_path_for_source(raw / "a.pdf", raw, markdown) != markdown_path_for_source(raw / "a.docx", raw, markdown)


def test_windows_drive_and_unc_sources_are_mirrored_portably(tmp_path: Path) -> None:
    assert markdown_path_for_source(PureWindowsPath("C:/OA/Done/报告.PDF"), PureWindowsPath("c:/oa"), tmp_path / "md") == tmp_path / "md/Done/报告.PDF.md"
    assert markdown_path_for_source(PureWindowsPath("//server/share/raw/a.docx"), PureWindowsPath("//server/share/raw"), tmp_path / "md") == tmp_path / "md/a.docx.md"


@pytest.mark.parametrize("source", [Path("../escape.pdf"), Path("/outside.pdf"), PureWindowsPath("C:/outside.pdf"), PureWindowsPath("//server/share/a.pdf")])
def test_markdown_path_rejects_unsafe_or_outside_source(tmp_path: Path, source) -> None:
    with pytest.raises(ValueError):
        markdown_path_for_source(source, tmp_path / "raw", tmp_path / "md")
