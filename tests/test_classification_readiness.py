from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from oa_knowledge.classification.readiness import ArchiveReadinessService
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ContentObject,
    OAManifestItem,
    OAItem,
    OnlineAuditItem,
    OnlineAuditRun,
)
from oa_knowledge.source_roles import AUDIT_ATTACHMENT_ROLES


NOW = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
FILE_SHA = "a" * 64


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session
    engine.dispose()


def _package(
    session: Session,
    *,
    key: str = "done:synthetic-1",
    manifest_status: str = "downloaded",
    no_attachment_confirmed: bool = False,
    file_status: str | None = "verified",
    expected_size: int | None = 12,
    size_bytes: int | None = 12,
    sha256: str | None = FILE_SHA,
    content_sha256: str | None = FILE_SHA,
    local_relpath: str | None = "originals/done/synthetic/source.pdf",
    verified_at: datetime | None = NOW,
    file_role: str = "direct_attachment",
) -> tuple[OAItem, OAManifestItem, ArchivedFile | None]:
    item = OAItem(
        oa_item_key=key,
        source_channel="done",
        title="Synthetic archive item",
        pipeline_status="files_verified",
    )
    manifest = OAManifestItem(
        oa_item_key=key,
        title=item.title,
        list_page=1,
        processing_status=manifest_status,
        no_attachment_confirmed=no_attachment_confirmed,
    )
    session.add_all((item, manifest))
    session.flush()
    if file_status is None:
        return item, manifest, None
    content = None
    if content_sha256 is not None:
        content = ContentObject(sha256=content_sha256, size_bytes=size_bytes)
        session.add(content)
        session.flush()
    file = ArchivedFile(
        oa_item_id=item.id,
        original_name="source.pdf",
        local_relpath=local_relpath,
        size_bytes=size_bytes,
        expected_size=expected_size,
        sha256=sha256,
        content_object_id=content.id if content else None,
        attachment_key="attachment-synthetic-1",
        file_role=file_role,
        source_container_key="root",
        download_status=file_status,
        verified_at=verified_at,
    )
    session.add(file)
    session.flush()
    return item, manifest, file


