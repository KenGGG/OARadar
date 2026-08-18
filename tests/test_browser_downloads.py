from pathlib import Path

import pytest

from oa_knowledge.collector.browser import BrowserSession
from oa_knowledge.collector.detail import CollaborationDetailAdapter
from oa_knowledge.config import Settings


def test_browser_session_enables_download_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    class Context:
        pages = []
        def set_default_timeout(self, _timeout): pass
        def new_page(self): return object()
        def close(self): pass

    class Chromium:
        def launch_persistent_context(self, _profile, **kwargs):
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
