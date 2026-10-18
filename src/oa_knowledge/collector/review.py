"""Collect visible body and attachment labels without fetching attachments."""
from html.parser import HTMLParser
from urllib.parse import urlsplit

from oa_knowledge.collector.detail import CollaborationDetailAdapter, AuthRequiredError


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "br"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip() + " ")


def html_text(html: str) -> str:
    parser = PlainText(); parser.feed(html)
    return "".join(parser.parts).strip()


def preview_detail(context, url: str, base_url: str) -> dict:
    origin = urlsplit(base_url)
    target = urlsplit(url)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        raise ValueError("review_origin_mismatch")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        CollaborationDetailAdapter._wait_dynamic_content(page, max_wait_ms=5000)
        if urlsplit(page.url).netloc != origin.netloc or urlsplit(page.url).path in {"/cas/login", "/seeyon/main.do", "/seeyon/index.jsp"} or page.locator("#login_username").count():
            raise AuthRequiredError("review_auth_required")
        snapshots = CollaborationDetailAdapter._snapshots(page, "body")
        body = "\n".join(html_text(snapshot.html) for snapshot in snapshots)
        names = []
        for frame in page.frames:
            try:
                values = frame.evaluate("""() => [...document.querySelectorAll(
                    '[onclick*="download"], [onclick*="Download"], [fileId], [fileid], .attachment a, .attachment_name'
                  )].map(el => (el.textContent || el.getAttribute('title') || '').trim()).filter(Boolean)""")
                names.extend(value for value in values if len(value) < 400)
            except Exception:
                continue
        return {"body": body[:200000], "body_truncated": len(body) > 200000,
                "attachment_names": list(dict.fromkeys(names)), "attachment_count": None,
                "inventory_scope": "visible_labels_only", "source": "oa_body_preview",
                "review_complete": False, "content_available": bool(body),
                "warning": "附件标签仅供预览，未遍历关联容器；数量仍为未知。未复核默认保留下载。"}
    finally:
        page.close()
