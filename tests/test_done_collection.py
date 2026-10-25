from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import oa_knowledge.done_collection as collection
from oa_knowledge.collector import LoginState
from oa_knowledge.collector.done import DiscoveredDoneItem
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAManifestItem, OAManifestSync, OperationJob
from oa_knowledge.web import create_web_app


@pytest.fixture
def setup(config_file):
    settings = load_settings(config_file)
    settings.browser.base_url = "https://synthetic-oa.test"
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    worker = SimpleNamespace(settings=settings, engine=engine, config_path=None, owner="worker-test")
    yield settings, worker
    engine.dispose()


def fake_browser(monkeypatch, pages, *, state=LoginState.AUTHENTICATED):
    class Browser:
        def __init__(self, settings, **kwargs):
            self.page = object()
            self.base_url = settings.browser.base_url
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def login_with_saved_credentials(self, seconds):
            return state

    class Adapter:
        visited = []
        def __init__(self, *args):
            pass
        def open_list(self):
            return 1
        def _list_stats(self, frame):
            return sum(len(items) for items in pages), len(pages)
        def _discover_frame(self, frame, limit, number, offset):
            return pages[number - 1]
        def _current_page(self, frame):
            return frame
        def navigate_to_page(self, page, delay):
            self.visited.append(page)
            return page

    monkeypatch.setattr(collection, "BrowserSession", Browser)
    monkeypatch.setattr(collection, "DoneAdapter", Adapter)
    return Adapter


def item(number, title=None):
    return DiscoveredDoneItem(str(number), title or f"synthetic-{number}", datetime(2026, 1, 1, 8),
                              datetime(2026, 1, 2, 9), "synthetic sender", None, None, number, number)


def job(engine, identifier):
    with Session(engine) as session:
        record = session.get(OperationJob, identifier)
        session.expunge(record)
        return record


def test_inventory_commits_pages_and_resumes_after_worker_restart(setup, monkeypatch):
    settings, worker = setup
    adapter = fake_browser(monkeypatch, [[item(i)] for i in range(1, 12)])
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    assert job(worker.engine, identifier).progress_total is None
    collection.run_inventory(worker, identifier)
    first = job(worker.engine, identifier)
    assert first.status == "queued"
    assert first.progress_current == 10
    assert json.loads(first.parameters_json)["next_page"] == 11
    collection.run_inventory(worker, identifier)
    final = job(worker.engine, identifier)
    assert final.status == "completed"
    assert final.progress_current == final.progress_total == 11
    with Session(worker.engine) as session:
        assert session.scalar(select(func.count()).select_from(OAManifestItem)) == 11
        row = session.scalar(select(OAManifestItem).where(OAManifestItem.workitem_id_text == "1"))
        assert row.initiated_at == datetime(2026, 1, 1, 8)
        assert row.completed_at == datetime(2026, 1, 2, 9)
        assert row.sender == "synthetic sender"
        assert row.processing_status == "discovered"  # No attachment work during inventory.


def test_pause_preserves_checkpoint_and_resume_keeps_same_job(setup, monkeypatch):
    settings, worker = setup
    fake_browser(monkeypatch, [[item(i)] for i in range(1, 5)])
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    original = collection.upsert_manifest_page

    def pause_after_page_two(session, items):
        original(session, items)
        if items[0].list_page == 2:
            record = session.get(OperationJob, identifier)
            params = json.loads(record.parameters_json)
            params["pause_requested"] = True
            record.parameters_json = json.dumps(params)

    monkeypatch.setattr(collection, "upsert_manifest_page", pause_after_page_two)
    collection.run_inventory(worker, identifier)
    paused = job(worker.engine, identifier)
    assert paused.status == "paused"
    assert paused.progress_current == 2
    assert json.loads(paused.parameters_json)["next_page"] == 3
    monkeypatch.setattr(collection, "upsert_manifest_page", original)
    collection.control_collection(settings, identifier, "resume")
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "completed"


def test_repeated_inventory_reads_new_source_total(setup, monkeypatch):
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1)]])
    first = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, first)
    assert job(worker.engine, first).progress_total == 1
    fake_browser(monkeypatch, [[item(1), item(2)]])
    second = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, second)
    assert second != first
    assert job(worker.engine, second).progress_total == 2
    assert job(worker.engine, second).status == "completed"


