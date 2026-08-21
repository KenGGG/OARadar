"""Synthetic tests for deterministic, rebuild-only numbered-item bodies."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.body_source import (
    body_markdown_filename,
    load_verified_page_body,
    select_body_source,
)
from oa_knowledge.rebuild.paths import resolve_rebuild_path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(
        app={"data_root": tmp_path / "live-data"},
        rebuild={"target_root": tmp_path / "clean-rebuild"},
    )
    value.data_root.mkdir(parents=True)
    return value


@pytest.fixture
def session(settings: Settings):
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    with Session(engine) as value:
        yield value


@pytest.fixture
def item(session: Session) -> OAItem:
    value = OAItem(
        oa_item_key="done:synthetic-body-source",
        source_channel="done",
        title="合成标题",
        document_number="示例〔2026〕12号",
        initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(value)
    session.commit()
    return value


def _file(item: OAItem, *, name: str, role: str, depth: int = 1) -> ArchivedFile:
    return ArchivedFile(
        id=None,
        oa_item_id=item.id,
        original_name=name,
        attachment_key=f"synthetic-{name}-{role}-{depth}",
        file_role=role,
        source_container_key="synthetic",
        depth=depth,
        download_status="verified",
    )


def test_no_document_number_has_no_body(item: OAItem) -> None:
    """A missing number must prevent an accidental main-body document."""
    item.document_number = " \t "

    assert select_body_source(item, [_file(item, name="正文.pdf", role="official_body")], True).kind == "none"
    assert body_markdown_filename(item) is None


def test_official_body_role_wins_over_filename_match(item: OAItem, session: Session) -> None:
    """Removing the role preference would choose the wrong attachment."""
    named_body = _file(item, name="示例〔2026〕12号.pdf", role="direct_attachment")
    official_body = _file(item, name="附件.pdf", role="official_body", depth=2)
    session.add_all((named_body, official_body))
    session.commit()

    result = select_body_source(item, [named_body, official_body], True)

    assert result.kind == "attachment"
    assert result.source_file_id == official_body.id
    assert result.reason == "official_body"


def test_filename_signals_choose_attachment_before_page_body(item: OAItem, session: Session) -> None:
    """A matching attachment is stronger evidence than a captured page body."""
    named_body = _file(item, name="合成标题.pdf", role="direct_attachment")
    session.add(named_body)
    session.commit()

    result = select_body_source(item, [named_body], True)

    assert result.kind == "attachment"
    assert result.source_file_id == named_body.id
    assert result.reason == "filename_match"


def test_blank_title_is_not_a_filename_signal(item: OAItem) -> None:
    """An empty title must not turn every attachment into a body candidate."""
    item.title = " \t "

    result = select_body_source(item, [_file(item, name="普通附件.pdf", role="direct_attachment")], True)

    assert result.kind == "page_body"


def test_matching_candidates_break_ties_by_role_depth_then_file_id(item: OAItem, session: Session) -> None:
    """Changing the sort order would make equal evidence non-deterministic."""
    later_direct = _file(item, name="正文-2.pdf", role="direct_attachment", depth=1)
    earlier_deep_official = _file(item, name="正文-1.pdf", role="official_attachment", depth=2)
    earlier_shallow_official = _file(item, name="正文-3.pdf", role="official_attachment", depth=1)
    session.add_all((later_direct, earlier_deep_official, earlier_shallow_official))
    session.commit()

    result = select_body_source(item, [later_direct, earlier_deep_official, earlier_shallow_official], False)

    assert result.source_file_id == earlier_shallow_official.id


def test_page_body_is_fallback_when_no_attachment_matches(item: OAItem) -> None:
    """Removing the fallback would leave a numbered item without available body evidence."""
    result = select_body_source(item, [_file(item, name="普通附件.pdf", role="direct_attachment")], True)

    assert result.kind == "page_body"
    assert result.source_file_id is None
    assert result.reason == "verified_page_body"


def test_body_markdown_filename_has_number_title_and_body_suffix(item: OAItem) -> None:
    """Omitting any semantic filename component would obscure the item identity."""

    assert body_markdown_filename(item) == "示例〔2026〕12号-合成标题-正文.md"


def test_body_markdown_filename_bounds_long_multibyte_number_and_title(item: OAItem) -> None:
    """Long multibyte metadata must retain both semantic components and the suffix."""
    item.document_number = "甲" * 100
    item.title = "乙" * 100

    filename = body_markdown_filename(item)

    assert filename is not None
    number, title, suffix = filename.rsplit("-", maxsplit=2)
    assert number and title
    assert set(number) == {"甲"}
    assert set(title) == {"乙"}
    assert suffix == "正文.md"
    assert filename.endswith("-正文.md")
    assert len(filename.encode("utf-8")) <= 240


def test_body_markdown_filename_rejects_entirely_unsafe_document_number(item: OAItem) -> None:
    """An unsafe number must have the same defined no-filename outcome as no number."""
    item.document_number = '<>:/\\|?*\x00'

    assert body_markdown_filename(item) is None


def test_page_body_loader_reads_only_verified_rebuilt_original(
    session: Session, settings: Settings, item: OAItem,
) -> None:
    """A live snapshot path must never substitute for a verified rebuilt copy."""
    content = b"<html><style>.hidden { display: none; }</style><body><h1>\xe5\x90\x88\xe6\x88\x90\xe6\xad\xa3\xe6\x96\x87</h1><script>hidden()</script></body></html>"
    snapshot = _file(item, name="body.html", role="body_snapshot")
    snapshot.local_relpath = "archive/raw/oa/done/synthetic/live-body.html"
    snapshot.size_bytes = len(content)
    snapshot.sha256 = hashlib.sha256(content).hexdigest()
    session.add(snapshot)
    run = PipelineRun(run_key="synthetic-body-loader", pipeline_type="data_rebuild")
    session.add(run)
    session.flush()
    target_relpath = "archive/oa/done/synthetic/body.html"
    target = resolve_rebuild_path(settings, target_relpath)
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    session.add(RebuildOutput(
        run_id=run.id,
        oa_item_id=item.id,
        source_file_id=snapshot.id,
        kind="original",
        target_relpath=target_relpath,
        sha256=snapshot.sha256,
        status="success",
        error_code=None,
    ))
    session.commit()

    assert load_verified_page_body(session, settings, item.id, run_id=run.id) == "合成正文"


def test_page_body_loader_rejects_unverified_or_tampered_snapshot(
    session: Session, settings: Settings, item: OAItem,
) -> None:
    """Changing verification, target bytes, or run must stop body disclosure."""
    content = b"<p>synthetic body</p>"
    snapshot = _file(item, name="body.html", role="body_snapshot")
    snapshot.size_bytes = len(content)
    snapshot.sha256 = hashlib.sha256(content).hexdigest()
    session.add(snapshot)
    first_run = PipelineRun(run_key="synthetic-body-rejected", pipeline_type="data_rebuild")
    second_run = PipelineRun(run_key="synthetic-body-other-run", pipeline_type="data_rebuild")
    session.add_all((first_run, second_run))
    session.flush()
    target_relpath = "archive/oa/done/synthetic/rejected-body.html"
    target = resolve_rebuild_path(settings, target_relpath)
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    output = RebuildOutput(
        run_id=first_run.id,
        oa_item_id=item.id,
        source_file_id=snapshot.id,
        kind="original",
        target_relpath=target_relpath,
        sha256=snapshot.sha256,
        status="success",
        error_code=None,
    )
    session.add(output)
    session.commit()

    snapshot.download_status = "failed"
    assert load_verified_page_body(session, settings, item.id, run_id=first_run.id) is None
    snapshot.download_status = "verified"
    target.write_bytes(b"tampered")
    assert load_verified_page_body(session, settings, item.id, run_id=first_run.id) is None
    assert output.status == "success"
    assert output.error_code is None
    assert load_verified_page_body(session, settings, item.id, run_id=second_run.id) is None


def test_page_body_loader_skips_stale_old_output_without_mutating_ledger(
    session: Session, settings: Settings, item: OAItem,
) -> None:
    """An old stale copy must not hide a newer valid copy or rewrite either ledger row."""
    content = b"<p>new synthetic body</p>"
    snapshot = _file(item, name="body.html", role="body_snapshot")
    snapshot.size_bytes = len(content)
    snapshot.sha256 = hashlib.sha256(content).hexdigest()
    session.add(snapshot)
    run = PipelineRun(run_key="synthetic-body-newest", pipeline_type="data_rebuild")
    session.add(run)
    session.flush()
    old_relpath = "archive/oa/done/synthetic/old-body.html"
    new_relpath = "archive/oa/done/synthetic/new-body.html"
    old_target = resolve_rebuild_path(settings, old_relpath)
    new_target = resolve_rebuild_path(settings, new_relpath)
    old_target.parent.mkdir(parents=True)
    old_target.write_bytes(b"stale")
    new_target.write_bytes(content)
    old_output = RebuildOutput(
        run_id=run.id, oa_item_id=item.id, source_file_id=snapshot.id, kind="original",
        target_relpath=old_relpath, sha256=snapshot.sha256, status="success", error_code=None,
    )
    new_output = RebuildOutput(
        run_id=run.id, oa_item_id=item.id, source_file_id=snapshot.id, kind="original",
        target_relpath=new_relpath, sha256=snapshot.sha256, status="success", error_code=None,
    )
    session.add_all((old_output, new_output))
    session.commit()

    assert load_verified_page_body(session, settings, item.id, run_id=run.id) == "new synthetic body"
    assert (old_output.status, old_output.error_code) == ("success", None)
    assert (new_output.status, new_output.error_code) == ("success", None)
