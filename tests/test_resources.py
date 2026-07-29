from datetime import datetime, timedelta, timezone
from pathlib import Path

from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.resources import ResourceCoordinator


def _coordinator(tmp_path: Path) -> ResourceCoordinator:
    db = tmp_path / "oa.db"
    upgrade_database(db)
    return ResourceCoordinator(create_db_engine(db))


def test_mineru_and_local_llm_share_exclusive_gpu_lease(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    mineru = coordinator.acquire("mineru", "worker-a", ttl_seconds=60, uses_local_gpu=True)

    assert mineru is not None
    assert coordinator.acquire("local_llm", "worker-b", ttl_seconds=60, uses_local_gpu=True) is None


def test_remote_llm_can_run_while_mineru_holds_gpu(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    assert coordinator.acquire("mineru", "worker-a", ttl_seconds=60, uses_local_gpu=True)
    assert coordinator.acquire("remote_llm", "worker-b", ttl_seconds=60, uses_local_gpu=False)


def test_only_owner_can_heartbeat_or_release(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    lease = coordinator.acquire("mineru", "owner", ttl_seconds=60, uses_local_gpu=True)

    assert lease is not None
    assert not coordinator.heartbeat(lease, "other", ttl_seconds=60)
    assert not coordinator.release(lease, "other")
    assert coordinator.release(lease, "owner")


def test_expired_lease_is_recovered_after_process_restart(tmp_path: Path) -> None:
    first = _coordinator(tmp_path)
    lease = first.acquire("mineru", "dead-worker", ttl_seconds=60, uses_local_gpu=True)
    assert lease is not None
    first.expire_for_test(lease, datetime.now(timezone.utc) - timedelta(seconds=1))

    second = _coordinator(tmp_path)
    replacement = second.acquire("local_llm", "new-worker", ttl_seconds=60, uses_local_gpu=True)

    assert replacement is not None