def test_duplicate_source_rows_never_report_complete_or_allow_download(setup, monkeypatch):
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1)], [item(1)]])
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "failed"
    with pytest.raises(ValueError, match="完整已办清单"):
        collection.start_collection(settings, "done_download")
    collection.control_collection(settings, identifier, "resume")
    assert json.loads(job(worker.engine, identifier).parameters_json)["next_page"] == 1


def test_changed_source_boundary_restarts_inventory_without_deleting_archives(setup, monkeypatch):
    settings, worker = setup
    pages = [[item(i)] for i in range(1, 12)]
    fake_browser(monkeypatch, pages)
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    pages[0] = [item(100)]
    collection.run_inventory(worker, identifier)
    second = job(worker.engine, identifier)
    assert second.status == "queued"
    assert second.progress_current == 10
    assert "done:100" in json.loads(second.parameters_json)["keys"]
    assert "done:1" not in json.loads(second.parameters_json)["keys"]
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "completed"


def test_authentication_failure_is_resumable_without_false_progress(setup, monkeypatch):
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1)]], state=LoginState.AUTH_REQUIRED)
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "auth_required"
    assert job(worker.engine, identifier).progress_current == 0
    fake_browser(monkeypatch, [[item(1)]])
    collection.control_collection(settings, identifier, "resume")
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "completed"


def test_inventory_automatically_reauthenticates_mid_scan(setup, monkeypatch):
    settings, worker = setup
    adapter = fake_browser(monkeypatch, [[item(1)], [item(2)]])
    browser = collection.BrowserSession
    calls = []
    monkeypatch.setattr(browser, "login_state", lambda self: LoginState.AUTH_REQUIRED, raising=False)
    monkeypatch.setattr(browser, "login_with_saved_credentials", lambda self, seconds: calls.append(seconds) or LoginState.AUTHENTICATED)
    original = adapter._discover_frame
    failed = []
    def expire_once(self, frame, limit, number, offset):
        if number == 2 and not failed:
            failed.append(True)
            raise RuntimeError("synthetic expired session")
        return original(self, frame, limit, number, offset)
    monkeypatch.setattr(adapter, "_discover_frame", expire_once)
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    assert job(worker.engine, identifier).status == "completed"
    assert len(calls) == 2


def test_browser_never_fills_saved_password_on_redirected_origin(setup, monkeypatch):
    from oa_knowledge.collector.browser import BrowserSession
    settings, _worker = setup
    class Control:
        def count(self): return 1
    page = SimpleNamespace(url="https://unrelated.test/login", locator=lambda selector: Control())
    browser = BrowserSession(settings, page=page)
    monkeypatch.setattr("oa_knowledge.collector.browser.load_chrome_saved_credential", lambda *args: pytest.fail("must not read saved credentials"))
    assert browser._submit_saved_credentials() is False


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows browser password manager')
def test_headless_login_reopens_same_profile_for_browser_autofill(setup, monkeypatch):
    from oa_knowledge.collector.browser import BrowserSession, LoginState
    settings, _worker = setup
    browser = BrowserSession(settings, page=SimpleNamespace())
    events = []
    states = iter([LoginState.AUTH_REQUIRED, LoginState.AUTHENTICATED])
    monkeypatch.setattr(browser, 'goto_home', lambda: next(states))
    monkeypatch.setattr(browser, 'login_state', lambda: LoginState.AUTHENTICATED)
    monkeypatch.setattr(browser, '_submit_saved_credentials', lambda: False)
    monkeypatch.setattr(BrowserSession, '__exit__', lambda *args: events.append('closed'))
    monkeypatch.setattr(BrowserSession, '__enter__', lambda self: events.append(self.settings.browser_profile_path) or self)
    assert browser.login_with_saved_credentials(1) == LoginState.AUTHENTICATED
    assert browser.headed
    assert events == ['closed', settings.browser_profile_path]


def test_download_pause_request_is_not_lost_in_heartbeat(setup, monkeypatch):
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1)]])
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    download = collection.start_collection(settings, "done_download")
    def simulated_child(command, cwd, heartbeat, poll_interval):
        heartbeat()
        collection.control_collection(settings, download["id"], "pause")
        heartbeat()
        return 0, "", ""
    monkeypatch.setattr("oa_knowledge.web.worker._run_piped", simulated_child)
    collection.run_download(worker, download["id"])
    assert job(worker.engine, download["id"]).status == "paused"
    assert job(worker.engine, download["id"]).progress_current == 0


