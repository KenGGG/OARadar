import subprocess
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

import pytest

from oa_knowledge.config import Settings, load_settings
from oa_knowledge.db.models import ArchivedFile, OAItem
from oa_knowledge.rebuild.paths import (
    _find_project_root,
    archive_file_relpath,
    archive_item_relpath,
    effective_item_date,
    markdown_item_relpath,
    resolve_rebuild_path,
    resolve_rebuild_root,
    safe_component,
)


@pytest.fixture
def item() -> OAItem:
    return OAItem(
        oa_item_key="done:synthetic-item",
        source_channel="done",
        title="合成事项标题",
        document_number="合成文号",
        initiated_at=datetime(2026, 8, 19, tzinfo=UTC),
        completed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


@pytest.fixture
def archived_file() -> ArchivedFile:
    return ArchivedFile(
        original_name="合成附件.pdf",
        attachment_key="synthetic-attachment",
        file_role="attachment",
        source_container_key="synthetic-container",
    )


def test_internal_markdown_path_uses_category_year_month(item: OAItem) -> None:
    item.source_type = "internal"
    item.internal_category = "风险管理"
    item.document_date = date(2026, 8, 20)
    item.classification_state = "confirmed"

    path = markdown_item_relpath(item)

    assert path.parts[:4] == ("markdown", "内部事项", "风险管理", "2026年")
    assert path.parts[4] == "08月"


def test_external_markdown_path_uses_exact_institution(item: OAItem) -> None:
    item.source_type = "external"
    item.external_issuer = "示例市工业和信息化局"
    item.document_date = date(2026, 8, 20)
    item.classification_state = "confirmed"

    assert markdown_item_relpath(item).parts[2] == "示例市工业和信息化局"


def test_archive_path_is_stable_when_classification_changes(
    item: OAItem, archived_file: ArchivedFile,
) -> None:
    before = archive_file_relpath(item, archived_file)
    item.internal_category = "财务资金"
    after = archive_file_relpath(item, archived_file)

    assert before == after
    assert before.parts[:3] == ("archive", "oa", "done")


def test_effective_item_date_prefers_document_then_initiated_then_completed(item: OAItem) -> None:
    assert effective_item_date(item) == date(2026, 8, 19)
    item.document_date = date(2026, 8, 20)
    assert effective_item_date(item) == date(2026, 8, 20)
    item.document_date = None
    item.initiated_at = None
    assert effective_item_date(item) == date(2026, 8, 21)


def test_effective_item_date_rejects_missing_date(item: OAItem) -> None:
    item.initiated_at = None
    item.completed_at = None

    with pytest.raises(ValueError, match="date"):
        effective_item_date(item)


def test_markdown_path_rejects_unconfirmed_or_malformed_classification(item: OAItem) -> None:
    item.source_type = "internal"
    item.internal_category = "风险管理"
    item.document_date = date(2026, 8, 20)

    with pytest.raises(ValueError, match="confirmed"):
        markdown_item_relpath(item)

    item.classification_state = "confirmed"
    item.internal_category = None
    with pytest.raises(ValueError, match="category"):
        markdown_item_relpath(item)


def test_safe_component_sanitizes_forbidden_and_control_characters() -> None:
    assert safe_component('  合成<>:"/\\|?*\x00标题.  ') == "合成标题"


@pytest.mark.parametrize("value", ["", " . ", "\x00<>:/\\|?*"])
def test_safe_component_rejects_empty_result(value: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        safe_component(value)


@pytest.mark.parametrize("max_chars", (0, -1))
def test_safe_component_rejects_non_positive_limits_explicitly(max_chars: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        safe_component("合成", max_chars=max_chars)


def test_safe_component_limits_multibyte_names_by_utf8_bytes() -> None:
    component = safe_component("甲" * 200)

    assert len(component.encode("utf-8")) <= 240
    assert component


def test_archive_item_folder_truncates_title_before_stable_key(item: OAItem) -> None:
    item.title = "甲" * 100
    folder = archive_item_relpath(item).parts[-1]

    assert folder.startswith("20260819-合成文号-")
    assert folder.endswith("--05e0f449")
    assert "甲" * 97 not in folder


def test_complete_item_folder_fits_byte_budget_with_long_number_and_title(
    item: OAItem,
) -> None:
    item.document_number = "文号" * 100
    item.title = "事项标题" * 100

    folder = archive_item_relpath(item).parts[-1]

    assert len(folder.encode("utf-8")) <= 240
    assert folder.startswith("20260819-")
    assert folder.endswith("--05e0f449")


def test_blank_document_number_is_treated_as_absent(item: OAItem) -> None:
    item.document_number = " \t "

    folder = archive_item_relpath(item).parts[-1]

    assert folder.startswith("20260819-合成事项标题--")
    assert "20260819--" not in folder


def test_item_path_builders_accept_a_configured_title_limit(item: OAItem) -> None:
    item.title = "甲" * 100
    item.source_type = "internal"
    item.internal_category = "风险管理"
    item.classification_state = "confirmed"

    settings = Settings(rebuild={"item_title_max_chars": 8})
    archive_folder = archive_item_relpath(
        item, item_title_max_chars=settings.rebuild.item_title_max_chars,
    ).parts[-1]
    markdown_folder = markdown_item_relpath(
        item, item_title_max_chars=settings.rebuild.item_title_max_chars,
    ).parts[-1]

    assert "甲" * 8 in archive_folder
    assert "甲" * 9 not in archive_folder
    assert markdown_folder == archive_folder


def test_archive_file_paths_do_not_collide_after_name_sanitization(item: OAItem) -> None:
    first = ArchivedFile(
        original_name="合成?.pdf", attachment_key="first", file_role="attachment", source_container_key="container",
    )
    second = ArchivedFile(
        original_name="合成*.pdf", attachment_key="second", file_role="attachment", source_container_key="container",
    )

    first_path = archive_file_relpath(item, first)
    second_path = archive_file_relpath(item, second)

    assert first_path != second_path
    assert first_path.name.startswith("合成--")
    assert second_path.name.startswith("合成--")
    assert first_path.suffix == ".pdf"
    assert second_path.suffix == ".pdf"


def test_archive_file_paths_do_not_collide_for_exact_duplicate_names(item: OAItem) -> None:
    first = ArchivedFile(
        original_name="合成.pdf", attachment_key="first", file_role="attachment", source_container_key="container",
    )
    second = ArchivedFile(
        original_name="合成.pdf", attachment_key="second", file_role="attachment", source_container_key="container",
    )

    first_path = archive_file_relpath(item, first)
    second_path = archive_file_relpath(item, second)

    assert first_path != second_path
    assert archive_file_relpath(item, first) == first_path


def test_archive_file_name_retains_suffix_and_hash_within_byte_budget(
    item: OAItem,
) -> None:
    source = ArchivedFile(
        original_name=f"{'长' * 200}.pdf",
        attachment_key="long-name",
        file_role="attachment",
        source_container_key="container",
    )

    name = archive_file_relpath(item, source).name

    assert len(name.encode("utf-8")) <= 240
    assert name.endswith("--900b2bd9.pdf")
    assert PurePosixPath(name).suffix == ".pdf"


def test_archive_file_name_preserves_a_multibyte_stem_with_a_long_extension(
    item: OAItem,
) -> None:
    source = ArchivedFile(
        original_name=f"合.{('x' * 400)}",
        attachment_key="long-extension",
        file_role="attachment",
        source_container_key="container",
    )

    name = archive_file_relpath(item, source).name

    assert len(name.encode("utf-8")) <= 240
    assert name.startswith("合--")
    assert "--7cff74c4." in name


def test_resolve_rebuild_root_is_a_sibling_of_live_data(tmp_path: Path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})

    assert resolve_rebuild_root(settings) == (tmp_path / "data_rebuilt").resolve()


def _init_git_repository(path: Path, ignore_text: str = "") -> None:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / ".gitignore").write_text(ignore_text, encoding="utf-8")


def test_default_in_repository_rebuild_target_is_allowed_when_fully_ignored(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-repository"
    _init_git_repository(repository, "/data_rebuilt/\n")
    settings = Settings(app={"data_root": repository / "data"})

    assert resolve_rebuild_root(settings) == repository / "data_rebuilt"


def test_custom_in_repository_rebuild_target_must_be_fully_ignored(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-repository"
    _init_git_repository(repository, "/data_rebuilt/\n")
    settings = Settings(
        app={"data_root": repository / "data"},
        rebuild={"target_root": repository / "custom-rebuild"},
    )

    with pytest.raises(ValueError, match="Git-ignored"):
        resolve_rebuild_root(settings)


def test_custom_in_repository_rebuild_target_is_allowed_when_fully_ignored(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "synthetic-repository"
    _init_git_repository(repository, "/custom-rebuild/\n")
    settings = Settings(
        app={"data_root": repository / "data"},
        rebuild={"target_root": repository / "custom-rebuild"},
    )

    assert resolve_rebuild_root(settings) == repository / "custom-rebuild"


def test_resolve_rebuild_root_is_anchored_to_live_data_when_cwd_changes(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert resolve_rebuild_root(settings) == (tmp_path / "data_rebuilt").resolve()


def test_load_settings_anchors_relative_data_and_rebuild_roots_to_config_path(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path = tmp_path / "project" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        "app:\n  data_root: data\nrebuild:\n  target_root: ../data_rebuilt\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)
    settings = load_settings(config_path)

    assert settings.data_root == config_path.parent / "data"
    assert resolve_rebuild_root(settings) == config_path.parent / "data_rebuilt"


def test_resolve_rebuild_root_rejects_actual_repository_root_when_cwd_changes(tmp_path: Path, monkeypatch) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    settings = Settings(
        app={"data_root": tmp_path / "data"},
        rebuild={"target_root": repository_root},
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    with pytest.raises(ValueError, match="repository root"):
        resolve_rebuild_root(settings)


def test_project_root_discovery_returns_none_without_a_project_marker(tmp_path: Path) -> None:
    nested = tmp_path / "installed" / "package"
    nested.mkdir(parents=True)

    assert _find_project_root(nested) is None


@pytest.mark.parametrize("target_root", [".", "data", "data/child"])
def test_resolve_rebuild_root_rejects_live_root_or_descendants(tmp_path: Path, target_root: str) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"}, rebuild={"target_root": target_root})

    with pytest.raises(ValueError, match="outside"):
        resolve_rebuild_root(settings)


@pytest.mark.parametrize("relpath", ["/tmp/escape", "../escape", "archive/../escape", "archive/\x00file"])
def test_resolve_rebuild_path_rejects_unsafe_relative_paths(tmp_path: Path, relpath: str) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})

    with pytest.raises(ValueError, match="safe relative"):
        resolve_rebuild_path(settings, relpath)


def test_resolve_rebuild_path_returns_target_descendant(tmp_path: Path) -> None:
    settings = Settings(app={"data_root": tmp_path / "data"})

    assert resolve_rebuild_path(settings, PurePosixPath("archive/oa/done/file.pdf")) == (
        tmp_path / "data_rebuilt/archive/oa/done/file.pdf"
    ).resolve()
