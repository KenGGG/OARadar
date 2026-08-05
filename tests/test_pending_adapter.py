import shutil
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from oa_knowledge.collector.pending import PendingAdapter

pytestmark = pytest.mark.skipif(
    shutil.which("google-chrome") is None,
    reason="requires a local google-chrome binary",
)

FIXTURE = Path(__file__).parent / "fixtures" / "pending_list_synthetic.html"


def _page(playwright):
    browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
    page = browser.new_page()
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    return browser, page


def test_pending_adapter_parses_text_ids_and_list_fields() -> None:
    with sync_playwright() as playwright:
        browser, page = _page(playwright)
        adapter = PendingAdapter(page)
        adapter.open_list = lambda: page.main_frame
        item = adapter.discover_current_page(limit=1)[0]
        browser.close()

    assert item.affair_id_text == "922337203685477580812345"
    assert item.occurrence_key == "pending:922337203685477580812345"
    assert item.title == "合成待办甲"
    assert item.sender == "合成人员甲"
    assert item.previous_approver == "合成人员乙"
    assert item.current_node == "部门审核"
    assert item.deadline_text == "2026-07-25 18:00"
    assert item.reminder_count == 1
    assert item.importance == "important"


def test_pending_adapter_paginates_and_reconciles_totals() -> None:
    with sync_playwright() as playwright:
        browser, page = _page(playwright)
        adapter = PendingAdapter(page)
        adapter.open_list = lambda: page.main_frame
        result = adapter.discover_pages(limit=3, max_pages=2)
        browser.close()

    assert [item.affair_id_text for item in result.items] == [
        "922337203685477580812345", "-922337203685477580812346", "pending-3",
    ]
    assert result.pages_scanned == 2
    assert result.scanned_row_count == result.query_count == 3
    assert result.source_total_count == 3
    assert result.source_total_pages == 2


def test_pending_adapter_deduplicates_affair_ids() -> None:
    with sync_playwright() as playwright:
        browser, page = _page(playwright)
        page.locator("#pending_rows").evaluate("""rows => rows.insertAdjacentHTML('beforeend', `
          <tr><td><input type='checkbox' value='922337203685477580812345'></td><td>重复行</td>
          <td></td><td></td><td></td><td></td><td></td><td>0</td><td></td><td></td><td></td></tr>`)""")
        adapter = PendingAdapter(page)
        adapter.open_list = lambda: page.main_frame
        result = adapter.discover_pages(limit=10, max_pages=1)
        browser.close()

    assert len(result.items) == 2
    assert result.scanned_row_count == 3


def test_pending_detail_url_uses_affair_id_and_read_only_origin() -> None:
    adapter = object.__new__(PendingAdapter)
    url = adapter.detail_url("https://oa.synthetic.invalid", "-123")

    assert url == (
        "https://oa.synthetic.invalid/seeyon/collaboration/collaboration.do"
        "?method=summary&openFrom=listPending&affairId=-123&showTab=1"
    )
