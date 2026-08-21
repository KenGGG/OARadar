from __future__ import annotations

from pathlib import Path


def test_v2_overview_component_has_its_required_styles() -> None:
    root = Path(__file__).parents[1]
    source = (root / "webui/src/views/SimpleOverviewView.tsx").read_text(encoding="utf-8")
    styles = (root / "webui/src/styles.css").read_text(encoding="utf-8")

    for class_name in ("simple-overview", "simple-banner", "simple-card-grid", "simple-card", "simple-metrics"):
        assert class_name in source
        assert f".{class_name}" in styles


def test_markdown_view_reports_an_outdated_web_api() -> None:
    source = (Path(__file__).parents[1] / "webui/src/App.tsx").read_text(encoding="utf-8")

    assert "Array.isArray(result.items)" in source
    assert "Markdown 页面需要 V2 Web API" in source


def test_rebuild_page_exposes_three_review_groups() -> None:
    source = Path("webui/src/views/RebuildClassificationView.tsx").read_text()

    assert all(label in source for label in ("内部事项", "外部事项", "待确认事项"))
    assert "确认全部明确的内部事项" in source
    assert "确认全部明确的外部事项" in source
