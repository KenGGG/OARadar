import json
import os
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner
from sqlalchemy.orm import Session

from oa_knowledge.cli import _oa_detail_url, _sanitize_operational_error, app
from oa_knowledge.collector import LoginState
from oa_knowledge.collector.detail import AuthRequiredError
from oa_knowledge.collector.done import DiscoveredDoneItem
from oa_knowledge.collector.pending import DiscoveredPendingItem, PendingDiscovery
from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.models import BatchItem, CollectionBatch, OperationJob

runner = CliRunner()


def test_oa_detail_url_preserves_the_seeyon_application_prefix() -> None:
    assert _oa_detail_url("https://oa.synthetic", "/govdoc/govdoc.do?method=summary") == (
        "https://oa.synthetic/seeyon/govdoc/govdoc.do?method=summary"
    )
    assert _oa_detail_url("https://oa.synthetic", "/seeyon/meeting.do?method=mydetail") == (
        "https://oa.synthetic/seeyon/meeting.do?method=mydetail"
    )


def _last_json(output: str) -> dict:
    """Parse the last JSON document in mixed CLI output (logs may precede it)."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON line in output: {output!r}")


def test_init_is_idempotent_and_status_works(config_file: Path) -> None:
    first = runner.invoke(app, ["init", "--config", str(config_file)])
    second = runner.invoke(app, ["init", "--config", str(config_file)])
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    data_root = config_file.parent / "data"
    database_path = load_settings(config_file).database_path
    assert oct(data_root.stat().st_mode & 0o777) == "0o700"
    assert oct(database_path.stat().st_mode & 0o777) == "0o600"
    assert {path.name for path in data_root.iterdir()} == {"originals", "markdown"}
    status = runner.invoke(app, ["status", "--config", str(config_file)])
    assert status.exit_code == 0
    assert json.loads(status.output)["schema"] == "0039_classification_run_adopted_decision"


def test_schedule_enqueue_creates_worker_job_without_opening_browser(config_file: Path) -> None:
    result = runner.invoke(app, [
        "schedule", "enqueue", "hourly", "--config", str(config_file),
    ])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.output)
    assert payload["status"] == "queued"
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        job = session.get(OperationJob, payload["job_id"])
        assert job.job_type == "scheduled_hourly"
        assert job.status == "queued"


def test_manifest_run_exposes_bounded_first_page_options_without_a_separate_pilot() -> None:
    result = runner.invoke(app, ["manifest", "--help"])
    assert result.exit_code == 0, result.output
    assert "pilot" not in result.output
    run_help = runner.invoke(app, ["manifest", "run", "--help"])
    assert run_help.exit_code == 0, run_help.output
    assert "--max-pages" in run_help.output
    assert "--max-items" in run_help.output


def test_bounded_manifest_run_falls_back_to_current_list_row_when_direct_detail_is_blank(config_file: Path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    item = DiscoveredDoneItem(
        workitem_id_text="synthetic-workitem",
        title="合成事项",
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
        completed_at=None,
        sender="合成人员",
        deadline_text=None,
        category=None,
        ordinal=1,
    )
    observed = {"direct": False, "list_click": False}

    class FakePage:
        def wait_for_timeout(self, _milliseconds): pass

    class FakeBrowser:
        base_url = "https://oa.invalid"
        page = FakePage()
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds): return LoginState.AUTHENTICATED

    class FakeDone:
        def __init__(self, *_args, **_kwargs): pass
        def open_list(self): return object()
        def _list_stats(self, _frame): return 1, 1
        def _discover_frame(self, *_args): return [item]
        def detail_link_for_item(self, _workitem_id): return None

    class FakeDetail:
        def __init__(self, *_args, **_kwargs): pass
        def capture_direct(self, *_args, **_kwargs):
            observed["direct"] = True
            raise RuntimeError("synthetic direct capture failure")
        def capture(self, *_args, **_kwargs):
            observed["list_click"] = True
            raise RuntimeError("synthetic list click must not be primary")

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.cli.DoneAdapter", FakeDone)
    monkeypatch.setattr("oa_knowledge.cli.CollaborationDetailAdapter", FakeDetail)

    result = runner.invoke(app, [
        "manifest", "run", "--max-pages", "1", "--max-items", "1",
        "--config", str(config_file),
    ])

    assert result.exit_code == 0, result.output
    assert observed == {"direct": True, "list_click": True}


def test_manifest_run_reauthenticates_and_retries_the_same_item_after_session_expiry(config_file: Path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    item = DiscoveredDoneItem(
        workitem_id_text="synthetic-auth-workitem", title="合成会话恢复事项",
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc), completed_at=None,
        sender="合成人员", deadline_text=None, category=None, ordinal=1,
    )
    observed = {"login_calls": 0, "direct_calls": 0, "reopened_page": None}

    class FakePage:
        def wait_for_timeout(self, _milliseconds): pass

    class FakeBrowser:
        base_url = "https://oa.invalid"
        page = FakePage()
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds):
            observed["login_calls"] += 1
            return LoginState.AUTHENTICATED

    class FakeDone:
        def __init__(self, *_args, **_kwargs): pass
        def open_list(self): return object()
        def _list_stats(self, _frame): return 1, 1
        def _discover_frame(self, *_args): return [item]
        def detail_link_for_item(self, _workitem_id): return None
        def navigate_to_page(self, page_number, _delay):
            observed["reopened_page"] = page_number
            return object()

    class FakeDetail:
        def __init__(self, *_args, **_kwargs): pass
        def capture_direct(self, *_args, **_kwargs):
            observed["direct_calls"] += 1
            if observed["direct_calls"] == 1:
                raise AuthRequiredError("synthetic expiry")
            return type("Capture", (), {"attachments": (), "related_containers": ()})()

    def archive_as_no_attachment(_session, proxy, _capture, _data_root):
        proxy.archive_status = "archived"
        proxy.last_error = None

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.cli.DoneAdapter", FakeDone)
    monkeypatch.setattr("oa_knowledge.cli.CollaborationDetailAdapter", FakeDetail)
    monkeypatch.setattr("oa_knowledge.cli.archive_collaboration_detail", archive_as_no_attachment)

    result = runner.invoke(app, [
        "manifest", "run", "--max-pages", "1", "--max-items", "1",
        "--config", str(config_file),
    ])

    assert result.exit_code == 0, result.output
    assert observed == {"login_calls": 2, "direct_calls": 2, "reopened_page": 1}


def test_curate_plan_is_read_only_and_commands_are_registered(config_file: Path) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    help_result = runner.invoke(app, ["curate", "--help"])
    assert help_result.exit_code == 0
    for command in ("plan", "run", "retry", "validate", "report"):
        assert command in help_result.output

    planned = runner.invoke(app, ["curate", "plan", "--config", str(config_file), "--limit", "2"])
    reported = runner.invoke(app, ["curate", "report", "--config", str(config_file)])

    assert planned.exit_code == 0, planned.output
    assert _last_json(planned.output)["packages"] == 0
    assert _last_json(reported.output)["runs"] == 0


def test_convert_synthetic_text_and_unsupported_file(config_file: Path) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    raw = config_file.parent / "data/originals/2026/07/OA-SYNTHETIC"
    raw.mkdir(parents=True)
    (raw / "body.html").write_text("<h1>合成正文</h1>", encoding="utf-8")
    (raw / "example.bin").write_bytes(b"synthetic")
    result = runner.invoke(app, ["convert", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] == 1 and payload["unsupported"] == 1
    output = config_file.parent / "data/markdown/2026/07/OA-SYNTHETIC"
    assert (output / "body.html.md").is_file()
    assert "UNSUPPORTED_FILE_TYPE" in (output / "example.bin.md").read_text(encoding="utf-8")
    assert not (config_file.parent / "data/wiki").exists()
    again = runner.invoke(app, ["convert", "--config", str(config_file)])
    assert json.loads(again.output)["skipped"] == 2


def test_operational_errors_redact_credentials() -> None:
    error = RuntimeError("request failed\n  - cookie: JSESSIONID=secret; token=abc\n  - authorization: Bearer secret")  # public-release: synthetic
    cleaned = _sanitize_operational_error(error)
    assert "secret" not in cleaned
    assert "abc" not in cleaned
    assert cleaned.count("[credential header redacted]") == 2


def test_missing_database_has_nonzero_exit(config_file: Path) -> None:
    assert runner.invoke(app, ["status", "--config", str(config_file)]).exit_code == 1
    assert runner.invoke(app, ["audit", "--config", str(config_file)]).exit_code == 1


def test_notifications_status_reports_state(config_file: Path) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    result = runner.invoke(app, ["notifications", "status", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    payload = _last_json(result.output)
    assert payload["feishu_state"] in {"disabled", "missing_webhook"}


def test_notifications_test_feishu_exits_nonzero_when_not_ready(config_file: Path) -> None:
    # No webhook/secret and feishu disabled -> must fail closed, never send.
    result = runner.invoke(app, ["notifications", "test-feishu", "--config", str(config_file)])
    assert result.exit_code == 1, result.output
    assert "feishu not ready" in result.output


def test_notifications_test_feishu_sends_synthetic(config_file: Path, monkeypatch) -> None:
    import yaml

    from oa_knowledge.notifications.models import DeliveryResult
    monkeypatch.setenv("FEISHU_OA_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")
    monkeypatch.setenv("FEISHU_OA_SECRET", "secret")
    # Enable Feishu so the runtime check passes before the (mocked) send.
    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    cfg.setdefault("feishu", {})["enabled"] = True
    config_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.FeishuService.send_test",
        lambda self, **kw: DeliveryResult("sent", False),
    )
    result = runner.invoke(app, ["notifications", "test-feishu", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert _last_json(result.output)["status"] == "sent"


def test_notifications_retry_reports_status(config_file: Path, monkeypatch) -> None:
    from oa_knowledge.notifications.models import DeliveryResult
    monkeypatch.setattr(
        "oa_knowledge.notifications.feishu_service.retry_pending_summary_delivery",
        lambda engine, settings, delivery_id: DeliveryResult("sent", False),
    )
    result = runner.invoke(app, ["notifications", "retry", "7", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert _last_json(result.output)["status"] == "sent"


def test_doctor_does_not_contact_oa(config_file: Path) -> None:
    runner.invoke(app, ["init", "--config", str(config_file)])
    result = runner.invoke(app, ["doctor", "--config", str(config_file)])
    assert "https://" not in result.output
    assert "sqlite" in result.output


def test_pending_discover_persists_bounded_synthetic_rows(config_file: Path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0

    class FakeBrowser:
        def __init__(self, *_args, **_kwargs):
            self.page = object()
            self.base_url = "https://oa.synthetic.invalid"
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds): return LoginState.AUTHENTICATED

    item = DiscoveredPendingItem(
        affair_id_text="synthetic-affair", title="Synthetic", sender="Synthetic Sender",
        previous_approver=None, initiated_at=None, received_at=None, deadline_text=None,
        reminder_count=0, processing_status="待处理", current_node="经办", importance=None, ordinal=1,
    )

    class FakeAdapter:
        def __init__(self, *_args, **_kwargs): pass
        def discover_pages(self, limit, max_pages, page_delay_seconds):
            assert limit == 3 and max_pages == 1
            return PendingDiscovery((item,), 1, 1, 1, 1, 1)

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.cli.PendingAdapter", FakeAdapter)
    result = runner.invoke(app, ["pending", "discover", "--limit", "3", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "created": 1, "updated": 0, "unchanged": 0, "closed": 0, "reactivated": 0, "pages_scanned": 1,
        "query_count": 1, "source_total_count": 1,
    }


def test_pending_discover_accepts_complete_list_bounds(config_file: Path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0

    class FakeBrowser:
        def __init__(self, *_args, **_kwargs):
            self.page = object()
            self.base_url = "https://oa.synthetic.invalid"
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds): return LoginState.AUTHENTICATED

    class FakeAdapter:
        def __init__(self, *_args, **_kwargs): pass
        def discover_pages(self, limit, max_pages, page_delay_seconds):
            assert limit == 30 and max_pages == 2
            return PendingDiscovery((), 2, 0, 30, 30, 2)

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.cli.PendingAdapter", FakeAdapter)
    result = runner.invoke(app, [
        "pending", "discover", "--limit", "30", "--max-pages", "2",
        "--config", str(config_file),
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source_total_count"] == 30


def test_pending_identity_inspection_accepts_complete_pending_count(config_file: Path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0

    class FakePage:
        def goto(self, *_args, **_kwargs): return None
        def wait_for_timeout(self, *_args): return None

    class FakeBrowser:
        def __init__(self, *_args, **_kwargs):
            self.page = FakePage()
            self.base_url = "https://oa.synthetic.invalid"
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds): return LoginState.AUTHENTICATED

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    result = runner.invoke(app, [
        "pending", "inspect-identities", "--limit", "30", "--config", str(config_file),
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"inspected": 0, "failed": 0}


def test_audit_reports_corrupt_database(config_file: Path) -> None:
    database = load_settings(config_file).database_path
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not sqlite")
    result = runner.invoke(app, ["audit", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "database_corrupt" in result.output


def test_batch_plan_show_freeze_and_cancel(config_file: Path) -> None:
    assert runner.invoke(app, ["init", "--config", str(config_file)]).exit_code == 0
    args = ["batch", "plan", "--from", "2026-07-01", "--to", "2026-07-31", "--max-items", "20", "--config", str(config_file)]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    first_payload = json.loads(first.output)
    assert first_payload["created"] is True
    assert json.loads(second.output)["created"] is False
    key = first_payload["batch_key"]
    shown = runner.invoke(app, ["batch", "show", key, "--config", str(config_file)])
    assert json.loads(shown.output)["planned_limit"] == 20
    frozen = runner.invoke(app, ["batch", "freeze", key, "--config", str(config_file)])
    assert json.loads(frozen.output)["frozen"] is True
    cancelled = runner.invoke(app, ["batch", "cancel", key, "--config", str(config_file)])
    assert cancelled.exit_code == 1


def test_batch_cancel_before_freeze(config_file: Path) -> None:
    runner.invoke(app, ["init", "--config", str(config_file)])
    planned = runner.invoke(app, ["batch", "plan", "--from", "2026-06-01", "--to", "2026-06-30", "--config", str(config_file)])
    key = json.loads(planned.output)["batch_key"]
    cancelled = runner.invoke(app, ["batch", "cancel", key, "--config", str(config_file)])
    assert json.loads(cancelled.output)["status"] == "cancelled"


def test_batch_discover_skips_batch_that_is_already_ready(config_file: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--config", str(config_file)])
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        session.add(CollectionBatch(
            batch_key="already-discovered",
            plan_hash="d" * 64,
            source_channel="done",
            planned_limit=20,
            discovered_count=20,
            query_count=20,
            status="ready",
            frozen_at=datetime.now(timezone.utc),
        ))
        session.commit()

    class BrowserMustNotStart:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("browser started for an already-discovered batch")

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", BrowserMustNotStart)
    result = runner.invoke(app, [
        "batch", "discover", "already-discovered", "--config", str(config_file),
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "ready"


def test_audit_detects_batch_manifest_mismatch(config_file: Path) -> None:
    runner.invoke(app, ["init", "--config", str(config_file)])
    planned = runner.invoke(app, ["batch", "plan", "--from", "2026-05-01", "--to", "2026-05-31", "--config", str(config_file)])
    assert planned.exit_code == 0
    database = load_settings(config_file).database_path
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE collection_batches SET discovered_count=1")
    audited = runner.invoke(app, ["audit", "--config", str(config_file)])
    assert audited.exit_code == 1
    assert "batch_manifest_count_mismatch" in audited.output


def test_batch_run_stops_after_first_item_failure(config_file: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--config", str(config_file)])
    settings = load_settings(config_file)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        batch = CollectionBatch(
            batch_key="stop-first-failure", plan_hash="e" * 64, source_channel="done",
            planned_limit=2, discovered_count=2, query_count=2, status="ready",
        )
        batch.items.extend([
            BatchItem(oa_item_key="fail-1", workitem_id_text="1", title="第一条", ordinal=1),
            BatchItem(oa_item_key="pending-2", workitem_id_text="2", title="第二条", ordinal=2),
        ])
        session.add(batch)
        session.commit()

    class FakePage:
        def wait_for_timeout(self, _milliseconds): pass

    class FakeBrowser:
        base_url = "https://example.invalid"
        page = FakePage()
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def login_with_saved_credentials(self, _seconds): return LoginState.AUTHENTICATED

    class FakeDone:
        def __init__(self, *_args, **_kwargs): pass
        def open_list(self): return object()
        def navigate_to_page(self, *_args, **_kwargs): return None

    class FakeDetail:
        def __init__(self, *_args, **_kwargs): pass
        def capture(self, *_args, **_kwargs): raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr("oa_knowledge.cli.BrowserSession", FakeBrowser)
    monkeypatch.setattr("oa_knowledge.cli.DoneAdapter", FakeDone)
    monkeypatch.setattr("oa_knowledge.cli.CollaborationDetailAdapter", FakeDetail)
    result = runner.invoke(app, [
        "batch", "run", "stop-first-failure", "--max-items", "2",
        "--time-budget-seconds", "60", "--config", str(config_file),
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["run_status"] == "item_failed"
    with Session(engine) as session:
        items = session.query(BatchItem).order_by(BatchItem.ordinal).all()
        assert [item.archive_status for item in items] == ["collect_failed", "pending"]
