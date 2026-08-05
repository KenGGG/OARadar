import shutil
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from oa_knowledge.collector.pending_detail import extract_pending_detail_identifiers

pytestmark = pytest.mark.skipif(
    shutil.which("google-chrome") is None,
    reason="requires a local google-chrome binary",
)

FIXTURE = Path(__file__).parent / "fixtures" / "pending_detail_synthetic.html"


def test_extract_pending_detail_identifiers_from_seeyon_globals() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page()
        page.set_content(FIXTURE.read_text(encoding="utf-8"))
        identifiers = extract_pending_detail_identifiers(page)
        browser.close()

    assert identifiers.affair_id_text == "affair-synthetic"
    assert identifiers.summary_id_text == "summary-synthetic"
    assert identifiers.process_id_text == "process-synthetic"
    assert identifiers.activity_id_text == "activity-synthetic"
    assert identifiers.case_id_text == "case-synthetic"
    assert identifiers.workitem_id_text == "workitem-synthetic"
    assert identifiers.form_record_id_text == "form-synthetic"
    assert identifiers.template_id_text == "template-synthetic"


def test_missing_optional_identifiers_remain_none() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        page = browser.new_page()
        page.set_content("<html><body><script>window.affairId='only-affair'</script></body></html>")
        identifiers = extract_pending_detail_identifiers(page)
        browser.close()

    assert identifiers.affair_id_text == "only-affair"
    assert identifiers.process_id_text is None
    assert identifiers.summary_id_text is None
