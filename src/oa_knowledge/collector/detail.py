from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import html
import io
import json
import re
from time import monotonic
from typing import Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import zipfile
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, Page


@dataclass(frozen=True)
class PageSnapshot:
    name: str
    source_url: str
    html: str = field(repr=False)


@dataclass(frozen=True)
class DirectAttachment:
    attachment_key: str
    filename: str
    file_url: str | None
    size_bytes: int | None
    mime_type: str | None = None
    file_role: str = "direct_attachment"
    content: bytes | None = field(default=None, repr=False)
    download_status: str = "discovered"


@dataclass(frozen=True)
class RelatedContainerCapture:
    container_key: str
    parent_container_key: str
    page_family: str
    depth: int
    source_url: str
    snapshots: tuple[PageSnapshot, ...]
    attachments: tuple[DirectAttachment, ...]
    has_unvisited_children: bool = False


@dataclass(frozen=True)
class DetailCapture:
    detail_url: str
    page_family: str
    body: tuple[PageSnapshot, ...]
    workflow: tuple[PageSnapshot, ...]
    attachments: tuple[DirectAttachment, ...]
    related_containers: tuple[RelatedContainerCapture, ...] = ()
    capture_issues: tuple[dict[str, object], ...] = ()


class AuthRequiredError(RuntimeError):
    """OA redirected the detail popup to the login page."""


