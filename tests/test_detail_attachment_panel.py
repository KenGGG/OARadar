import base64
import shutil

import pytest
from playwright.sync_api import sync_playwright

from oa_knowledge.collector.detail import CollaborationDetailAdapter


pytestmark = pytest.mark.skipif(
    shutil.which("google-chrome") is None,
    reason="requires a local google-chrome binary",
)


def _browser_page():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
    context = browser.new_context(accept_downloads=True)
    return playwright, browser, context.new_page()


def test_blank_direct_collaboration_detail_is_rejected_for_list_fallback() -> None:
    playwright, browser, list_page = _browser_page()
    list_page.context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>"))
    adapter = CollaborationDetailAdapter(list_page, inventory_only=True)

    try:
        with pytest.raises(LookupError, match="blank|inventory"):
            adapter.capture_direct("https://oa.synthetic", "workitem-synthetic")
    finally:
        browser.close()
        playwright.stop()


def test_attachment_panel_find_attachment_link_is_downloaded() -> None:
    payload = base64.b64encode(b"synthetic attachment bytes").decode("ascii")
    panel = f"""
        <html><body>
          <script>
            function findAttachment() {{
              const anchor = document.createElement('a');
              anchor.href = 'data:application/octet-stream;base64,{payload}';
              anchor.download = 'synthetic-document.docx';
              document.body.appendChild(anchor);
              anchor.click();
              anchor.remove();
            }}
          </script>
          <a href="#" onclick="findAttachment('file-1','2026-08-24','synthetic-document','docx','hash-1'); return false">synthetic-document.docx</a>
        </body></html>
    """
    document = f"""
        <html><body>
          <main>synthetic OA detail</main>
          <button title="查看附件列表" onclick="document.querySelector('iframe').style.display='block'">attachments</button>
          <iframe style="display:none" srcdoc="{panel.replace('&', '&amp;').replace('"', '&quot;')}"></iframe>
        </body></html>
    """
    playwright, browser, page = _browser_page()
    page.set_content(document)
    adapter = CollaborationDetailAdapter(page)

    try:
        attachments = adapter._download_files(page, "direct_attachment", download_timeout_seconds=5)
    finally:
        browser.close()
        playwright.stop()

    assert len(attachments) == 1
    assert attachments[0].attachment_key == "file-1"
    assert attachments[0].filename == "synthetic-document.docx"
    assert attachments[0].content == b"synthetic attachment bytes"
    assert attachments[0].download_status == "downloaded"


def test_attachment_panel_explicit_empty_message_is_confirmed() -> None:
    document = """
        <html><body>
          <button title="查看附件列表" onclick="document.querySelector('iframe').style.display='block'">attachments</button>
          <iframe style="display:none" srcdoc="<html><body><div>无附件显示</div><button>批量下载</button></body></html>"></iframe>
        </body></html>
    """
    playwright, browser, page = _browser_page()
    page.set_content(document)
    adapter = CollaborationDetailAdapter(page, inventory_only=True)

    try:
        attachments = adapter._download_files(page, "direct_attachment", download_timeout_seconds=2)
        confirmed_empty = adapter._last_attachment_inventory_confirmed_empty
    finally:
        browser.close()
        playwright.stop()

    assert attachments == ()
    assert confirmed_empty is True
