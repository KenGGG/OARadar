from pathlib import Path

from playwright.sync_api import sync_playwright

from oa_knowledge.collector.done import DoneAdapter


def test_done_adapter_parses_text_identifier_and_columns() -> None:
    html = """
    <table><tr id='row-9223372036854775809'>
      <td><input name='workitemId' value='-9223372036854775809'></td>
      <td>合成测试事项</td><td>2026-07-01 08:00</td><td>2026-07-17 19:09</td>
      <td>合成人员</td><td></td><td>协同</td>
    </tr></table>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page()
        page.set_content(f"<iframe srcdoc=\"{html.replace(chr(34), '&quot;')}\"></iframe>")
        frame = next(frame for frame in page.frames if frame != page.main_frame)
        adapter = DoneAdapter(page)
        adapter.open_list = lambda: frame
        items = adapter.discover_current_page()
        browser.close()
    assert len(items) == 1
    assert items[0].workitem_id_text == "-9223372036854775809"
    assert items[0].oa_item_key == "done:-9223372036854775809"
    assert items[0].category == "协同"


def test_done_adapter_paginates_and_reconciles_source_totals() -> None:
    html = """
    <span id='x_total_number'>条/共4条记录</span><span id='x_total_page'>共2页</span>
    <table><tbody id='rows'>
      <tr><td><input name='workitemId' value='1'></td><td>A</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
      <tr><td><input name='workitemId' value='2'></td><td>B</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
    </tbody></table>
    <a class='pNext' onclick="document.querySelector('#rows').innerHTML=`
      <tr><td><input name='workitemId' value='3'></td><td>C</td><td>2026-07-01</td><td>2026-07-03</td><td>S</td><td></td><td>协同</td></tr>
      <tr><td><input name='workitemId' value='4'></td><td>D</td><td>2026-07-01</td><td>2026-07-03</td><td>S</td><td></td><td>协同</td></tr>`">next</a>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page(); page.set_content(html)
        adapter = DoneAdapter(page); adapter.open_list = lambda: page.main_frame
        result = adapter.discover_pages(4, max_pages=2)
        browser.close()
    assert [item.workitem_id_text for item in result.items] == ["1", "2", "3", "4"]
    assert result.pages_scanned == 2 and result.query_count == 4 and result.scanned_row_count == 4
    assert result.source_total_count == 4 and result.source_total_pages == 2
    assert result.items[-1].list_page == 2


def test_done_discovery_separates_scanned_rows_from_accepted_manifest() -> None:
    html = """
    <span id='x_total_number'>条/共3条记录</span><span id='x_total_page'>共1页</span>
    <table><tbody>
      <tr><td><input name='workitemId' value='1'></td><td>A</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
      <tr><td><input name='workitemId' value='2'></td><td>B</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
      <tr><td><input name='workitemId' value='3'></td><td>C</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
    </tbody></table>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page(); page.set_content(html)
        adapter = DoneAdapter(page); adapter.open_list = lambda: page.main_frame
        result = adapter.discover_pages(2, accept=lambda item: item.workitem_id_text != "2", max_pages=1)
        browser.close()
    assert result.query_count == 2
    assert result.scanned_row_count == 3


def test_done_discovery_deduplicates_workitem_ids_before_query_count() -> None:
    html = """
    <span id='x_total_number'>条/共2条记录</span><span id='x_total_page'>共1页</span>
    <table><tbody>
      <tr><td><input name='workitemId' value='1'></td><td>A</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
      <tr><td><input name='workitemId' value='1'></td><td>A duplicate</td><td>2026-07-01</td><td>2026-07-02</td><td>S</td><td></td><td>协同</td></tr>
    </tbody></table>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page(); page.set_content(html)
        adapter = DoneAdapter(page); adapter.open_list = lambda: page.main_frame
        result = adapter.discover_pages(2, max_pages=1)
        browser.close()
    assert len(result.items) == result.query_count == 1
    assert result.scanned_row_count == 2


def test_full_discovery_uses_oa_page_total_without_static_cap() -> None:
    adapter = object.__new__(DoneAdapter)
    sentinel = object()
    adapter.open_list = lambda: sentinel
    adapter._list_stats = lambda frame: (7974, 399)
    observed = {}
    def fake_discover(limit, **kwargs):
        observed.update(limit=limit, **kwargs)
        return "complete"
    adapter.discover_pages = fake_discover
    assert adapter.discover_all_pages(page_delay_seconds=0.2) == "complete"
    assert observed == {"limit": 7974, "max_pages": 399, "page_delay_seconds": 0.2}


def test_missing_real_identifier_uses_composite_fallback_hash() -> None:
    from datetime import datetime
    from oa_knowledge.collector.done import DiscoveredDoneItem
    item = DiscoveredDoneItem("", "合成标题", None, datetime(2026, 7, 19, 8), "合成人", None, None, 1)
    assert item.oa_item_key.startswith("done:fallback:")
    assert "合成标题" not in item.oa_item_key


def test_done_adapter_searches_title_and_verifies_workitem_id() -> None:
    html = """
    <input id='subject'><button id='search' onclick="document.querySelector('#rows').innerHTML =
      document.querySelector('#subject').value === '目标事项'
      ? `<tr><td><input name='workitemId' value='target-1'></td><td>目标事项</td></tr>` : ''">搜索</button>
    <table><tbody id='rows'><tr><td><input name='workitemId' value='other'></td><td>其他事项</td></tr></tbody></table>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page(); page.set_content(html)
        adapter = DoneAdapter(page); adapter.open_list = lambda: page.main_frame
        located_id = adapter.search_for_item("【文件传阅】目标事项(由合成人原发)", "target-1")
        browser.close()
    assert located_id == "target-1"


def test_navigation_uses_actual_page_number_after_list_reopens() -> None:
    html = """
    <input id='x_page_number' value='1'>
    <table><tbody id='rows'><tr><td><input name='workitemId' value='page-1'></td></tr></tbody></table>
    <a class='pNext' onclick="document.querySelector('#x_page_number').value='2';
      document.querySelector('#rows').innerHTML=`<tr><td><input name='workitemId' value='page-2'></td></tr>`">next</a>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page(); page.set_content(html)
        adapter = DoneAdapter(page); adapter.open_list = lambda: page.main_frame
        adapter._current_list_page = 99
        adapter.navigate_to_page(2)
        current_id = page.locator("input[name='workitemId']").first.get_attribute("value")
        browser.close()
    assert current_id == "page-2"
