"""Real browser interactions using only synthetic HTTP responses.

Run after npm run build with OARADAR_BROWSER_TESTS=1. No OA connection is made.
"""
import functools
import json
import os
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from oa_knowledge.config import load_settings
from oa_knowledge.web.console_views import settings_view

pytestmark = pytest.mark.skipif(os.environ.get("OARADAR_BROWSER_TESTS") != "1", reason="Opt in to synthetic Chrome UI tests with OARADAR_BROWSER_TESTS=1")


@pytest.fixture
def ui(config_file):
    assets = Path(__file__).parents[1] / "src/oa_knowledge/web/static"
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(assets)))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    requests = []
    form = settings_view(load_settings(config_file))
    state = {"auth": True, "cleaned": False, "fail_action": False}
    summary = {"status": "working", "headline": "Synthetic workflow", "enabled": True, "total": 105, "eligible": 105, "complete": 1, "pending": 104, "failed": 0, "excluded": 0, "last_success_at": None, "last_scan_at": None, "next_run_at": None}
    def handle(route):
        url = urlsplit(route.request.url)
        if url.hostname != "127.0.0.1":
            route.abort(); return
        if not url.path.startswith("/api/"):
            route.continue_(); return
        path = url.path; method = route.request.method
        requests.append((method, path, parse_qs(url.query), route.request.post_data))
        status = 200
        if path == "/api/auth/status":
            data = {"required": not state["auth"], "authenticated": state["auth"]}
        elif path == "/api/auth/login":
            state["auth"] = True; data = {}
        elif path == "/api/settings":
            if method == "POST": status, data = 405, {"detail": "PATCH required"}
            elif method == "PATCH": data = {**form, "restart_required": True}
            else: data = form
        elif path == "/api/policies": data = []
        elif path == "/api/simple-status":
            data = {"generated_at": None, "overall_status": "attention", "archive": summary, "markdown": summary,
                    "done": {}, "pending": {"status": "not_run", "headline": "等待首次扫描", "oa_pending_count": 1, "feishu_sent": 0, "feishu_failed": 0, "feishu_unknown": 1, "model_failed": 0, "model_fallback": 0},
                    "oa_activity": {"status": "unknown", "label": "合成测试", "detail": "未连接 OA", "heartbeat_at": None},
                    "attention": [{"label": "归档异常", "severity": "error", "jump": "done", "filter": "attention"}], "local_delivery": {"available": False}}
        elif path == "/api/done-archives":
            data = {"items": [], "total": 0, "metrics": {"oa_done_total": 0, "downloaded_items": 0, "verified_attachments": 0}}
        elif path == "/api/markdown-outputs":
            page = int(parse_qs(url.query).get("page", [1])[0])
            data = {"items": [{"id": page, "title": f"Synthetic page {page}", "source_type": "internal", "internal_category": "Synthetic", "markdown_count": 1, "delivery_status": "部分交付", "delivery": {"successful": 1, "expected": 2, "unsupported": 0, "failed": 1, "status": "partial"}}], "item_total": 105}
        elif path.startswith("/api/markdown-outputs/items/"):
            data = {"id": 2, "manifest_id": 42, "title": "Synthetic detail", "delivery_status": "部分交付", "delivery": {"status": "partial", "successful": 1, "expected": 2, "index_status": "success", "classification_status": "classified"}, "files": [], "documents": [], "tasks": [], "can_retry": True}
        elif path == "/api/done-archives/42":
            data = {"id": 2, "manifest_id": 42, "title": "Synthetic archive", "archive_status": "downloaded", "files": [], "documents": [], "tasks": [], "delivery": None}
        elif path == "/api/pending-notifications":
            data = {"items": [{"id": 1, "title": "Synthetic pending", "summary_status": "current", "feishu_status": "unknown", "cleanup_status": "not_eligible"}], "total": 1}
        elif path == "/api/pending-notifications/1":
            data = {"id": 1, "delivery_id": 9, "title": "Synthetic pending", "feishu_status": state.get("delivery", "unknown"), "cleanup_status": "cleaned" if state["cleaned"] else "not_eligible", "occurrence_status": "cleaned" if state["cleaned"] else "active", "requires_delivery_reconciliation": "delivery" not in state, "can_retry_delivery": False, "can_retry_summary": False, "can_cleanup": False, "stages": {}, "ollama_summary": {"summary": "Synthetic retained body", "key_points": []}}
        elif path == "/api/pending-notifications/1/reconcile-delivery":
            state["delivery"] = route.request.post_data_json["outcome"]
            data = {"delivery_id": 9, "status": state["delivery"]}
        else:
            status, data = 404, {"detail": "Unexpected synthetic route"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data, default=str), headers={"Set-Cookie": "oa_csrf=synthetic; Path=/; SameSite=Strict"})
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("OARADAR_TEST_CHROME", "/usr/bin/google-chrome"))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", handle)
        try:
            yield page, f"http://127.0.0.1:{server.server_port}/", requests, state
        finally:
            browser.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert errors == []


