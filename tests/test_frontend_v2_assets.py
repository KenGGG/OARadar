from __future__ import annotations

from pathlib import Path


def test_v2_overview_component_has_its_required_styles() -> None:
    root = Path(__file__).parents[1]
    source = (root / "webui/src/views/SimpleOverviewView.tsx").read_text(encoding="utf-8")
    styles = (root / "webui/src/styles.css").read_text(encoding="utf-8")

    for class_name in ("simple-overview", "simple-banner", "simple-card-grid", "simple-card", "simple-metrics"):
        assert class_name in source
        assert f".{class_name}" in styles


def test_v2_overview_explains_manifest_download_progress_before_oa_total_is_known() -> None:
    source = (Path(__file__).parents[1] / "webui/src/views/SimpleOverviewView.tsx").read_text(encoding="utf-8")

    assert "已发现" in source
    assert "待下载" in source
    assert "正在扫描更多页面" in source


def test_v2_overview_groups_all_manifest_download_counters_together() -> None:
    source = (Path(__file__).parents[1] / "webui/src/views/SimpleOverviewView.tsx").read_text(encoding="utf-8")

    for label in ("已发现", "原件完整", "待下载", "已排除"):
        assert label in source


def test_done_view_labels_the_sync_timestamp_without_claiming_it_is_an_oa_update() -> None:
    source = (Path(__file__).parents[1] / "webui/src/views/SimpleDoneView.tsx").read_text(encoding="utf-8")

    assert "最近成功同步" in source
    assert "最后更新" not in source


def test_done_view_supports_direct_page_jump_and_last_page() -> None:
    source = (Path(__file__).parents[1] / "webui/src/views/SimpleDoneView.tsx").read_text(encoding="utf-8")

    assert 'aria-label="跳转页码"' in source
    assert ">跳转</button>" in source
    assert ">末页</button>" in source
    assert "Math.min(pages, Math.max(1" in source


def test_done_list_hides_internal_item_id_from_the_title_cell_and_uses_compact_spacing() -> None:
    root = Path(__file__).parents[1]
    source = (root / "webui/src/views/SimpleDoneView.tsx").read_text(encoding="utf-8")
    styles = (root / "webui/src/styles.css").read_text(encoding="utf-8")

    assert '<td className="title-cell"><strong>{row.title}</strong></td>' in source
    assert "done-table-wrap" in source
    assert ".done-table-wrap td" in styles


def test_markdown_view_reports_an_outdated_web_api() -> None:
    source = (Path(__file__).parents[1] / "webui/src/App.tsx").read_text(encoding="utf-8")

    assert "Array.isArray(result.items)" in source
    assert "Markdown 页面需要 V2 Web API" in source
