"""Pending (待办) data cleanup tests (plan-0807-1 §6, §13.1-§13.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import load_settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, ItemOccurrence, ItemSnapshot, LogicalItem,
    NotificationDelivery, OAItem, SourceAttachment, SummaryVersion,
)
from oa_knowledge.pending_cleanup import (
    CLEANED,
    NOT_ELIGIBLE,
    cleanup_eligibility,
    maybe_cleanup_after_delivery,
    perform_cleanup,
)


def _build_graph(session: Session, settings, *, delivery_status, key="pend:1"):
    logical = LogicalItem(logical_key=key, title="T", lifecycle_status="pending")
    session.add(logical)
    session.flush()
    occ = ItemOccurrence(
        logical_item_id=logical.id, occurrence_key=key, channel="pending",
        title="机密标题", sender="张三", current_node="审批中", occurrence_status="active",
        discovery_hash="h1", raw_fields_json='{"x": 1}',
    )
    session.add(occ)
    session.flush()

    oa = OAItem(oa_item_key="pending:oa:1", logical_item_id=logical.id, source_channel="pending",
                title="T", archive_relpath="pending/1")
    session.add(oa)
    session.flush()

    co = ContentObject(sha256="a" * 64, size_bytes=10)
    session.add(co)
    session.flush()

    af = ArchivedFile(
        oa_item_id=oa.id, attachment_key="att1", file_role="direct_attachment",
        source_container_key="c1", original_name="tmp.pdf",
        local_relpath="pending/1/tmp.pdf", content_object_id=co.id,
        download_status="verified", sha256="a" * 64,
    )
    session.add(af)
    session.flush()

    snap = ItemSnapshot(logical_item_id=logical.id, occurrence_id=occ.id, snapshot_kind="body",
                        version=1, content_hash="c1", payload_json='{"body": "secret"}')
    session.add(snap)
    session.flush()

    sa = SourceAttachment(snapshot_id=snap.id, source_file_id=af.id, source_key="k", ordinal=1,
                          role="main", original_name="tmp.pdf", download_status="verified",
                          content_object_id=co.id)
    session.add(sa)

    sv = SummaryVersion(logical_item_id=logical.id, snapshot_id=snap.id, summary_kind="pending",
                        version=1, status="current", input_hash="i1",
                        structured_json='{"matter_type": "x"}', provider_name="ollama",
                        model_name="m", prompt_version="v", schema_valid=True)
    session.add(sv)

    dl = NotificationDelivery(logical_item_id=logical.id, snapshot_id=snap.id, channel="feishu",
                              notification_type="pending_summary",
                              idempotency_key=f"feishu:pending:{logical.id}:i1",
                              status=delivery_status)
    if delivery_status == "sent":
        dl.sent_at = datetime.now(timezone.utc)
    session.add(dl)
    session.commit()
    return occ, logical, oa, af, snap, dl


def _write_temp_file(settings) -> None:
    path = settings.archive_root / "pending/1/tmp.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"secret-pending-attachment")


def test_successful_delivery_cleans_business_data_and_keeps_ledger(config_file, tmp_path):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        occ, logical, oa, af, snap, dl = _build_graph(session, settings, delivery_status="sent")
        _write_temp_file(settings)
        now = datetime.now(timezone.utc)

        result = maybe_cleanup_after_delivery(session, occ, dl, settings, now)
        session.commit()

        assert result is not None
        assert occ.cleanup_status == CLEANED
        assert occ.title is None and occ.sender is None and occ.current_node is None
        assert occ.raw_fields_json == "{}"
        assert occ.occurrence_status == "cleaned"
        assert occ.notify_fingerprint is not None
        assert occ.allow_renotify is False
        # ledger retained
        assert occ.discovery_hash == "h1"

        # business rows erased
        assert session.get(ItemSnapshot, snap.id) is None
        assert session.get(SourceAttachment, sa_id(session, snap.id)) is None
        assert session.get(OAItem, oa.id) is None
        assert session.get(ArchivedFile, af.id) is None
        # delivery (minimal ledger) retained
        assert session.get(NotificationDelivery, dl.id) is not None
        # physical file erased
        assert not (settings.archive_root / "pending/1/tmp.pdf").exists()


def sa_id(session, snapshot_id):
    return session.scalar(select(SourceAttachment.id).where(SourceAttachment.snapshot_id == snapshot_id))


def test_failed_delivery_is_not_cleaned(config_file):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        occ, logical, oa, af, snap, dl = _build_graph(session, settings, delivery_status="failed")
        _write_temp_file(settings)
        now = datetime.now(timezone.utc)

        result = maybe_cleanup_after_delivery(session, occ, dl, settings, now)
        session.commit()

        assert result is None
        assert occ.cleanup_status in {None, NOT_ELIGIBLE}
        assert occ.title == "机密标题"
        assert session.get(ItemSnapshot, snap.id) is not None
        assert (settings.archive_root / "pending/1/tmp.pdf").exists()


def test_unknown_outcome_is_not_cleaned_and_not_resent(config_file):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        occ, logical, oa, af, snap, dl = _build_graph(session, settings, delivery_status="unknown_outcome")
        _write_temp_file(settings)
        now = datetime.now(timezone.utc)

        eligible, reason = cleanup_eligibility(occ, dl, settings, now)
        assert eligible is False
        assert reason == "delivery_not_sent:unknown_outcome"

        result = maybe_cleanup_after_delivery(session, occ, dl, settings, now)
        session.commit()
        assert result is None
        assert occ.title == "机密标题"
        assert (settings.archive_root / "pending/1/tmp.pdf").exists()


def test_force_cleanup_respects_config_flag(config_file):
    settings = load_settings(config_file)
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as session:
        occ, logical, oa, af, snap, dl = _build_graph(session, settings, delivery_status="failed")
        _write_temp_file(settings)
        now = datetime.now(timezone.utc)

        # allow_force_cleanup defaults True -> force works
        perform_cleanup(session, occ, settings, now, force=True)
        session.commit()
        assert occ.cleanup_status == CLEANED

        # disabled -> force raises
        settings.pending_cleanup.allow_force_cleanup = False
        occ2, *_ = _build_graph(session, settings, delivery_status="failed", key="pend:2")
        from sqlalchemy.orm import Session as S2
        with S2(engine) as s2:
            occ2r = s2.get(ItemOccurrence, occ2.id)
            try:
                perform_cleanup(s2, occ2r, settings, now, force=True)
                assert False, "expected ValueError"
            except ValueError as exc:
                assert "force_cleanup_disabled" in str(exc)
