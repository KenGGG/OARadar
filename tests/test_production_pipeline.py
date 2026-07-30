from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ItemOccurrence, LogicalItem, OAManifestItem, PipelineTask
from oa_knowledge.production_pipeline import ProductionQueue
from oa_knowledge.web.lifecycle_views import processing_center


def _queue(config_file: Path) -> tuple[ProductionQueue, object]:
    settings = load_settings(config_file)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    return ProductionQueue(engine), settings


def test_realtime_pending_and_done_are_claimed_before_historical(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    queue.enqueue("historical_done_backfill", "item-3", "parse", "history-3")
    queue.enqueue("realtime_done", "item-2", "detail_sync", "done-2")
    queue.enqueue("realtime_pending", "item-1", "detail_sync", "pending-1")

    first = queue.claim("worker-a")
    second = queue.claim("worker-a")
    third = queue.claim("worker-a")

    assert [first.queue_name, second.queue_name, third.queue_name] == [
        "realtime_pending", "realtime_done", "historical_done_backfill",
    ]


def test_historical_wave_finishes_parse_before_ollama(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    ollama_id = queue.enqueue("historical_done_backfill", "item-1", "ollama_extract", "history-1")
    parse_id = queue.enqueue("historical_done_backfill", "item-2", "parse", "history-2")

    first = queue.claim("worker-a")

    assert first.id == parse_id
    assert first.id != ollama_id


def test_historical_wave_does_not_block_realtime(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    queue.enqueue("historical_done_backfill", "item-1", "parse", "history-1")
    realtime_id = queue.enqueue("realtime_pending", "item-2", "detail_sync", "pending-2")

    assert queue.claim("worker-a").id == realtime_id


def test_enqueue_is_idempotent_and_survives_new_queue_instance(config_file: Path) -> None:
    queue, settings = _queue(config_file)
    first = queue.enqueue("realtime_pending", "logical-7", "summarize", "same-key")
    second = ProductionQueue(create_db_engine(settings.database_path)).enqueue(
        "realtime_pending", "logical-7", "summarize", "same-key"
    )

    assert first == second
    with Session(queue.engine) as session:
        assert session.query(PipelineTask).count() == 1


def test_pausing_history_does_not_pause_realtime_claims(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    queue.set_historical_paused(True)
    queue.enqueue("historical_done_backfill", "item-3", "parse", "history-3")
    queue.enqueue("realtime_done", "item-2", "detail_sync", "done-2")

    claimed = queue.claim("worker-a")

    assert claimed.queue_name == "realtime_done"
    assert queue.claim("worker-a") is None


def test_processing_center_reports_real_queue_state(config_file: Path) -> None:
    queue, settings = _queue(config_file)
    queue.enqueue("realtime_pending", "logical-1", "ollama_summary", "pending-1")
    queue.enqueue("historical_done_backfill", "logical-2", "parse", "history-2")
    queue.set_historical_paused(True)

    result = processing_center(settings)

    assert result["queues"]["realtime_pending"]["queued"] == 1
    assert result["queues"]["historical_done_backfill"]["queued"] == 1
    assert result["historical_paused"] is True
    assert result["mock_data"] is False


def test_processing_center_reports_history_idle_without_active_work(config_file: Path) -> None:
    _, settings = _queue(config_file)

    result = processing_center(settings)

    assert result["historical_state"] == "idle"


def test_bootstrap_enqueues_pending_and_unparsed_done_without_duplicates(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    with Session(queue.engine) as session:
        logical = LogicalItem(logical_key="pending:1", title="新待办")
        session.add(logical); session.flush()
        session.add(ItemOccurrence(logical_item_id=logical.id, occurrence_key="p-1", channel="pending", occurrence_status="active"))
        session.add(OAManifestItem(oa_item_key="d-1", title="历史已办", list_page=1, processing_status="downloaded"))
        session.commit()

    first = queue.bootstrap_current_state()
    second = queue.bootstrap_current_state()

    assert first == {"realtime_pending": 1, "historical_done_backfill": 1}
    assert second == {"realtime_pending": 0, "historical_done_backfill": 0}


def test_finish_and_retry_are_durable_and_do_not_block_next_task(config_file: Path) -> None:
    queue, _ = _queue(config_file)
    failed_id = queue.enqueue("realtime_pending", "one", "detail_sync", "one")
    next_id = queue.enqueue("realtime_done", "two", "attachment_inventory", "two")
    assert queue.claim("worker-a").id == failed_id

    queue.fail(failed_id, "worker-a", "OA_DETAIL_FETCH_FAILED", "sanitized", recoverable=False)
    assert queue.claim("worker-a").id == next_id
    queue.advance(next_id, "worker-a", "parse", progress_current=1, progress_total=3)

    with Session(queue.engine) as session:
        failed = session.get(PipelineTask, failed_id)
        advanced = session.get(PipelineTask, next_id)
        assert failed.status == "failed"
        assert failed.error_code == "OA_DETAIL_FETCH_FAILED"
        assert advanced.status == "queued"
        assert advanced.stage == "parse"
        assert advanced.attempts == 0
        assert (advanced.progress_current, advanced.progress_total) == (1, 3)
