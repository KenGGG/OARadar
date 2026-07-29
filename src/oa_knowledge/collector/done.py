from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import re
from typing import Callable

from playwright.sync_api import Frame, Page


@dataclass(frozen=True)
class DiscoveredDoneItem:
    workitem_id_text: str
    title: str
    created_at: datetime | None
    completed_at: datetime | None
    sender: str | None
    deadline_text: str | None
    category: str | None
    ordinal: int
    list_page: int = 1

    @property
    def oa_item_key(self) -> str:
        if self.workitem_id_text.strip():
            return f"done:{self.workitem_id_text.strip()}"
        fallback = "\x1f".join((self.title, self.sender or "", self.completed_at.isoformat() if self.completed_at else ""))
        return f"done:fallback:{hashlib.sha256(fallback.encode('utf-8')).hexdigest()}"


class DoneAdapter:
    def __init__(self, page: Page, direct_list_url: str | None = None):
        self.page = page
        self.direct_list_url = direct_list_url
        self._current_list_page = 1

    def open_list(self) -> Frame:
        # Prefer the already-open mainIframe; otherwise open the portal card's
        # “更多” action without relying on generated element IDs.
        frame = self._done_frame()
        if frame:
            return frame
        if self.direct_list_url:
            last_error = None
            for _ in range(3):
                try:
                    self.page.goto(self.direct_list_url, wait_until="domcontentloaded")
                    self.page.locator("input[name='workitemId']").first.wait_for(state="attached", timeout=15000)
                    self._current_list_page = 1
                    return self.page.main_frame
                except Exception as exc:
                    last_error = exc
                    self.page.wait_for_timeout(1000)
            raise RuntimeError("done list did not become ready after 3 attempts") from last_error
        try:
            self.page.get_by_text("已办事项", exact=True).first.wait_for(state="attached", timeout=15000)
        except Exception as exc:
            raise RuntimeError("done section title not loaded") from exc
        clicked = self.page.evaluate(
            """() => {
                const titles = [...document.querySelectorAll('li,div,span')]
                  .filter(el => el.textContent.trim() === '已办事项');
                for (const title of titles) {
                  let node = title;
                  for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                    const more = node.querySelector('[title="更多"]');
                    if (more) { more.click(); return true; }
                  }
                }
                return false;
            }"""
        )
        if not clicked:
            raise RuntimeError("done section 'more' action not found")
        return self._wait_done_frame()

    def _wait_done_frame(self) -> Frame:
        deadline = 150
        frame = None
        while deadline > 0 and frame is None:
            self.page.wait_for_timeout(100)
            frame = self._done_frame()
            deadline -= 1
        if not frame:
            raise RuntimeError("done list iframe did not open")
        return frame

    def _done_frame(self) -> Frame | None:
        for frame in self.page.frames:
            if "portalAffairController.do?method=moreDone" in frame.url:
                return frame
            try:
                if frame.locator("input[name='workitemId']").count() > 0 and "已办事项" in frame.locator("body").inner_text()[:100]:
                    return frame
            except Exception:
                continue
        return None

    def discover_current_page(self, limit: int = 20) -> list[DiscoveredDoneItem]:
        frame = self.open_list()
        return self._discover_frame(frame, limit, 1, 0)

    def discover_pages(
        self,
        limit: int,
        accept: Callable[[DiscoveredDoneItem], bool] | None = None,
        max_pages: int = 20,
        page_delay_seconds: float = 0,
        deal_time_start: datetime | None = None,
        deal_time_end: datetime | None = None,
    ) -> "DoneDiscovery":
        frame = self.open_list()
        if deal_time_start is not None and deal_time_end is not None:
            self.apply_deal_time_filter(frame, deal_time_start, deal_time_end)
        total_count, total_pages = self._list_stats(frame)
        accepted: list[DiscoveredDoneItem] = []
        accepted_keys: set[str] = set()
        scanned_row_count = 0
        pages_scanned = 0
        for page_number in range(1, min(max_pages, total_pages or max_pages) + 1):
            page_items = self._discover_frame(frame, 10_000, page_number, scanned_row_count)
            pages_scanned += 1
            scanned_row_count += len(page_items)
            for item in page_items:
                if accept is None or accept(item):
                    if item.oa_item_key in accepted_keys:
                        continue
                    accepted_keys.add(item.oa_item_key)
                    accepted.append(item)
                    if len(accepted) >= limit:
                        return DoneDiscovery(tuple(accepted), pages_scanned, len(accepted), scanned_row_count, total_count, total_pages)
            if total_pages and page_number >= total_pages:
                break
            if not self._next_page(frame, page_delay_seconds):
                break
        return DoneDiscovery(tuple(accepted), pages_scanned, len(accepted), scanned_row_count, total_count, total_pages)

    def discover_all_pages(self, page_delay_seconds: float = 0) -> "DoneDiscovery":
        """Read the complete unfiltered Done manifest and reconcile it to OA's total."""
        frame = self.open_list()
        total_count, total_pages = self._list_stats(frame)
        if total_count is None:
            raise RuntimeError("OA done-list total count is unavailable")
        if total_pages is None:
            raise RuntimeError("OA done-list total page count is unavailable")
        # The source total/page controls are the only bounds. There is deliberately
        # no configured/static page ceiling and no 'no new rows' early exit.
        return self.discover_pages(
            limit=total_count,
            max_pages=total_pages,
            page_delay_seconds=page_delay_seconds,
        )

    def apply_deal_time_filter(self, frame: Frame, start: datetime, end_exclusive: datetime) -> None:
        if end_exclusive <= start:
            raise ValueError("deal time end must be after start")
        previous = frame.locator("input[name='workitemId']").first.get_attribute("value") if frame.locator("input[name='workitemId']").count() else None
        option = frame.locator("a[title='处理时间']")
        if option.count() != 1:
            raise RuntimeError("done-list deal-time filter not found")
        option.evaluate("(element) => element.click()")
        end_inclusive = end_exclusive - timedelta(days=1)
        for selector, value in (
            ("#from_dealtime", start.strftime("%Y-%m-%d")),
            ("#to_dealtime", end_inclusive.strftime("%Y-%m-%d")),
        ):
            field = frame.locator(selector)
            if field.count() != 1:
                raise RuntimeError(f"done-list filter field not found: {selector}")
            field.evaluate(
                "(element, value) => { element.value = value; element.dispatchEvent(new Event('change', {bubbles: true})); }",
                value,
            )
        button = frame.locator("a.seary-bar-btn")
        if button.count() != 1:
            raise RuntimeError("done-list search button not found")
        button.evaluate("(element) => element.click()")
        for _ in range(120):
            self.page.wait_for_timeout(100)
            current = frame.locator("input[name='workitemId']").first.get_attribute("value") if frame.locator("input[name='workitemId']").count() else None
            if current != previous or self._list_stats(frame)[0] == 0:
                self._current_list_page = 1
                return
        raise RuntimeError("done-list deal-time filter did not refresh")

    def navigate_to_page(self, target_page: int, page_delay_seconds: float = 0) -> Frame:
        if target_page < 1:
            raise ValueError("target page must be positive")
        frame = self.open_list()
        current = self._current_page(frame)
        if target_page < current:
            field = frame.locator("[id$='_page_number']").first
            go = frame.locator("a.common_over_page_go").first
            if not field.count() or not go.count():
                raise RuntimeError("done list page jump controls not found")
            previous = frame.locator("input[name='workitemId']").first.get_attribute("value")
            field.fill(str(target_page)); go.click(force=True)
            self._wait_page_changed(frame, previous, page_delay_seconds)
            current = target_page
        while current < target_page:
            if not self._next_page(frame, page_delay_seconds):
                raise RuntimeError(f"cannot navigate to done list page {target_page}")
            current += 1
        self._current_list_page = current
        return frame

    def locate_item(self, target_page: int, title: str, workitem_id_text: str, page_delay_seconds: float = 0) -> str:
        try:
            frame = self.navigate_to_page(target_page, page_delay_seconds)
            if frame.locator(f"input[name='workitemId'][value='{workitem_id_text}']").count() == 1:
                return workitem_id_text
        except Exception:
            pass
        return self.search_for_item(title, workitem_id_text)

    def search_for_item(self, title: str, workitem_id_text: str) -> str:
        frame = self.open_list()
        variants = [title]
        cleaned = re.sub(r"^【[^】]+】", "", title).strip()
        cleaned = re.sub(r"\(由[^()]+原发\)$", "", cleaned).strip()
        cleaned = re.sub(r"^（[^）]*(?:合办|盖章)[^）]*）", "", cleaned).strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
        selector = f"input[name='workitemId'][value='{workitem_id_text}']"
        for search_title in variants:
            previous = frame.locator("input[name='workitemId']").first.get_attribute("value") if frame.locator("input[name='workitemId']").count() else None
            triggered = frame.evaluate(
            """title => {
                const subject = document.getElementById('subject');
                const titleField = document.getElementById('title');
                const field = subject || titleField;
                if (field) {
                    field.value = title;
                    field.dispatchEvent(new Event('input', {bubbles: true}));
                    field.dispatchEvent(new Event('change', {bubbles: true}));
                }
                if (window.jQuery && window.jQuery('#listDone').length && typeof window.jQuery('#listDone').ajaxgridLoad === 'function') {
                    window.jQuery('#listDone').ajaxgridLoad({subject: title, state: '4'});
                    return true;
                }
                const button = document.querySelector('#search, a.seary-bar-btn, button[type="submit"], input[type="submit"]');
                if (field && button) { button.click(); return true; }
                if (typeof window.advanceQuery === 'function') { window.advanceQuery('listDone'); return true; }
                if (typeof window.doSearch === 'function') { window.doSearch(); return true; }
                return false;
            }""",
                search_title,
            )
            if not triggered:
                raise RuntimeError("done-list title search control not found")
            for _ in range(100):
                self.page.wait_for_timeout(100)
                if frame.locator(selector).count() == 1:
                    self._current_list_page = 1
                    return workitem_id_text
                current = frame.locator("input[name='workitemId']").first
                if current.count() and current.get_attribute("value") != previous:
                    previous = current.get_attribute("value")
            rows = self._discover_frame(frame, 2, 1, 0)
            if len(rows) == 1 and _normalized_title(rows[0].title) == _normalized_title(search_title):
                self._current_list_page = 1
                return rows[0].workitem_id_text
        raise LookupError(f"workitem is not present after title search: {workitem_id_text}")

    @staticmethod
    def _discover_frame(frame: Frame, limit: int, page_number: int, ordinal_offset: int) -> list[DiscoveredDoneItem]:
        rows = frame.locator("input[name='workitemId']")
        items: list[DiscoveredDoneItem] = []
        for index in range(min(rows.count(), limit)):
            checkbox = rows.nth(index)
            row = checkbox.locator("xpath=ancestor::tr")
            cells = row.locator("td")
            texts = [cells.nth(i).inner_text().strip() for i in range(cells.count())]
            # checkbox, title, created, completed, sender, deadline, category
            items.append(
                DiscoveredDoneItem(
                    workitem_id_text=checkbox.get_attribute("value") or "",
                    title=texts[1] if len(texts) > 1 else "",
                    created_at=_parse_time(texts[2] if len(texts) > 2 else ""),
                    completed_at=_parse_time(texts[3] if len(texts) > 3 else ""),
                    sender=(texts[4] or None) if len(texts) > 4 else None,
                    deadline_text=(texts[5] or None) if len(texts) > 5 else None,
                    category=(texts[6] or None) if len(texts) > 6 else None,
                    ordinal=ordinal_offset + index + 1,
                    list_page=page_number,
                )
            )
        return items

    @staticmethod
    def _list_stats(frame: Frame) -> tuple[int | None, int | None]:
        total_text = frame.locator("[id$='_total_number']").first.inner_text() if frame.locator("[id$='_total_number']").count() else ""
        pages_text = frame.locator("[id$='_total_page']").first.inner_text() if frame.locator("[id$='_total_page']").count() else ""
        total_match = re.search(r"共\s*([\d,]+)\s*条", total_text)
        pages_match = re.search(r"共\s*([\d,]+)\s*页", pages_text)
        return (
            int(total_match.group(1).replace(",", "")) if total_match else None,
            int(pages_match.group(1).replace(",", "")) if pages_match else None,
        )

    @staticmethod
    def _next_page(frame: Frame, page_delay_seconds: float = 0) -> bool:
        button = frame.locator("a.pNext").first
        if not button.count() or "disabled" in (button.get_attribute("class") or "").lower():
            return False
        first = frame.locator("input[name='workitemId']").first
        previous = first.get_attribute("value") if first.count() else None
        button.click(force=True)
        DoneAdapter._wait_page_changed(frame, previous, page_delay_seconds)
        return True

    @staticmethod
    def _wait_page_changed(frame: Frame, previous: str | None, page_delay_seconds: float = 0) -> None:
        if page_delay_seconds:
            frame.page.wait_for_timeout(int(page_delay_seconds * 1000))
        for _ in range(200):
            frame.page.wait_for_timeout(100)
            current = frame.locator("input[name='workitemId']").first
            if current.count() and current.get_attribute("value") != previous:
                return
        raise RuntimeError("done list next page did not load")

    @staticmethod
    def _current_page(frame: Frame) -> int:
        field = frame.locator("[id$='_page_number']").first
        value = field.input_value() if field.count() else None
        try:
            return int(value or "1")
        except ValueError:
            return 1


@dataclass(frozen=True)
class DoneDiscovery:
    items: tuple[DiscoveredDoneItem, ...]
    pages_scanned: int
    query_count: int
    scanned_row_count: int
    source_total_count: int | None
    source_total_pages: int | None


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def _normalized_title(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()
