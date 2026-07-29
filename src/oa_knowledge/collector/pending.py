"""Read-only adapter for Seeyon pending-list discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import urlencode

from playwright.sync_api import Frame, Page

PENDING_LIST_PATH = "/seeyon/collaboration/collaboration.do?method=listPending"


@dataclass(frozen=True)
class DiscoveredPendingItem:
    affair_id_text: str
    title: str
    sender: str | None
    previous_approver: str | None
    initiated_at: datetime | None
    received_at: datetime | None
    deadline_text: str | None
    reminder_count: int
    processing_status: str | None
    current_node: str | None
    importance: str | None
    ordinal: int
    list_page: int = 1

    @property
    def occurrence_key(self) -> str:
        return f"pending:{self.affair_id_text}"


@dataclass(frozen=True)
class PendingDiscovery:
    items: tuple[DiscoveredPendingItem, ...]
    pages_scanned: int
    query_count: int
    scanned_row_count: int
    source_total_count: int | None
    source_total_pages: int | None


class PendingAdapter:
    list_path = PENDING_LIST_PATH

    def __init__(self, page: Page, direct_list_url: str | None = None):
        self.page = page
        self.direct_list_url = direct_list_url

    def open_list(self) -> Frame:
        target = self.direct_list_url
        if target is None:
            raise RuntimeError("pending list URL is required")
        self.page.goto(target, wait_until="domcontentloaded")
        self.page.locator("#listPending").wait_for(state="attached", timeout=15000)
        return self.page.main_frame

    def discover_current_page(self, limit: int = 20) -> list[DiscoveredPendingItem]:
        return self._discover_frame(self.open_list(), limit, 1, 0)

    def discover_pages(self, limit: int, max_pages: int = 1, page_delay_seconds: float = 0) -> PendingDiscovery:
        frame = self.open_list()
        total_count, total_pages = self._list_stats(frame)
        accepted: list[DiscoveredPendingItem] = []
        seen: set[str] = set()
        scanned = 0
        pages = 0
        for page_number in range(1, min(max_pages, total_pages or max_pages) + 1):
            rows = self._discover_frame(frame, 10_000, page_number, scanned)
            pages += 1
            scanned += len(rows)
            for item in rows:
                if item.affair_id_text in seen:
                    continue
                seen.add(item.affair_id_text)
                accepted.append(item)
                if len(accepted) >= limit:
                    return PendingDiscovery(tuple(accepted), pages, len(accepted), scanned, total_count, total_pages)
            if total_pages and page_number >= total_pages:
                break
            if not self._next_page(frame, page_delay_seconds):
                break
        return PendingDiscovery(tuple(accepted), pages, len(accepted), scanned, total_count, total_pages)

    @staticmethod
    def detail_url(base_url: str, affair_id_text: str) -> str:
        query = urlencode({
            "method": "summary",
            "openFrom": "listPending",
            "affairId": affair_id_text,
            "showTab": "1",
        })
        return f"{base_url.rstrip('/')}/seeyon/collaboration/collaboration.do?{query}"

    @staticmethod
    def _discover_frame(frame: Frame, limit: int, page_number: int, ordinal_offset: int) -> list[DiscoveredPendingItem]:
        rows = frame.locator("#listPending tbody tr")
        items: list[DiscoveredPendingItem] = []
        for index in range(min(rows.count(), limit)):
            row = rows.nth(index)
            checkbox = row.locator("input[type='checkbox']").first
            if not checkbox.count():
                continue
            affair_id = (checkbox.get_attribute("value") or "").strip()
            if not affair_id:
                continue
            cells = row.locator("td")
            texts = [" ".join(cells.nth(i).inner_text().split()) for i in range(cells.count())]
            importance_node = cells.nth(1).locator("[data-importance]").first if cells.count() > 1 else None
            importance = (
                (importance_node.get_attribute("data-importance") or "").strip() or None
                if importance_node is not None and importance_node.count()
                else None
            )
            items.append(DiscoveredPendingItem(
                affair_id_text=affair_id,
                title=texts[1] if len(texts) > 1 else "",
                sender=_optional(texts, 2),
                previous_approver=_optional(texts, 3),
                initiated_at=_parse_time(texts[4] if len(texts) > 4 else ""),
                received_at=_parse_time(texts[5] if len(texts) > 5 else ""),
                deadline_text=_optional(texts, 6),
                reminder_count=_parse_int(texts[7] if len(texts) > 7 else ""),
                processing_status=_optional(texts, 8),
                current_node=_optional(texts, 10),
                importance=importance,
                ordinal=ordinal_offset + index + 1,
                list_page=page_number,
            ))
        return items

    @staticmethod
    def _list_stats(frame: Frame) -> tuple[int | None, int | None]:
        total_node = frame.locator("[id$='_total_number']").first
        pages_node = frame.locator("[id$='_total_page']").first
        total_text = total_node.inner_text() if total_node.count() else ""
        pages_text = pages_node.inner_text() if pages_node.count() else ""
        total_match = re.search(r"共\s*([\d,]+)\s*条", total_text)
        pages_match = re.search(r"共\s*([\d,]+)\s*页", pages_text)
        return (
            int(total_match.group(1).replace(",", "")) if total_match else None,
            int(pages_match.group(1).replace(",", "")) if pages_match else None,
        )

    @staticmethod
    def _next_page(frame: Frame, page_delay_seconds: float) -> bool:
        button = frame.locator("a.pNext").first
        if not button.count() or "disabled" in (button.get_attribute("class") or "").lower():
            return False
        first = frame.locator("#listPending tbody tr input[type='checkbox']").first
        previous = first.get_attribute("value") if first.count() else None
        button.click(force=True)
        if page_delay_seconds:
            frame.page.wait_for_timeout(int(page_delay_seconds * 1000))
        for _ in range(200):
            frame.page.wait_for_timeout(50)
            current = frame.locator("#listPending tbody tr input[type='checkbox']").first
            if current.count() and current.get_attribute("value") != previous:
                return True
        raise RuntimeError("pending list next page did not load")


def _optional(values: list[str], index: int) -> str | None:
    return (values[index] or None) if len(values) > index else None


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_int(value: str) -> int:
    match = re.search(r"\d+", value)
    return int(match.group()) if match else 0
