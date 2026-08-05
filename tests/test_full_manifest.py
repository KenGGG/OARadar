from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from oa_knowledge.collector.done import DiscoveredDoneItem, DoneAdapter, DoneDiscovery
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import OAItem, OAManifestItem
from oa_knowledge.full_manifest import classify_manifest, export_manifest_csv, manifest_counts, synchronize_manifest
from oa_knowledge.cli import _audit_opens_detail


def _item(identifier: str, title: str, ordinal: int) -> DiscoveredDoneItem:
    return DiscoveredDoneItem(identifier, title, None, datetime(2026, 7, 19), "合成人", None, "协同", ordinal, 1)


def test_done_grid_structured_rows_preserve_initiation_time_when_columns_are_hidden() -> None:
    rows = [{"id": "42", "subject": "Synthetic", "createDate": "2022-04-22 09:30", "completeTime": "2026-07-01 10:00", "createMemberName": "Tester", "categoryLabel": "协同"}]
    item = DoneAdapter._items_from_grid_rows(rows, 3, 40)[0]
    assert item.created_at == datetime(2022, 4, 22, 9, 30)
    assert item.completed_at == datetime(2026, 7, 1, 10, 0)
    assert item.ordinal == 41


def test_manifest_requires_exact_source_reconciliation_before_classification(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    discovery = DoneDiscovery((_item("1", "请假申请", 1),), 1, 1, 1, 2, 1)
    with Session(engine) as session:
        sync = synchronize_manifest(session, discovery); session.commit()
        assert sync.status == "manifest_incomplete"
        try:
            classify_manifest(session, ("请假",), tmp_path)
        except ValueError as exc:
            assert "manifest_incomplete" in str(exc)
        else:
            raise AssertionError("incomplete manifest must block classification")


def test_manifest_refresh_propagates_initiation_time_to_existing_archive(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    initiated = datetime(2022, 4, 22, 9, 0)
    source = DiscoveredDoneItem("42", "Synthetic", initiated, datetime(2026, 7, 1), None, None, None, 1, 1)
    with Session(engine) as session:
        session.add(OAItem(oa_item_key="done:42", workitem_id_text="42", source_channel="done", title="Synthetic"))
        session.commit()
        synchronize_manifest(session, DoneDiscovery((source,), 1, 1, 1, 1, 1))
        session.commit()
        assert session.query(OAItem).one().initiated_at == initiated


def test_manifest_classifies_skips_and_exports_every_row(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    discovery = DoneDiscovery(
        (_item("1", "请假申请", 1), _item("2", "普通审批", 2)), 1, 2, 2, 2, 1,
    )
    with Session(engine) as session:
        sync = synchronize_manifest(session, discovery)
        counts = classify_manifest(session, ("请假",), tmp_path)
        path = export_manifest_csv(session, tmp_path)
        sync_status = sync.status
        session.commit()
        rows = session.query(OAManifestItem).order_by(OAManifestItem.id).all()
    assert sync_status == "manifest_complete"
    assert [r.processing_status for r in rows] == ["skipped", "pending_download"]
    assert counts == {"local_manifest_count": 2, "skipped": 1, "needs_download": 1, "downloaded": 0, "no_attachment": 0, "download_failed": 0, "auth_required": 0, "pending": 1}
    assert path == tmp_path / "runtime/reports/oa_manifest.csv"
    assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 3


def test_precise_expense_rules_do_not_skip_informational_notice(tmp_path: Path) -> None:
    db = tmp_path / "oa.db"; upgrade_database(db); engine = create_db_engine(db)
    discovery = DoneDiscovery((
        _item("notice", "关于协助采集区属国企财务报销制度及数据的通知", 1),
        _item("form", "差旅费报销申请", 2),
        _item("sheet", "通用报销单", 3),
    ), 1, 3, 3, 3, 1)
    with Session(engine) as session:
        synchronize_manifest(session, discovery)
        classify_manifest(session, ("报销申请", "报销单"), tmp_path)
        session.commit()
        rows = session.query(OAManifestItem).order_by(OAManifestItem.id).all()
    assert [row.processing_status for row in rows] == ["pending_download", "skipped", "skipped"]
    assert [row.matched_exclusion_keyword for row in rows] == [None, "报销申请", "报销单"]


def test_full_audit_reopens_every_status_except_latest_rule_skip() -> None:
    assert _audit_opens_detail("downloaded") is True
    assert _audit_opens_detail("no_attachment") is True
    assert _audit_opens_detail("download_failed") is True
    assert _audit_opens_detail("pending_download") is True
    assert _audit_opens_detail("skipped") is False
