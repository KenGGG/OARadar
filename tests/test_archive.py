from datetime import datetime, timezone
from pathlib import Path

import pytest

from oa_knowledge.archive.integrity import inspect_file
from oa_knowledge.archive.manifest import ContainerManifest, ItemManifest
from oa_knowledge.archive.naming import safe_filename, validate_relative_path
from oa_knowledge.archive.writer import atomic_commit


def test_safe_filename_and_relative_path() -> None:
    assert safe_filename('a/b:c?.pdf') == "a_b_c_.pdf"
    assert str(validate_relative_path("raw/2026/document.pdf")) == "raw/2026/document.pdf"
    for value in ("../secret", "/etc/passwd", "C:\\secret"):
        with pytest.raises(ValueError):
            validate_relative_path(value)
    unicode_name = safe_filename("中文标题" * 100 + ".pdf", 100)
    assert len(unicode_name.encode("utf-8")) <= 100
    assert unicode_name.endswith(".pdf")


def test_integrity_rejects_empty_html_and_fake_pdf(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert inspect_file(empty, "pdf").status == "rejected_zero_byte"
    html = tmp_path / "login.pdf"
    html.write_text("<!doctype html><html>login</html>", encoding="utf-8")
    assert inspect_file(html, "pdf").status == "rejected_error_page"
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not-a-pdf")
    assert inspect_file(fake, "pdf").status == "rejected_type_mismatch"


def test_integrity_accepts_html_attachment_but_rejects_login_html(tmp_path: Path) -> None:
    form = tmp_path / "form.htm"
    form.write_text("<!doctype html><html><body>合成呈批表内容</body></html>", encoding="utf-8")
    login = tmp_path / "login.htm"
    login.write_text("<html><body><input id='login_username'></body></html>", encoding="utf-8")

    assert inspect_file(form, "html_attachment").status == "verified"
    assert inspect_file(login, "html_attachment").status == "rejected_error_page"


def test_valid_pdf_and_atomic_commit(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")
    result = inspect_file(source, "pdf")
    assert result.valid and len(result.sha256 or "") == 64
    destination = atomic_commit(source, tmp_path / "data", "raw/item/file.pdf")
    assert destination.read_bytes() == source.read_bytes()


def test_manifest_depth_limit() -> None:
    manifest = ItemManifest(
        oa_item_key="synthetic",
        workitem_id_text="-9223372036854775809",
        title="synthetic",
        captured_at=datetime.now(timezone.utc),
        containers=[ContainerManifest(container_key="depth-10", page_family="govdoc", depth=10, has_unvisited_children=True)],
    )
    assert manifest.depth_limit_reached
    with pytest.raises(ValueError):
        ContainerManifest(container_key="bad", page_family="govdoc", depth=11)


def test_unvisited_children_before_limit_rejected() -> None:
    with pytest.raises(ValueError, match="depth-limit"):
        ContainerManifest(container_key="bad", page_family="govdoc", depth=4, has_unvisited_children=True)


def test_ten_level_container_tree_reconciles_and_flags_unvisited_branch() -> None:
    containers = []
    for depth in range(1, 11):
        containers.append(ContainerManifest(
            container_key=f"level-{depth}",
            parent_container_key=f"level-{depth - 1}" if depth > 1 else None,
            page_family="collaboration" if depth == 1 else "govdoc",
            depth=depth,
            child_container_count=1 if depth < 10 else 0,
            has_unvisited_children=depth == 10,
        ))
    manifest = ItemManifest(
        oa_item_key="ten-level", workitem_id_text="123", title="synthetic",
        captured_at=datetime.now(timezone.utc), containers=containers,
    )
    assert manifest.depth_limit_reached
    containers[5].child_container_count = 0
    with pytest.raises(ValueError, match="child container count"):
        ItemManifest(
            oa_item_key="bad-tree", workitem_id_text="123", title="synthetic",
            captured_at=datetime.now(timezone.utc), containers=containers,
        )
