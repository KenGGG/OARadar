"""End-to-end auto-run tests (plan-0806-1 §9).

Test three (新增待办 -> 摘要 -> 飞书, 只发送一次) drives the real worker
pipeline with only the LLM summary and the Feishu network call mocked, so it
exercises the durable task/summary/delivery chain exactly as production does.
The other five §9 scenarios are covered by their dedicated test modules:
  * test one   -> tests/test_cli_registration.py::test_schedule_status_runs_on_real_database
  * test two   -> tests/test_schedule_web.py::test_worker_runs_scheduled_hourly_job
  * test four  -> tests/test_worker.py::test_pipeline_done_capture_and_archive_*
  * test five  -> tests/test_web.py::test_built_console_exposes_autorun_strings
  * test six   -> tests/test_systemd_render.py::test_render_passes_systemd_analyze_verify
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ItemOccurrence, ItemSnapshot, LogicalItem, NotificationDelivery, PipelineTask, SummaryVersion,
)
from oa_knowledge.notifications.feishu_service import DeliveryResult
from oa_knowledge.web.worker import OperationWorker


def _setup(config_file: Path) -> tuple[object, int]:
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True, exist_ok=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        logical = LogicalItem(logical_key="e2e:affair-1", title="E2E 待办", lifecycle_status="pending")
        session.add(logical)
        session.flush()
        occ = ItemOccurrence(
            logical_item_id=logical.id, occurrence_key="pending:e2e-1", channel="pending",
            title="E2E 待办", sender="甲", current_node="经办", affair_id_text="affair-1",
            occurrence_status="active",
        )
        session.add(occ)
        session.flush()
        snap = ItemSnapshot(
            logical_item_id=logical.id, occurrence_id=occ.id, snapshot_kind="pending_initial",
            version=1, content_hash="a" * 64, payload_json="{}",
        )
        session.add(snap)
        session.flush()
        task = PipelineTask(
            queue_name="realtime_pending", stage="pending_summary", priority=0,
            logical_item_key=str(logical.id), status="queued",
            idempotency_key=f"e2e:pending_summary:{logical.id}",
            payload_json=f'{{"notify": true, "occurrence_id": {occ.id}, "baseline": false}}',
        )
        session.add(task)
        session.commit()
        lid = logical.id
    engine.dispose()
    return settings, lid


def test_e2e_new_pending_notifies_exactly_once(config_file: Path) -> None:
    """§9 test three: new pending item yields one sent Feishu delivery; re-runs do not resend."""
    settings, lid = _setup(config_file)
    send_calls = {"n": 0}

    def fake_send(self, summary, **fields):  # type: ignore[no-untyped-def]
        send_calls["n"] += 1
        return DeliveryResult("sent", False, error_code=None)

    def fake_summarize(s, engine, logical_item_id):  # type: ignore[no-untyped-def]
        with Session(engine) as session:
            snap = session.scalar(select(ItemSnapshot).where(
                ItemSnapshot.logical_item_id == logical_item_id).order_by(ItemSnapshot.id.desc()))
            session.add(SummaryVersion(
                logical_item_id=logical_item_id, snapshot_id=snap.id, summary_kind="pending",
                version=1, status="current", input_hash="a" * 64,
                structured_json='{"summary":"x","matter_type":"测试","current_stage":"经办",'
                                '"key_points":[],"required_action":"处理","confidence":0.9}',
                provider_name="ollama", model_name="m", prompt_version="v1",
                created_at=datetime.now(timezone.utc),
            ))
            session.commit()

    with patch("oa_knowledge.pending_summary.summarize_pending", fake_summarize), \
         patch("oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary", fake_send), \
         patch("oa_knowledge.config.validate_feishu_runtime_config", lambda settings: "ready"):
        worker = OperationWorker(settings, config_path=config_file)
        try:
            # pending_summary -> advances to notify_feishu
            assert worker.run_once() is True
            # notify_feishu -> sends to Feishu
            assert worker.run_once() is True
            # pending_cleanup runs separately, so a successful send can never
            # be repeated while transient cleanup work is retried.
            assert worker.run_once() is True
            # All three Pending stages are now complete.
            assert worker.run_once() is False
        finally:
            worker.close()

    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            # After a successful Feishu send the business payload is cleaned and
            # only the minimal de-duplication ledger remains (plan-0807-1 §6).
            occ = session.scalar(select(ItemOccurrence).where(ItemOccurrence.logical_item_id == lid))
            assert occ is not None
            assert occ.cleanup_status == "cleaned"
            assert occ.notify_fingerprint is not None
            assert occ.title is None, "business title must be erased after cleanup"
            deliveries = session.scalars(select(NotificationDelivery).where(
                NotificationDelivery.logical_item_id == lid,
                NotificationDelivery.channel == "feishu",
            )).all()
            assert len(deliveries) == 1, "exactly one delivery row must exist"
            assert deliveries[0].status == "sent"
            assert send_calls["n"] == 1, "Feishu must be called exactly once"
    finally:
        engine.dispose()


def test_e2e_unchanged_pending_does_not_resend(config_file: Path) -> None:
    """§7 / §9: an unchanged pending item must not produce a second Feishu send."""
    settings, lid = _setup(config_file)
    send_calls = {"n": 0}

    def fake_send(self, summary, **fields):  # type: ignore[no-untyped-def]
        send_calls["n"] += 1
        return DeliveryResult("sent", False, error_code=None)

    def fake_summarize(s, engine, logical_item_id):  # type: ignore[no-untyped-def]
        with Session(engine) as session:
            snap = session.scalar(select(ItemSnapshot).where(
                ItemSnapshot.logical_item_id == logical_item_id).order_by(ItemSnapshot.id.desc()))
            session.add(SummaryVersion(
                logical_item_id=logical_item_id, snapshot_id=snap.id, summary_kind="pending",
                version=1, status="current", input_hash="a" * 64,
                structured_json='{"summary":"x","matter_type":"测试","current_stage":"经办",'
                                '"key_points":[],"required_action":"处理","confidence":0.9}',
                provider_name="ollama", model_name="m", prompt_version="v1",
                created_at=datetime.now(timezone.utc),
            ))
            session.commit()

    with patch("oa_knowledge.pending_summary.summarize_pending", fake_summarize), \
         patch("oa_knowledge.notifications.feishu_service.FeishuService.send_pending_summary", fake_send), \
         patch("oa_knowledge.config.validate_feishu_runtime_config", lambda settings: "ready"):
        worker = OperationWorker(settings, config_path=config_file)
        try:
            assert worker.run_once() is True
            assert worker.run_once() is True
        finally:
            worker.close()

    # Re-enqueue a notify_feishu task for the same (already sent) logical item.
    engine = create_db_engine(settings.database_path)
    try:
        with Session(engine) as session:
            occ = session.scalar(select(ItemOccurrence).where(ItemOccurrence.logical_item_id == lid))
            session.add(PipelineTask(
                queue_name="realtime_pending", stage="notify_feishu", priority=0,
                logical_item_key=str(lid), status="queued",
                idempotency_key=f"e2e:notify_feishu:{lid}",
                payload_json=f'{{"occurrence_id": {occ.id}}}',
            ))
            session.commit()

        worker = OperationWorker(settings, config_path=config_file)
        try:
            assert worker.run_once() is True
        finally:
            worker.close()

        with Session(engine) as session:
            assert send_calls["n"] == 1, "resending an unchanged item must not call Feishu again"
            assert session.query(NotificationDelivery).filter_by(
                logical_item_id=lid, channel="feishu").count() == 1
    finally:
        engine.dispose()