def _audit(
    session: Session,
    *,
    key: str = "done:synthetic-1",
    status: str,
    depth_limit_reached: bool = False,
    finished_at: datetime = NOW + timedelta(minutes=1),
    recognized_attachments: int | None = 1,
    downloaded_attachments: int = 1,
    online_evidence: tuple[tuple[str, str, int | None, str | None], ...] = (),
    local_evidence: tuple[tuple[str, str, int | None, str | None], ...] = (),
) -> None:
    run = OnlineAuditRun(status="completed", total_items=1, finished_at=finished_at)
    session.add(run)
    session.flush()
    session.add(
        OnlineAuditItem(
            run_id=run.id,
            oa_item_key=key,
            title="Synthetic archive item",
            status=status,
            recognized_attachments=recognized_attachments,
            downloaded_attachments=downloaded_attachments,
            online_evidence_json=json.dumps(
                [
                    {"role": role, "key": key, "size": size, "sha256": sha256}
                    for role, key, size, sha256 in online_evidence
                ]
            ),
            local_evidence_json=json.dumps(
                [
                    {"role": role, "key": key, "size": size, "sha256": sha256}
                    for role, key, size, sha256 in local_evidence
                ]
            ),
            depth_limit_reached=depth_limit_reached,
            finished_at=finished_at,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("setup", "expected_status", "publishable"),
    [
        ({}, "ok", True),
        (
            {"manifest_status": "no_attachment", "no_attachment_confirmed": True, "file_status": None},
            "no_attachment_confirmed",
            True,
        ),
        ({"local_relpath": None}, "missing", False),
        ({"expected_size": 13}, "size_mismatch", False),
        ({"content_sha256": "b" * 64}, "sha256_mismatch", False),
        ({"file_status": "download_failed"}, "download_failed", False),
        ({"file_status": "discovered", "verified_at": None}, "not_checked", False),
    ],
)
def test_assess_exposes_all_seven_integrity_states(
    session: Session,
    setup: dict[str, object],
    expected_status: str,
    publishable: bool,
) -> None:
    _package(session, **setup)

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == expected_status
    assert result.publishable is publishable
    assert not session.dirty


def test_absent_file_rows_are_not_silently_treated_as_no_attachments(session: Session) -> None:
    _package(session, file_status=None)

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "not_checked"
    assert result.publishable is False


def test_explicit_zero_attachment_evidence_does_not_require_a_file_row(session: Session) -> None:
    _package(
        session,
        manifest_status="no_attachment",
        no_attachment_confirmed=True,
        file_status=None,
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "no_attachment_confirmed"
    assert result.reason_codes == ("NO_ATTACHMENT_CONFIRMED",)


def test_non_attachment_evidence_cannot_manufacture_attachment_readiness(session: Session) -> None:
    assert "metadata_snapshot" not in AUDIT_ATTACHMENT_ROLES
    _package(session, file_role="metadata_snapshot")

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "not_checked"
    assert result.publishable is False


def test_non_attachment_evidence_does_not_conflict_with_confirmed_zero_attachments(
    session: Session,
) -> None:
    _package(
        session,
        manifest_status="no_attachment",
        no_attachment_confirmed=True,
        file_role="workflow_snapshot",
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "no_attachment_confirmed"


@pytest.mark.parametrize(
    ("manifest_status", "audit_status", "expected"),
    [
        ("download_failed", None, "download_failed"),
        ("no_attachment", "missing_download", "missing"),
        ("no_attachment", "content_unverified", "not_checked"),
    ],
)
def test_no_attachment_evidence_cannot_override_conflicting_current_evidence(
    session: Session,
    manifest_status: str,
    audit_status: str | None,
    expected: str,
) -> None:
    _package(
        session,
        manifest_status=manifest_status,
        no_attachment_confirmed=True,
        file_status=None,
    )
    if audit_status is not None:
        _audit(session, status=audit_status)

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == expected
    assert result.publishable is False


@pytest.mark.parametrize(
    ("manifest_status", "confirmed"),
    [("downloaded", True), ("no_attachment", False)],
)
def test_zero_attachment_requires_matching_manifest_status_and_flag(
    session: Session, manifest_status: str, confirmed: bool
) -> None:
    _package(
        session,
        manifest_status=manifest_status,
        no_attachment_confirmed=confirmed,
        file_status=None,
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "not_checked"
    assert "ZERO_ATTACHMENT_EVIDENCE_INCOMPLETE" in result.reason_codes


def test_zero_attachment_is_blocked_by_contradictory_latest_audit_counts(session: Session) -> None:
    _package(
        session,
        manifest_status="no_attachment",
        no_attachment_confirmed=True,
        file_status=None,
    )
    _audit(
        session,
        status="matched",
        recognized_attachments=1,
        downloaded_attachments=1,
        online_evidence=(("direct_attachment", "foreign", 10, FILE_SHA),),
        local_evidence=(("direct_attachment", "foreign", 10, FILE_SHA),),
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "missing"
    assert "ZERO_ATTACHMENT_AUDIT_CONFLICT" in result.reason_codes


@pytest.mark.parametrize("source", ["manifest", "audit"])
def test_depth_limit_always_blocks_completion(session: Session, source: str) -> None:
    """Depth truncation is an incomplete inventory, never a completed empty package."""
    _, manifest, _ = _package(
        session,
        manifest_status="no_attachment",
        no_attachment_confirmed=True,
        file_status=None,
    )
    if source == "manifest":
        manifest.processing_status = "depth_limit_reached"
    else:
        _audit(session, status="matched", depth_limit_reached=True)
    session.flush()

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "missing"
    assert result.publishable is False
    assert "DEPTH_LIMIT_REACHED" in result.reason_codes
    assert not session.new
    assert not session.dirty


def test_newest_current_audit_evidence_wins_regardless_of_insert_order(session: Session) -> None:
    _package(session)
    _audit(session, status="content_mismatch", finished_at=NOW + timedelta(minutes=2))
    _audit(session, status="matched", finished_at=NOW + timedelta(minutes=1))

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "sha256_mismatch"


@pytest.mark.parametrize(
    ("audit_status", "expected"),
    [("missing_download", "missing"), ("content_mismatch", "sha256_mismatch")],
)
def test_file_verification_cannot_clear_latest_package_audit_failure(
    session: Session, audit_status: str, expected: str
) -> None:
    """One later file timestamp cannot prove a package discrepancy was repaired."""
    _package(session, verified_at=NOW + timedelta(minutes=2))
    _audit(session, status=audit_status, finished_at=NOW + timedelta(minutes=1))

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == expected


def test_later_successful_package_audit_clears_prior_inventory_failure(session: Session) -> None:
    _package(session)
    _audit(session, status="missing_download", finished_at=NOW + timedelta(minutes=1))
    _audit(session, status="matched", finished_at=NOW + timedelta(minutes=2))

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "ok"


def test_depth_limit_evidence_is_not_cleared_by_a_later_file_verification(session: Session) -> None:
    """Verifying known rows cannot prove that depth-truncated children do not exist."""
    _package(session, verified_at=NOW + timedelta(minutes=2))
    _audit(
        session,
        status="depth_limit_reached",
        depth_limit_reached=True,
        finished_at=NOW + timedelta(minutes=1),
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "missing"
    assert "DEPTH_LIMIT_REACHED" in result.reason_codes


@pytest.mark.parametrize(
    ("audit_status", "expected"),
    [
        ("missing_download", "missing"),
        ("inventory_mismatch", "missing"),
        ("historical_retained", "missing"),
        ("content_mismatch", "sha256_mismatch"),
        ("content_unverified", "not_checked"),
        ("access_failed", "not_checked"),
    ],
)
def test_current_audit_evidence_maps_to_integrity_states(
    session: Session, audit_status: str, expected: str
) -> None:
    _package(session)
    _audit(session, status=audit_status)

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == expected


def test_failure_precedence_is_deterministic_when_multiple_failures_exist(session: Session) -> None:
    """Use the earliest fail-closed gate: missing, transfer, size, hash, unchecked."""
    _, manifest, _ = _package(
        session,
        file_status="download_failed",
        expected_size=99,
        content_sha256="b" * 64,
        local_relpath=None,
        verified_at=None,
    )
    manifest.processing_status = "depth_limit_reached"

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "missing"
    assert result.reason_codes == (
        "DEPTH_LIMIT_REACHED",
        "DOWNLOAD_FAILED",
        "SIZE_MISMATCH",
        "SHA256_MISMATCH",
        "NOT_CHECKED",
    )


@pytest.mark.parametrize(
    "failure_status",
    [
        "failed",
        "error",
        "rejected_zero_byte",
        "rejected_error_page",
        "rejected_type_mismatch",
        "download_failed",
    ],
)
def test_known_transfer_failures_map_to_download_failed_without_false_missing(
    session: Session, failure_status: str
) -> None:
    _package(
        session,
        manifest_status="download_failed",
        file_status=failure_status,
        local_relpath=None,
        verified_at=None,
    )

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "download_failed"
    assert "DOWNLOAD_FAILED" in result.reason_codes
    assert "ARCHIVE_FILE_MISSING" not in result.reason_codes


def test_content_object_size_is_durable_size_mismatch_evidence(session: Session) -> None:
    _, _, file = _package(session, expected_size=None)
    assert file is not None and file.content_object_id is not None
    session.get(ContentObject, file.content_object_id).size_bytes = 99
    session.flush()

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "size_mismatch"


def test_malformed_content_object_hash_is_reported_as_not_checked(session: Session) -> None:
    _, _, file = _package(session)
    assert file is not None and file.content_object_id is not None
    session.get(ContentObject, file.content_object_id).sha256 = "invalid"
    session.flush()

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "not_checked"


def test_readiness_never_reads_the_filesystem(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _package(session)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("classification readiness must not touch attachment bytes")

    monkeypatch.setattr("pathlib.Path.open", forbidden)
    monkeypatch.setattr("pathlib.Path.stat", forbidden)
    monkeypatch.setattr("pathlib.Path.is_file", forbidden)

    result = ArchiveReadinessService().assess(session, "done:synthetic-1")

    assert result.content_integrity_status == "ok"


def test_missing_manifest_or_item_is_missing_without_mutation(session: Session) -> None:
    result = ArchiveReadinessService().assess(session, "done:unknown")

    assert result.content_integrity_status == "missing"
    assert result.reason_codes == ("MANIFEST_MISSING", "ARCHIVED_ITEM_MISSING")
    assert not session.new
    assert not session.dirty


def test_assess_many_handles_full_historical_target_with_bounded_selects(
    session: Session,
) -> None:
    keys = [f"done:bulk-{index}" for index in range(6_144)]
    session.add_all(
        OAItem(
            oa_item_key=key,
            source_channel="done",
            title="Synthetic bulk item",
            pipeline_status="files_verified",
        )
        for key in keys
    )
    session.add_all(
        OAManifestItem(
            oa_item_key=key,
            title="Synthetic bulk item",
            list_page=1,
            processing_status="no_attachment",
            no_attachment_confirmed=True,
        )
        for key in keys
    )
    session.flush()
    selects: list[str] = []

    def record_select(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(session.bind, "before_cursor_execute", record_select)
    try:
        results = ArchiveReadinessService().assess_many(session, keys)
    finally:
        event.remove(session.bind, "before_cursor_execute", record_select)

    assert len(results) == 6_144
    assert all(result.content_integrity_status == "no_attachment_confirmed" for result in results.values())
    assert len(selects) <= 5
    assert sum("online_audit_items" in statement for statement in selects) == 1
