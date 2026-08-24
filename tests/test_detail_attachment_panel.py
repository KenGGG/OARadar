import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

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


def test_attachment_panel_uses_file_download_api_instead_of_preview_click() -> None:
    observed_query = {}
    panel = """
        <html><body>
          <script>
            function findAttachment(fileId, createDate, fileName, fileType, version) {
              document.getElementById(fileId).href = '/seeyon/officeTrans.do?method=view&fileId=' + fileId;
            }
          </script>
          <a id="file-1" target="downloadFileFrame" onclick="findAttachment('file-1','2026-08-24','synthetic-document','docx','hash-1')">synthetic-document.docx</a>
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

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/seeyon/fileDownload.do?"):
                observed_query.update(parse_qs(urlsplit(self.path).query))
                payload = b"synthetic attachment bytes"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            body = document.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page.goto(f"http://127.0.0.1:{server.server_port}/seeyon/collaboration/collaboration.do")
    adapter = CollaborationDetailAdapter(page)

    try:
        attachments = adapter._download_files(page, "direct_attachment", download_timeout_seconds=5)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        browser.close()
        playwright.stop()

    assert len(attachments) == 1
    assert attachments[0].attachment_key == "file-1"
    assert attachments[0].filename == "synthetic-document.docx"
    assert attachments[0].content == b"synthetic attachment bytes"
    assert attachments[0].download_status == "downloaded"
    assert observed_query == {
        "method": ["download"],
        "v": ["hash-1"],
        "fileId": ["file-1"],
        "createDate": ["2026-08-24"],
        "filename": ["synthetic-document.docx"],
    }


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


def test_meeting_page_hidden_empty_attachment_inventory_is_confirmed() -> None:
    document = """
        <html><body>
          <main>synthetic meeting detail</main>
          <div id="attachmentTRAtt" style="display:none">
            <span id="attachmentNumberDivAtt"></span>
          </div>
          <div id="attachmentAreaAtt"></div>
          <div id="attachmentTRSummary" style="display:none">
            <span id="attachmentNumberDivSummary"></span>
          </div>
        </body></html>
    """
    playwright, browser, page = _browser_page()
    page.context.route("**/seeyon/meeting.do**", lambda route: route.fulfill(
        content_type="text/html", body=document,
    ))
    page.goto("https://oa.synthetic/seeyon/meeting.do?method=view&meetingId=synthetic")
    adapter = CollaborationDetailAdapter(page, inventory_only=True)

    try:
        attachments = adapter._download_files(page, "direct_attachment", download_timeout_seconds=2)
        confirmed_empty = adapter._last_attachment_inventory_confirmed_empty
    finally:
        browser.close()
        playwright.stop()

    assert attachments == ()
    assert confirmed_empty is True
