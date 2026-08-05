from pathlib import Path

import pytest
import yaml

from oa_knowledge.markdown_export.publisher import PublicationError, publish_markdown
from oa_knowledge.markdown_export.render import ExportMetadata, render_markdown
from oa_knowledge.markdown_export.service import needs_conversion, rewrite_parser_asset_links, sanitize_parser_markdown, terminal_source_error


def metadata(**changes) -> ExportMetadata:
    values = dict(source_relpath="done/OA-SYNTHETIC/报告.pdf", source_filename="报告.pdf", source_sha256="a" * 64,
                  source_size_bytes=10, source_file_id=7, source_channel="done", oa_item_key="done:synthetic",
                  logical_item_id="synthetic", parse_status="success", parse_engine="markitdown",
                  parse_engine_version="1", parse_config_hash="b" * 64)
    values.update(changes)
    return ExportMetadata(**values)


def test_rendered_markdown_has_stable_safe_frontmatter() -> None:
    first = render_markdown(metadata(), "正文")
    second = render_markdown(metadata(), "正文")
    header = yaml.safe_load(first.split("---", 2)[1])
    assert header["id"] == "oa-file:7"
    assert header["schema_version"] == "oa-markdown-v1"
    assert header["managed_by"] == "oaradar"
    assert header["source_relpath"] == "done/OA-SYNTHETIC/报告.pdf"
    assert header["id"] == yaml.safe_load(second.split("---", 2)[1])["id"]


@pytest.mark.parametrize("change", [
    {"source_sha256": "c" * 64}, {"parse_engine_version": "2"},
    {"parse_config_hash": "d" * 64}, {"schema_version": "v2"}, {"status": "failed"},
])
def test_incremental_rebuild_conditions(change: dict) -> None:
    prior = {"source_sha256": "a" * 64, "parse_engine": "markitdown", "parse_engine_version": "1",
             "parse_config_hash": "b" * 64, "schema_version": "oa-markdown-v1", "status": "success"}
    assert needs_conversion(prior | change, "a" * 64, "markitdown", "1", "b" * 64, "oa-markdown-v1")
    assert not needs_conversion(prior, "a" * 64, "markitdown", "1", "b" * 64, "oa-markdown-v1")
    assert needs_conversion(prior, "a" * 64, "markitdown", "1", "b" * 64, "oa-markdown-v1", force=True)


def test_publish_rejects_missing_asset_and_preserves_previous(tmp_path: Path) -> None:
    destination = tmp_path / "报告.pdf.md"
    destination.write_text("previous", encoding="utf-8")
    with pytest.raises(PublicationError):
        publish_markdown(destination, "---\nsource_sha256: " + "a" * 64 + "\n---\n![](报告.pdf.assets/missing.png)\n", "a" * 64)
    assert destination.read_text(encoding="utf-8") == "previous"


def test_publish_accepts_self_contained_data_image(tmp_path: Path) -> None:
    destination = tmp_path / "报告.docx.md"
    content = "---\nsource_sha256: " + "a" * 64 + "\n---\n![](data:image/png;base64,c3ludGhldGlj)\n"
    publish_markdown(destination, content, "a" * 64)
    assert destination.exists()


def test_publish_rejects_non_image_data_uri(tmp_path: Path) -> None:
    destination = tmp_path / "报告.docx.md"
    content = "---\nsource_sha256: " + "a" * 64 + "\n---\n![](data:text/html;base64,c3ludGhldGlj)\n"
    with pytest.raises(PublicationError, match="unsafe image URI"):
        publish_markdown(destination, content, "a" * 64)


def test_parser_placeholder_images_become_explicit_text() -> None:
    source = "before ![diagram](data:image/x-wmf;base64...) after"
    result = sanitize_parser_markdown(source)
    assert "data:image" not in result
    assert "图片未嵌入" in result


def test_publish_frontmatter_allows_triple_dash_inside_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "synthetic.md"
    content = "---\nsource_sha256: " + "a" * 64 + "\nsource_relpath: done/synthetic---name.txt\n---\nbody\n"
    publish_markdown(destination, content, "a" * 64)
    assert destination.exists()


def test_corrupted_or_encrypted_source_is_terminal_not_retryable() -> None:
    assert terminal_source_error(RuntimeError("corrupted_file")) == "CORRUPTED_FILE"
    assert terminal_source_error(RuntimeError("encrypted_document")) == "ENCRYPTED_DOCUMENT"
    assert terminal_source_error(RuntimeError("temporary parser outage")) is None


def test_parser_assets_are_rewritten_to_published_assets_directory(tmp_path: Path) -> None:
    assets = tmp_path / "parse"; (assets / "images").mkdir(parents=True); (assets / "images" / "synthetic.png").write_bytes(b"png")
    destination = tmp_path / "report.pdf.md"
    body = rewrite_parser_asset_links("![](images/synthetic.png)", destination, assets)
    assert body == "![](report.pdf.assets/images/synthetic.png)"


def test_missing_parser_asset_becomes_explicit_text(tmp_path: Path) -> None:
    assets = tmp_path / "parse"; assets.mkdir()
    body = rewrite_parser_asset_links("before ![](images/missing.png) after", tmp_path / "report.pdf.md", assets)
    assert "images/missing.png" not in body
    assert "图片未包含在解析结果中" in body


def test_first_failure_can_publish_explicit_stub(tmp_path: Path) -> None:
    destination = tmp_path / "example.bin.md"
    content = render_markdown(metadata(source_filename="example.bin", source_relpath="done/example.bin", parse_status="unsupported",
                                       parse_engine="none", last_error_code="UNSUPPORTED_FILE_TYPE"), "该文件暂不支持内容解析。")
    publish_markdown(destination, content, "a" * 64)
    assert "UNSUPPORTED_FILE_TYPE" in destination.read_text(encoding="utf-8")