def test_settings_save_uses_patch_and_warns_about_restart(ui):
    page, url, requests, _ = ui
    page.goto(url + "#view=settings")
    page.get_by_label("启用 Source Markdown 生成").uncheck()
    page.get_by_role("button", name="保存设置").first.click()
    expect(page.get_by_text("设置已保存").first).to_be_visible()
    saved = [entry for entry in requests if entry[:2] == ("PATCH", "/api/settings")]
    assert len(saved) == 1
    assert json.loads(saved[0][3])["markdown_export"]["enabled"] is False
    assert not any(entry[:2] == ("POST", "/api/settings") for entry in requests)


def test_markdown_paginates_resets_search_and_restores_url(ui):
    page, url, requests, _ = ui
    page.goto(url + "#view=markdown")
    page.get_by_role("button", name="下一页").click()
    expect(page.get_by_text("Synthetic page 2")).to_be_visible()
    page.reload()
    expect(page.get_by_text("Synthetic page 2")).to_be_visible()
    page.get_by_role("textbox", name="搜索 Markdown 事项").fill("search")
    expect(page.get_by_text("Synthetic page 1")).to_be_visible()
    assert "page=2" not in page.url
    assert any(q.get("query") == ["search"] and q.get("page") == ["1"] for _, path, q, _ in requests if path == "/api/markdown-outputs")


def test_attention_jump_applies_server_filter(ui):
    page, url, requests, _ = ui
    page.goto(url)
    page.get_by_role("button", name="归档异常 去处理").click()
    expect(page.get_by_role("heading", name="已办资料").first).to_be_visible()
    assert any(q.get("simple_status") == ["attention"] for _, path, q, _ in requests if path == "/api/done-archives")


def test_unknown_pending_cannot_resend_and_cleaned_body_is_hidden(ui):
    page, url, _, state = ui
    page.goto(url + "#view=pending&item=1")
    expect(page.get_by_text("飞书投递结果不确定", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="重试飞书投递")).to_have_count(0)
    state["cleaned"] = True
    page.reload()
    expect(page.get_by_text("本地待办内容已清理", exact=False)).to_be_visible()
    expect(page.get_by_text("Synthetic retained body")).to_have_count(0)


def test_markdown_detail_links_to_same_archive(ui):
    page, url, _, _ = ui
    page.goto(url + "#view=markdown&item=2")
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("部分交付", exact=True)).to_be_visible()
    expect(dialog.get_by_text("1 / 2", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="查看原件归档").click()
    expect(page.get_by_role("dialog").get_by_text("Synthetic archive")).to_be_visible()
    assert "view=done" in page.url and "item=42" in page.url


def test_optional_local_auth_has_login_flow(ui):
    page, url, requests, state = ui
    state["auth"] = False
    page.goto(url)
    page.get_by_label("访问令牌").fill("synthetic-token")
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("heading", name="总览", exact=True)).to_be_visible()
    assert any(method == "POST" and path == "/api/auth/login" for method, path, _, _ in requests)


def test_manual_delivery_reconciliation_requires_user_confirmation(ui):
    page, url, requests, _ = ui
    page.goto(url + "#view=pending&item=1")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="核对后登记已收到").click()
    assert not any(method == "POST" for method, _, _, _ in requests)
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="核对后登记已收到").click()
    expect(page.get_by_text("已记录核对结果", exact=False)).to_be_visible()
    posts = [(path, json.loads(body)) for method, path, _, body in requests if method == "POST"]
    assert posts == [("/api/pending-notifications/1/reconcile-delivery", {"delivery_id": 9, "outcome": "sent", "confirmed": True})]


def test_mobile_navigation_and_table_scroll_stay_within_viewport(ui):
    page, url, _, _ = ui
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url + "#view=markdown")
    expect(page.get_by_text("Synthetic page 1")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_title("打开导航").click()
    page.get_by_role("button", name="待办通知", exact=True).click()
    expect(page.get_by_text("Synthetic pending")).to_be_visible()