class CollaborationDetailAdapter:
    _IGNORED_FRAME_MARKERS = (
        "downloadfile", "downloadiframe", "iframeright", "formrelative",
        "communicationbtns", "caozuo_more", "iframe_mask", "attachmentlist",
    )

    def __init__(
        self,
        list_page: Page,
        attachment_resolver: Callable[[str], tuple[bytes | None, str | None] | None] | None = None,
    ):
        self.list_page = list_page
        self.attachment_resolver = attachment_resolver
        self._capture_issues: list[dict[str, object]] = []
        self._last_download_failure: str | None = None

    @staticmethod
    def _read_download_payload(path: Path | None) -> bytes | None:
        """Accept a non-empty binary browser download, never an OA HTML response."""
        if path is None:
            return None
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        stripped = payload.lstrip().lower()
        if not payload or stripped.startswith((b"<!doctype", b"<html", b"<head", b"<body")):
            return None
        return payload

    @classmethod
    def _completed_download_payload(cls, download) -> bytes | None:
        """Read bytes only after Chromium reports a completed download."""
        if download.failure() is not None:
            return None
        return cls._read_download_payload(download.path())

    def _browser_download_payload(self, page: Page, trigger, timeout_ms: int) -> bytes | None:
        self._last_download_failure = None
        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                trigger()
            download = download_info.value
            self._last_download_failure = download.failure()
            payload = self._completed_download_payload(download)
            if payload is None and self._last_download_failure is None:
                self._last_download_failure = "download completed without a valid non-empty file"
            return payload
        except (PlaywrightError, TimeoutError, OSError) as exc:
            self._last_download_failure = f"{type(exc).__name__}: {exc}"
            return None

    @staticmethod
    def _cap4_filename(value: str) -> tuple[str, bool]:
        """Remove CAP4's display-only size suffix and identify plain 0-byte labels."""
        raw = value.strip()
        zero_byte_label = bool(re.search(r"\(\s*0\s*B\s*\)\s*$", raw, re.IGNORECASE))
        filename = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*(?:B|KB|MB|GB)\s*\)\s*$", "", raw, flags=re.IGNORECASE).strip()
        return filename or raw, zero_byte_label

    @classmethod
    def _downloadable_cap4_names(cls, values: list[str]) -> list[str]:
        return [value for value in values if value.strip() and not cls._cap4_filename(value)[1]]

    @staticmethod
    def _response_content_type(response) -> str:
        return (response.headers.get("content-type") or "").lower() if response is not None else ""

    def capture_direct(self, base_url: str, workitem_id_text: str, max_depth: int = 10, total_timeout_seconds: int = 900, download_timeout_seconds: int = 60) -> DetailCapture:
        query = urlencode({"method": "summary", "openFrom": "listDone", "affairId": workitem_id_text, "showTab": "1"})
        detail_url = f"{base_url.rstrip('/')}/seeyon/collaboration/collaboration.do?{query}"
        return self.capture(
            workitem_id_text,
            max_depth=max_depth,
            total_timeout_seconds=total_timeout_seconds,
            download_timeout_seconds=download_timeout_seconds,
            direct_url=detail_url,
        )

    def capture(self, workitem_id_text: str, max_depth: int = 10, total_timeout_seconds: int = 900, download_timeout_seconds: int = 60, direct_url: str | None = None) -> DetailCapture:
        self._capture_issues = []
        if direct_url:
            detail = self.list_page.context.new_page()
            detail.goto(direct_url, wait_until="domcontentloaded")
        else:
            checkbox = self.list_page.locator(
                f"input[name='workitemId'][value='{workitem_id_text}']"
            )
            if checkbox.count() != 1:
                raise LookupError(f"workitem is not present on the current list page: {workitem_id_text}")
            before = set(self.list_page.context.pages)
            subject_cell = checkbox.locator("xpath=ancestor::tr").locator("td[abbr='subject']")
            subject_cell.click(force=True)
            detail = self._wait_new_page(before)
        capture_deadline = monotonic() + total_timeout_seconds
        try:
            detail.wait_for_load_state("domcontentloaded")
            self._wait_dynamic_content(detail, max_wait_ms=1500)
            path = urlsplit(detail.url).path
            if path.rstrip("/") in {"/cas/login", "/seeyon/main.do"}:
                raise AuthRequiredError(f"OA session redirected to {path}")
            body = self._snapshots(detail, "body")
            default_role = "official_attachment" if "/govdoc/govdoc.do" in path else "direct_attachment"
            attachments = self._download_files(detail, default_role, capture_deadline, download_timeout_seconds)
            if "/collaboration/collaboration.do" in path:
                family = "collaboration"
                root_key = f"collaboration:{workitem_id_text}"
                related = self._crawl_associated(detail, root_key, 1, max_depth, set(), capture_deadline, download_timeout_seconds)
                workflow = self._optional_workflow(detail, 1500)
            elif "/mdf-node/meta/voucher/" in path:
                family = "expense_voucher"
                related = []
                flow = detail.get_by_text("流程", exact=True)
                if flow.count():
                    flow.first.click(force=True)
                    self._wait_dynamic_content(detail, max_wait_ms=1000)
                    workflow = self._workflow_snapshot(detail)
                else:
                    workflow = ()
            elif "/nccloud/thirdpartybyyfk" in path:
                family = "nccloud_finance"
                related = []
                workflow = ()
            elif "/govdoc/govdoc.do" in path:
                family = "govdoc"
                root_key = f"govdoc:{workitem_id_text}"
                related = self._crawl_associated(detail, root_key, 1, max_depth, set(), capture_deadline, download_timeout_seconds)
                flow = detail.get_by_text("流程", exact=True)
                if flow.count():
                    flow.first.click(force=True)
                    self._wait_dynamic_content(detail, max_wait_ms=1000)
                    workflow = self._workflow_snapshot(detail)
                else:
                    workflow = ()
            elif "/seeyon/meeting.do" in path:
                # Meeting detail pages expose their body and direct attachments
                # but do not have the collaboration workflow/association controls.
                family = "meeting"
                related = []
                workflow = ()
            else:
                raise ValueError(f"unsupported detail page family: {path}")
            return DetailCapture(
                detail_url=detail.url,
                page_family=family,
                body=body,
                workflow=workflow,
                attachments=attachments,
                related_containers=tuple(related),
                capture_issues=tuple(self._capture_issues),
            )
        finally:
            detail.close()

    def _wait_new_page(self, before: set[Page]) -> Page:
        for _ in range(120):
            pages = [page for page in self.list_page.context.pages if page not in before]
            if pages:
                return pages[-1]
            self.list_page.wait_for_timeout(100)
        raise RuntimeError("detail page did not open")

    def _optional_workflow(self, detail: Page, wait_ms: int) -> tuple[PageSnapshot, ...]:
        flow = detail.get_by_text("流程", exact=True)
        if not flow.count():
            return ()
        flow.first.click(force=True)
        self._wait_dynamic_content(detail, max_wait_ms=wait_ms)
        return self._workflow_snapshot(detail)

    @classmethod
    def _snapshots(cls, page: Page, prefix: str) -> tuple[PageSnapshot, ...]:
        snapshots: list[PageSnapshot] = []
        for index, frame in enumerate(page.frames):
            frame_name = frame.name or f"frame-{index}"
            identity = f"{frame_name} {frame.url}".lower()
            if any(marker in identity for marker in cls._IGNORED_FRAME_MARKERS):
                continue
            try:
                html = frame.content()
            except Exception:
                continue
            if not html.strip():
                continue
            snapshots.append(PageSnapshot(frame_name, frame.url, html))
        return cls._select_effective_snapshots(tuple(snapshots), prefix)

    @staticmethod
    def _select_effective_snapshots(
        snapshots: tuple[PageSnapshot, ...], prefix: str
    ) -> tuple[PageSnapshot, ...]:
        """Select one meaningful document and discard OA utility iframes."""
        candidates: list[tuple[int, PageSnapshot]] = []
        seen_hashes: set[str] = set()
        for snapshot in snapshots:
            identity = f"{snapshot.name} {snapshot.source_url}".lower()
            if any(marker in identity for marker in CollaborationDetailAdapter._IGNORED_FRAME_MARKERS):
                continue
            digest = hashlib.sha256(snapshot.html.encode("utf-8")).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            text = re.sub(r"<[^>]+>", " ", snapshot.html)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) < 2:
                continue
            score = min(len(text), 50_000)
            name = snapshot.name.lower()
            url = snapshot.source_url.lower()
            if "/content/content.do" in url:
                score += 100_000
            if "zwiframe" in name or "myformiframe" in name:
                score += 60_000
            if name == "main":
                score += 10_000
            candidates.append((score, snapshot))
        if not candidates:
            return ()
        selected = max(candidates, key=lambda entry: entry[0])[1]
        filename = "body.html" if prefix == "body" else f"{prefix}-body.html"
        return (PageSnapshot(filename, selected.source_url, selected.html),)

    @classmethod
    def _workflow_snapshot(cls, page: Page) -> tuple[PageSnapshot, ...]:
        selected = cls._snapshots(page, "workflow-source")
        if not selected:
            return ()
        source = selected[0]
        text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", source.html, flags=re.I | re.S)
        text = re.sub(r"<br\s*/?>|</(?:p|div|li|tr|td|th|section)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in text.splitlines():
            value = re.sub(r"\s+", " ", raw).strip()
            if len(value) < 2 or value in seen:
                continue
            seen.add(value)
            entries.append({"text": value})
            if len(entries) >= 500:
                break
        payload = json.dumps(
            {"schema_version": 1, "source_url": source.source_url, "entries": entries},
            ensure_ascii=False,
            indent=2,
        )
        return (PageSnapshot("workflow.json", source.source_url, payload),)

    def _crawl_associated(self, page: Page, parent_key: str, parent_depth: int, max_depth: int, visited: set[str], deadline: float, download_timeout_seconds: int) -> list[RelatedContainerCapture]:
        captures: list[RelatedContainerCapture] = []
        associations = self._associated_documents(page)
        for association in associations:
            key = association["key"]
            if key in visited:
                continue
            visited.add(key)
            before = set(page.context.pages)
            try:
                href = association.get("href")
                if href:
                    child = page.context.new_page()
                    child.goto(str(href), wait_until="domcontentloaded")
                else:
                    frame = page.frames[association["frame_index"]]
                    frame.locator(f"[id='attachmentDiv_{key}'] a[onclick*='openDetailURL']").first.click(force=True)
                    child = self._wait_context_page(page, before)
            except Exception as exc:
                self._capture_issues.append({
                    "kind": "associated_container_unavailable",
                    "container_key": str(key),
                    "parent_container_key": parent_key,
                    "depth": parent_depth + 1,
                    "error_code": type(exc).__name__,
                })
                continue
            try:
                child.wait_for_load_state("domcontentloaded")
                self._wait_dynamic_content(child, max_wait_ms=1800)
                family = "govdoc" if "/govdoc/" in child.url else "associated_document"
                container_key = f"{family}:{key}"
                snapshots = self._snapshots(child, f"container-{parent_depth + 1}")
                attachments = self._download_files(child, "official_attachment", deadline, download_timeout_seconds)
                child_depth = parent_depth + 1
                has_unvisited = child_depth == max_depth and bool(self._associated_documents(child))
                captures.append(RelatedContainerCapture(
                    container_key=container_key, parent_container_key=parent_key,
                    page_family=family, depth=child_depth, source_url=child.url,
                    snapshots=snapshots, attachments=attachments, has_unvisited_children=has_unvisited,
                ))
                if child_depth < max_depth:
                    captures.extend(self._crawl_associated(child, container_key, child_depth, max_depth, visited, deadline, download_timeout_seconds))
            finally:
                child.close()
        return captures

    @staticmethod
    def _wait_context_page(source: Page, before: set[Page]) -> Page:
        # OA popups normally appear immediately. Broken associations are isolated
        # as review issues, so four seconds is enough without delaying the item.
        for _ in range(40):
            pages = [page for page in source.context.pages if page not in before]
            if pages:
                return pages[-1]
            source.wait_for_timeout(100)
        raise RuntimeError("associated document did not open")

    @staticmethod
    def _is_recipient_collaboration_link(href: str, context_text: str) -> bool:
        parsed = urlsplit(href)
        if parsed.path.rstrip("/") != "/seeyon/collaboration/collaboration.do":
            return False
        query = parse_qs(parsed.query)
        if query.get("method", [""])[0].lower() != "summary":
            return False
        if query.get("openFrom", [""])[0].lower() != "glwd":
            return False
        if not query.get("affairId", [""])[0]:
            return False
        compact = re.sub(r"\s+", "", context_text)
        if re.search(r"协同意见|处理人意见|回复意见|意见区", compact):
            return False
        return bool(
            query.get("baseObjectId", [""])[0]
            or re.search(r"接收人|接收单位|主送|抄送|阅知|传阅文件|收文", compact)
        )

    @staticmethod
    def _associated_documents(page: Page) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for frame_index, frame in enumerate(page.frames):
            try:
                count = frame.locator("[comptype='assdoc'][attsdata]").count()
            except Exception:
                continue
            for index in range(count):
                try:
                    raw = frame.locator("[comptype='assdoc'][attsdata]").nth(index).get_attribute("attsdata") or "[]"
                except Exception:
                    continue
                try:
                    entries = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for entry in entries:
                    if entry.get("type") != 2 and str(entry.get("mimeType", "")).lower() != "edoc":
                        continue
                    key = str(entry.get("fileUrl") or entry.get("description") or entry.get("id") or "")
                    link = frame.locator(f"[id='attachmentDiv_{key}'] a[onclick*='openDetailURL']")
                    if key and link.count() and key not in seen:
                        result.append({"key": key, "frame_index": frame_index})
                        seen.add(key)
            try:
                links = frame.locator("a[href*='/seeyon/collaboration/collaboration.do']").evaluate_all(
                    """nodes => nodes.map(node => {
                        let context = '';
                        for (let current = node, depth = 0; current && depth < 8; current = current.parentElement, depth++) {
                            context += ' ' + (current.innerText || current.textContent || '');
                            if (current.tagName === 'TR' || current.tagName === 'TABLE' || current.tagName === 'FORM') break;
                        }
                        return {href: node.href || node.getAttribute('href') || '', context: context.slice(0, 1200)};
                    })"""
                )
            except Exception:
                links = []
            try:
                onclick_links = frame.locator("[onclick*='openFrom=glwd'], [onclick*='openfrom=glwd']").evaluate_all(
                    """nodes => nodes.map(node => ({
                        onclick: node.getAttribute('onclick') || '',
                        context: [node.innerText || node.textContent || '', node.getAttribute('title') || '', node.parentElement?.innerText || ''].join(' ').slice(0, 1200),
                    }))"""
                )
            except Exception:
                onclick_links = []
            for entry in onclick_links:
                match = re.search(r"(['\"])(/[^'\"]*?/collaboration/collaboration\.do\?[^'\"]+)\1", html.unescape(str(entry.get("onclick") or "")), re.IGNORECASE)
                if match:
                    links.append({"href": match.group(2), "context": entry.get("context") or ""})
            for entry in links:
                href = urljoin(frame.url or page.url, str(entry.get("href") or ""))
                if not CollaborationDetailAdapter._is_recipient_collaboration_link(href, str(entry.get("context") or "")):
                    continue
                key = "collaboration-link:" + hashlib.sha256(href.encode("utf-8")).hexdigest()[:20]
                if key not in seen:
                    result.append({"key": key, "frame_index": frame_index, "href": href})
                    seen.add(key)
        return result

    def _download_files(self, page: Page, default_role: str, deadline: float | None = None, download_timeout_seconds: int = 60) -> tuple[DirectAttachment, ...]:
        files: list[DirectAttachment] = []
        seen: set[str] = set()
        for frame in page.frames:
            try:
                links = frame.locator("a[_temp*='fileDownload.do?method=download']")
                descriptors = links.evaluate_all(
                    """(nodes, fallback) => {
                        const labels = [...document.querySelectorAll('span[fieldval*="displayName"]')];
                        return nodes.map(a => {
                            let role = fallback;
                            let node = a;
                            for (let i=0; node && i<8; i++, node=node.parentElement) {
                                const field = node.querySelector?.('span[fieldval*="displayName"]')?.getAttribute('fieldval') || '';
                                if (field.includes('displayName:"正文"')) { role = 'official_body'; break; }
                                if (field.includes('displayName:"附件"')) { role = 'official_attachment'; break; }
                            }
                            if (role === fallback) {
                                const block = a.closest('[id^="attachmentDiv_"]');
                                const prior = block ? labels.filter(x => x.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).pop() : null;
                                if ((prior?.getAttribute('fieldval') || '').includes('displayName:"正文"')) role = 'official_body';
                            }
                            return {file_url:a.getAttribute('_temp'), key:a.getAttribute('_id') || '', filename:a.getAttribute('title'), role};
                        });
                    }""",
                    default_role,
                )
            except Exception:
                continue
            for descriptor in descriptors:
                file_url = descriptor.get("file_url")
                key = descriptor.get("key") or ""
                if not file_url or not key or key in seen:
                    continue
                seen.add(key)
                filename = descriptor.get("filename") or f"attachment-{key}"
                role = descriptor.get("role") or default_role
                reused = self.attachment_resolver(key) if self.attachment_resolver is not None else None
                if reused is not None:
                    content, mime_type = reused
                    files.append(DirectAttachment(
                        attachment_key=key, filename=filename, file_url=file_url,
                        size_bytes=len(content) if content is not None else None,
                        mime_type=mime_type, file_role=role, content=content,
                        download_status="downloaded" if content is not None else "known_download_failed",
                    ))
                    continue
                absolute = page.url.split("/seeyon/")[0] + file_url if file_url.startswith("/seeyon/") else file_url
                content = None
                content_type = ""
                candidates = frame.locator("a[_temp]")
                for candidate_index in range(candidates.count()):
                    candidate = candidates.nth(candidate_index)
                    if candidate.get_attribute("_temp") == file_url:
                        content = self._browser_download_payload(
                            page,
                            lambda: candidate.evaluate("node => node.click()"),
                            download_timeout_seconds * 1000,
                        )
                        break
                if content is None:
                    response = None
                    for _attempt in range(2):
                        try:
                            response = page.context.request.get(
                                absolute, timeout=download_timeout_seconds * 1000
                            )
                            break
                        except PlaywrightError:
                            continue
                    try:
                        content = response.body() if response is not None and response.ok else None
                    except PlaywrightError:
                        content = None
                    content_type = self._response_content_type(response)
                    if content is not None and (
                        "text/html" in content_type
                        or content.lstrip().lower().startswith((b"<!doctype", b"<html"))
                    ):
                        content = None
                if content is None and Path(filename).suffix.lower() in {".htm", ".html"}:
                    try:
                        content = frame.content().encode("utf-8")
                        content_type = "text/html"
                    except PlaywrightError:
                        pass
                if content is None:
                    self._capture_issues.append({
                        "kind": "attachment_download_failed", "attachment_key": key,
                        "error": self._last_download_failure or "browser and API download failed",
                    })
                files.append(DirectAttachment(
                    attachment_key=key, filename=filename, file_url=file_url,
                    size_bytes=len(content) if content is not None else None,
                    mime_type=content_type, file_role=role,
                    content=content, download_status="downloaded" if content is not None else "download_failed",
                ))
        cap4_files = self._download_cap4_widgets(page, default_role, seen, deadline, download_timeout_seconds)
        files.extend(cap4_files)
        if not any(file.download_status == "downloaded" for file in cap4_files):
            files.extend(self._download_cap4_batches(page, default_role, seen, deadline, download_timeout_seconds))
        return tuple(files)

    def _download_cap4_widgets(
        self,
        page: Page,
        default_role: str,
        seen: set[str],
        deadline: float | None,
        download_timeout_seconds: int,
    ) -> list[DirectAttachment]:
        """Download each visible CAP4 attachment through its authenticated control."""
        files: list[DirectAttachment] = []
        for frame in page.frames:
            try:
                widgets = frame.locator(".cap4-attach__att")
                count = widgets.count()
            except PlaywrightError:
                continue
            for index in range(count):
                widget = widgets.nth(index)
                try:
                    descriptor = widget.evaluate("""node => ({
                        filename: node.querySelector('.cap4-attach__aright')?.textContent?.trim() || node.textContent?.trim() || '',
                        field: node.closest('.cap4-attach')?.querySelector('.cap4-attach__left')?.textContent || ''
                    })""")
                    filename, zero_byte_label = self._cap4_filename(
                        descriptor.get("filename") or f"cap4-attachment-{index + 1}"
                    )
                    if zero_byte_label:
                        continue
                    key = f"cap4-widget:{hashlib.sha256((frame.url + filename).encode('utf-8')).hexdigest()[:20]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    widget.hover(timeout=1000)
                    control = widget.locator(".cap4-attach__download").first
                    payload = self._browser_download_payload(
                        page,
                        lambda: control.click(force=True),
                        download_timeout_seconds * 1000,
                    )
                    if payload is None and Path(filename).suffix.lower() in {".htm", ".html"}:
                        payload = frame.content().encode("utf-8")
                    role = "official_body" if "正文" in (descriptor.get("field") or "") else "official_attachment"
                    files.append(DirectAttachment(
                        attachment_key=key,
                        filename=filename,
                        file_url=None,
                        size_bytes=len(payload) if payload is not None else None,
                        mime_type=None,
                        file_role=role,
                        content=payload,
                        download_status="downloaded" if payload is not None else "download_failed",
                    ))
                except PlaywrightError:
                    continue
        return files

    def _download_cap4_batches(
        self,
        page: Page,
        default_role: str,
        seen: set[str],
        deadline: float | None,
        download_timeout_seconds: int,
    ) -> list[DirectAttachment]:
        """Handle newer Cap4 forms whose attachment DOM exposes only batch ZIP links."""
        files: list[DirectAttachment] = []
        for frame in page.frames:
            try:
                descriptors = frame.locator("a.cap4-attach-downall[href*='batchDownload/']").evaluate_all(
                    """nodes => nodes.map(node => ({
                        href: node.href,
                        field: node.closest('.cap4-attach')?.querySelector('.cap4-attach__left')?.textContent || '',
                        names: [...(node.closest('.cap4-attach')?.querySelectorAll('.cap4-attach__att .cap4-attach__aright') || [])].map(item => item.textContent.trim())
                    }))"""
                )
                if not descriptors:
                    descriptors = frame.locator(".cap4-attach__att").evaluate_all(
                        """nodes => nodes.map(node => ({
                            href: node.closest('.cap4-attach')?.querySelector('a[href*="batchDownload/"]')?.href || '',
                            field: node.closest('.cap4-attach')?.querySelector('.cap4-attach__left')?.textContent || '',
                            names: [node.querySelector('.cap4-attach__aright')?.textContent || '']
                        })).filter(item => item.href)"""
                    )
            except Exception:
                continue
            for descriptor in descriptors:
                href = descriptor.get("href") or ""
                if not href:
                    continue
                batch_key = hashlib.sha256(href.encode("utf-8")).hexdigest()[:16]
                if batch_key in seen:
                    continue
                seen.add(batch_key)
                try:
                    response = page.context.request.get(href, timeout=download_timeout_seconds * 1000)
                    payload = response.body() if response.ok else None
                except PlaywrightError:
                    payload = None
                if not payload or not zipfile.is_zipfile(io.BytesIO(payload)):
                    # Some Cap4 deployments reject APIRequestContext downloads but allow
                    # the same authenticated request through the browser download flow.
                    try:
                        links = frame.locator("a.cap4-attach-downall[href*='batchDownload/']")
                        for index in range(links.count()):
                            candidate = links.nth(index)
                            candidate_href = candidate.evaluate("node => node.href")
                            if candidate_href == href:
                                payload = self._browser_download_payload(
                                    page,
                                    lambda: candidate.click(force=True),
                                    download_timeout_seconds * 1000,
                                )
                                break
                    except (PlaywrightError, TimeoutError, OSError):
                        payload = None
                if not payload:
                    names = self._downloadable_cap4_names(descriptor.get("names") or [])
                    role = "official_body" if "正文" in (descriptor.get("field") or "") else "official_attachment"
                    for index, raw_name in enumerate(names):
                        key = f"cap4:{batch_key}:failed:{index}"
                        if key in seen:
                            continue
                        seen.add(key)
                        files.append(DirectAttachment(
                            attachment_key=key, filename=raw_name.strip() or f"attachment-{batch_key}-{index + 1}",
                            file_url=href, size_bytes=None, mime_type=None, file_role=role,
                            content=None, download_status="download_failed",
                        ))
                    continue
                try:
                    archive = zipfile.ZipFile(io.BytesIO(payload))
                    names = [name for name in archive.namelist() if name and not name.endswith("/")]
                except zipfile.BadZipFile:
                    names = self._downloadable_cap4_names(descriptor.get("names") or [])
                    role = "official_body" if "正文" in (descriptor.get("field") or "") else "official_attachment"
                    for index, raw_name in enumerate(names):
                        filename = raw_name.strip() or f"attachment-{batch_key}-{index + 1}"
                        key = f"cap4:{batch_key}:failed:{index}"
                        if key in seen:
                            continue
                        seen.add(key)
                        files.append(DirectAttachment(
                            attachment_key=key, filename=filename, file_url=href,
                            size_bytes=None, mime_type=None, file_role=role,
                            content=None, download_status="download_failed",
                        ))
                    continue
                role = "official_body" if "正文" in (descriptor.get("field") or "") else "official_attachment"
                for name in names:
                    filename = Path(name).name or f"attachment-{batch_key}"
                    key = f"cap4:{batch_key}:{name}"
                    if key in seen:
                        continue
                    seen.add(key)
                    content = archive.read(name)
                    files.append(DirectAttachment(
                        attachment_key=key, filename=filename, file_url=href,
                        size_bytes=len(content), mime_type=None, file_role=role,
                        content=content, download_status="downloaded",
                    ))
        return files

    @staticmethod
    def _wait_dynamic_content(page: Page, max_wait_ms: int, min_wait_ms: int = 600) -> None:
        """Wait for legacy OA iframe/attachment DOM to settle without a fixed full delay."""
        elapsed = 0
        previous: tuple | None = None
        stable = 0
        while elapsed < max_wait_ms:
            page.wait_for_timeout(100)
            elapsed += 100
            signature: list[tuple[str, int, int, int, int, int]] = []
            for frame in page.frames:
                try:
                    attachment_count = frame.locator("a[_temp*='fileDownload.do?method=download']").count()
                    association_count = frame.locator("[comptype='assdoc'][attsdata]").count()
                    cap4_batch_count = frame.locator("a.cap4-attach-downall[href*='batchDownload/']").count()
                    cap4_file_count = frame.locator(".cap4-attach__att").count()
                    child_count = frame.evaluate("() => document.body?.childElementCount || 0")
                    signature.append((frame.url, attachment_count, association_count, cap4_batch_count, cap4_file_count, int(child_count)))
                except Exception:
                    continue
            current = tuple(signature)
            stable = stable + 1 if current == previous else 0
            previous = current
            if elapsed >= min_wait_ms and stable >= 2:
                return