def test_download_watchdog_defers_only_stalled_item_and_continues(setup, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import json
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1), item(2)]])
    inventory = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, inventory)
    download = collection.start_collection(settings, "done_download")["id"]
    def stalled_child(command, cwd, heartbeat, poll_interval):
        with Session(worker.engine) as session:
            row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:1"))
            row.processing_status = "processing"
            row.last_retry_at = datetime.now(timezone.utc) - timedelta(seconds=settings.collector.attachment_total_timeout_seconds + 1)
            session.commit()
        assert heartbeat() is False
        return -1, "", ""
    monkeypatch.setattr("oa_knowledge.web.worker._run_piped", stalled_child)
    collection.run_download(worker, download)
    state = job(worker.engine, download)
    assert state.status == "queued"
    assert json.loads(state.parameters_json)["deferred_keys"] == ["done:1"]
    assert collection._job_dict(state)["deferred"] == 1
    with Session(worker.engine) as session:
        stalled = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:1"))
        assert stalled.processing_status == "download_failed" and stalled.failure_stage == "attachment_timeout"
        assert session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:2")).processing_status == "pending_download"
    collection.control_collection(settings, download, "pause")
    collection.control_collection(settings, download, "resume")
    assert json.loads(job(worker.engine, download).parameters_json)["deferred_keys"] == []


def test_download_targets_exclude_verified_and_skipped_rows_and_keep_inventory(setup, monkeypatch):
    from oa_knowledge.db.models import ExclusionPolicy
    settings, worker = setup
    fake_browser(monkeypatch, [[item(1, "出差申请 synthetic"), item(2), item(3)]])
    identifier = collection.start_collection(settings, "done_inventory")["id"]
    collection.run_inventory(worker, identifier)
    with Session(worker.engine) as session:
        session.add(ExclusionPolicy(name="synthetic exclusion", pattern="出差申请", scope="title", action="skip", enabled=True))
        row = session.scalar(select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:2"))
        row.processing_status = "no_attachment"
        row.no_attachment_confirmed = True
        session.commit()
    download = collection.start_collection(settings, "done_download")
    params = json.loads(job(worker.engine, download["id"]).parameters_json)
    assert params["keys"] == ["done:3"]
    with Session(worker.engine) as session:
        assert session.scalar(select(func.count()).select_from(OAManifestItem)) == 3
    collection.control_collection(settings, download["id"], "pause")
    assert job(worker.engine, download["id"]).status == "paused"


def test_api_requires_csrf_and_never_echoes_credentials(setup, monkeypatch):
    settings, worker = setup
    client = TestClient(create_web_app(settings))
    csrf = client.get("/").cookies["oa_csrf"]
    assert client.get("/api/done-collection").status_code == 200
    assert client.post("/api/done-collection/start", json={"operation": "done_inventory"}).status_code == 403
    headers = {"x-csrf-token": csrf}
    response = client.post("/api/done-collection/credentials", headers=headers,
                           json={"username": "synthetic", "password": "synthetic-secret" * 100})
    assert response.status_code == 422
    assert "synthetic-secret" not in response.text
    saved = []
    monkeypatch.setattr("oa_knowledge.collector.windows_credentials.save_credential", lambda *args: saved.append(args))
    response = client.post("/api/done-collection/credentials", headers=headers,
                           json={"username": "synthetic", "password": "synthetic-secret"})
    assert response.status_code == 410
    assert "synthetic-secret" not in response.text
    assert not saved


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Credential Manager integration")
def test_windows_credentials_roundtrip_and_origin_isolation():
    from uuid import uuid4
    from oa_knowledge.collector.windows_credentials import save_credential, load_credential, delete_credential
    origin = f"https://synthetic-{uuid4().hex}.invalid"
    try:
        save_credential(origin, "synthetic-user", "synthetic-password-测试")
        saved = load_credential(origin)
        assert saved.username == "synthetic-user"
        assert saved.password == "synthetic-password-测试"
        assert "synthetic-password" not in repr(saved)
        assert load_credential(origin.replace("https", "http")) is None
    finally:
        delete_credential(origin)
    assert load_credential(origin) is None
