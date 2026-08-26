from pathlib import Path

import pytest

from oa_knowledge.collector.browser import BrowserSession
from oa_knowledge.collector.detail import CollaborationDetailAdapter, RelatedContainerCapture
from oa_knowledge.config import Settings


def test_browser_session_enables_download_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    class Context:
        pages = []
        def set_default_timeout(self, _timeout): pass
        def new_page(self): return object()
        def close(self): pass

    class Chromium:
        def launch_persistent_context(self, profile, **kwargs):
            captured["profile"] = profile
            captured.update(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()
        def stop(self): pass

    monkeypatch.setattr("oa_knowledge.collector.browser.sync_playwright", lambda: type("Manager", (), {"start": lambda self: Playwright()})())
    settings = Settings.model_validate({"app": {"data_root": str(tmp_path)}, "browser": {"executable_path": "/usr/bin/google-chrome"}})

    with BrowserSession(settings):
        pass

    assert captured["accept_downloads"] is True
    assert Path(captured["profile"]) == settings.cache_root / "browser-profile"


def test_download_payload_rejects_html_and_empty_files(tmp_path: Path) -> None:
    html_file = tmp_path / "login.html"
    html_file.write_bytes(b"<!doctype html><html>login</html>")
    empty_file = tmp_path / "empty.pdf"
    empty_file.touch()

    assert CollaborationDetailAdapter._read_download_payload(html_file) is None
    assert CollaborationDetailAdapter._read_download_payload(empty_file) is None


def test_download_payload_accepts_pdf_bytes(tmp_path: Path) -> None:
    pdf_file = tmp_path / "attachment.pdf"
    pdf_file.write_bytes(b"%PDF-1.7\nsynthetic")

    assert CollaborationDetailAdapter._read_download_payload(pdf_file) == b"%PDF-1.7\nsynthetic"


def test_browser_download_is_accepted_only_after_chromium_reports_completion(tmp_path: Path) -> None:
    file = tmp_path / "archive.zip"
    file.write_bytes(b"PK\x03\x04synthetic")

    class Download:
        def __init__(self, failure): self._failure = failure
        def failure(self): return self._failure
        def path(self): return file

    assert CollaborationDetailAdapter._completed_download_payload(Download("canceled")) is None
    assert CollaborationDetailAdapter._completed_download_payload(Download(None)) == b"PK\x03\x04synthetic"


def test_missing_api_response_has_empty_content_type() -> None:
    assert CollaborationDetailAdapter._response_content_type(None) == ""


def test_unavailable_associated_document_blocks_no_attachment_confirmation() -> None:
    """A failed secondary container must not inherit the root page's empty result."""
    issues = ({"kind": "associated_container_unavailable"},)

    assert not CollaborationDetailAdapter._can_confirm_no_attachment(
        root_confirmed_empty=True,
        related=(),
        capture_issues=issues,
    )


def test_wait_context_page_keeps_polling_for_a_slow_oa_popup() -> None:
    """A valid popup appearing after the old four-second window remains discoverable."""
    child = type("Child", (), {"url": "https://oa.invalid/seeyon/govdoc/govdoc.do"})()

    class Context:
        pages: list[object] = []

    class Source:
        context = Context()
        polls = 0

        def wait_for_timeout(self, _milliseconds: int) -> None:
            self.polls += 1
            if self.polls == 50:
                self.context.pages.append(child)

    source = Source()

    assert CollaborationDetailAdapter._wait_context_page(source, set()) is child  # type: ignore[arg-type]


def test_wait_context_page_does_not_return_the_initial_blank_popup() -> None:
    """Reading about:blank would miss attachments loaded by the later govdoc navigation."""
    child = type("Child", (), {"url": "about:blank"})()

    class Context:
        pages = [child]

    class Source:
        context = Context()
        polls = 0

        def wait_for_timeout(self, _milliseconds: int) -> None:
            self.polls += 1
            if self.polls == 5:
                child.url = "https://oa.invalid/seeyon/govdoc/govdoc.do"

    source = Source()
    opened = CollaborationDetailAdapter._wait_context_page(source, set())  # type: ignore[arg-type]

    assert opened is child
    assert source.polls == 5


def test_dynamic_content_wait_does_not_settle_before_late_govdoc_attachments() -> None:
    """A stable empty shell is not a settled govdoc attachment inventory."""
    class Locator:
        def __init__(self, page, selector: str):
            self.page = page
            self.selector = selector

        def count(self) -> int:
            if self.page.polls >= 20 and "fileDownload.do" in self.selector:
                return 3
            return 0

    class Frame:
        url = "https://oa.invalid/seeyon/govdoc/govdoc.do"

        def __init__(self, page):
            self.page = page

        def locator(self, selector: str) -> Locator:
            return Locator(self.page, selector)

        def evaluate(self, _script: str) -> int:
            return 1

    class Page:
        polls = 0

        def __init__(self):
            self.frames = [Frame(self)]

        def wait_for_timeout(self, _milliseconds: int) -> None:
            self.polls += 1

    page = Page()
    CollaborationDetailAdapter._wait_dynamic_content(page, max_wait_ms=5000)  # type: ignore[arg-type]

    assert page.polls >= 20


def test_collaboration_capture_opens_associated_documents_before_root_attachment_panel() -> None:
    """Associated attachments count even when the root collaboration is empty."""
    events: list[str] = []

    class Detail:
        url = "https://oa.invalid/seeyon/collaboration/collaboration.do"

        def goto(self, *_args, **_kwargs) -> None: pass
        def wait_for_load_state(self, *_args, **_kwargs) -> None: pass
        def close(self) -> None: pass

    class Context:
        def new_page(self) -> Detail: return Detail()

    class ListPage:
        context = Context()

    class Adapter(CollaborationDetailAdapter):
        @staticmethod
        def _wait_dynamic_content(*_args, **_kwargs) -> None: pass

        @classmethod
        def _snapshots(cls, *_args, **_kwargs): return (object(),)

        def _crawl_associated(self, *_args, **_kwargs):
            events.append("associated")
            return [RelatedContainerCapture(
                container_key="govdoc:synthetic",
                parent_container_key="collaboration:synthetic",
                page_family="govdoc",
                depth=2,
                source_url="https://oa.invalid/seeyon/govdoc/govdoc.do",
                snapshots=(),
                attachments=(object(),),  # type: ignore[arg-type]
            )]

        def _download_files(self, *_args, **_kwargs):
            events.append("root_attachments")
            self._last_attachment_inventory_confirmed_empty = True
            return ()

        def _optional_workflow(self, *_args, **_kwargs): return ()

    adapter = Adapter(ListPage())  # type: ignore[arg-type]
    capture = adapter.capture("synthetic", direct_url="https://oa.invalid/detail")

    assert events == ["associated", "root_attachments"]
    assert len(capture.related_containers) == 1


def test_direct_attachment_prefers_completed_browser_download_over_api() -> None:
    descriptor = {"file_url": "/seeyon/fileDownload.do?id=1", "key": "file-1", "filename": "large.zip", "role": "direct_attachment"}

    class Candidate:
        def get_attribute(self, name): return descriptor["file_url"] if name == "_temp" else None
        def click(self, **_kwargs): raise AssertionError("hidden OA links must not use locator click")
        def evaluate(self, script):
            assert "node.click" in script
    class Locator:
        def __init__(self, candidates=False): self.candidates = candidates
        def evaluate_all(self, *_args): return [descriptor]
        def count(self): return 1 if self.candidates else 0
        def nth(self, _index): return Candidate()
    class Frame:
        def locator(self, selector): return Locator(candidates=selector == "a[_temp]")
    class Request:
        def get(self, *_args, **_kwargs): raise AssertionError("API must not run after browser completion")
    class Page:
        frames = [Frame()]
        url = "https://oa.invalid/seeyon/detail"
        context = type("Context", (), {"request": Request()})()

    adapter = CollaborationDetailAdapter(None)  # type: ignore[arg-type]
    def completed_browser_download(_page, trigger, _timeout):
        trigger()
        return b"PK\x03\x04complete"
    adapter._browser_download_payload = completed_browser_download  # type: ignore[method-assign]

    files = adapter._download_files(Page(), "direct_attachment", download_timeout_seconds=10)

    assert len(files) == 1
    assert files[0].content == b"PK\x03\x04complete"
    assert files[0].download_status == "downloaded"


def test_inventory_only_lists_attachment_without_downloading() -> None:
    descriptor = {"file_url": "/seeyon/fileDownload.do?id=1", "key": "file-1", "filename": "large.zip", "role": "direct_attachment"}
    class Locator:
        def evaluate_all(self, *_args): return [descriptor]
        def count(self): return 0
    class Frame:
        def locator(self, _selector): return Locator()
    class Page:
        frames = [Frame()]
        url = "https://oa.invalid/seeyon/detail"
    adapter = CollaborationDetailAdapter(None, inventory_only=True)  # type: ignore[arg-type]
    adapter._browser_download_payload = lambda *_args: (_ for _ in ()).throw(AssertionError("must not download"))  # type: ignore[method-assign]
    files = adapter._download_files(Page(), "direct_attachment")
    assert [(row.attachment_key, row.download_status, row.content) for row in files] == [("file-1", "discovered", None)]


def test_attachment_loop_stops_when_total_capture_deadline_is_reached(monkeypatch) -> None:
    descriptors = [
        {"file_url": f"/seeyon/fileDownload.do?id={index}", "key": f"file-{index}",
         "filename": f"file-{index}.pdf", "role": "direct_attachment"}
        for index in (1, 2)
    ]
    clock = {"value": 0.0}

    class Candidate:
        def __init__(self, descriptor): self.descriptor = descriptor
        def get_attribute(self, name): return self.descriptor["file_url"] if name == "_temp" else None
        def evaluate(self, _script): pass
    class Locator:
        def __init__(self, candidates=False): self.candidates = candidates
        def evaluate_all(self, *_args): return descriptors
        def count(self): return len(descriptors) if self.candidates else 0
        def nth(self, index): return Candidate(descriptors[index])
    class Frame:
        def locator(self, selector): return Locator(candidates=selector == "a[_temp]")
    class Page:
        frames = [Frame()]
        url = "https://oa.invalid/seeyon/detail"
        context = type("Context", (), {"request": object()})()

    adapter = CollaborationDetailAdapter(None)  # type: ignore[arg-type]

    def download_one(_page, trigger, _timeout):
        trigger()
        clock["value"] = 10.0
        return b"%PDF synthetic"

    adapter._browser_download_payload = download_one  # type: ignore[method-assign]
    monkeypatch.setattr("oa_knowledge.collector.detail.monotonic", lambda: clock["value"])

    files = adapter._download_files(
        Page(), "direct_attachment", deadline=5.0, download_timeout_seconds=30,
    )

    assert [row.attachment_key for row in files] == ["file-1"]
    assert adapter._capture_issues == [{"kind": "capture_timeout", "stage": "attachments"}]


def test_missing_workflow_tab_is_optional() -> None:
    class Flow:
        def count(self): return 0
    class Detail:
        def get_by_text(self, *_args, **_kwargs): return Flow()
    adapter = CollaborationDetailAdapter(None)  # type: ignore[arg-type]

    assert adapter._optional_workflow(Detail(), 1500) == ()
