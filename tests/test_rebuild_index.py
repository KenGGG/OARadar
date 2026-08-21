"""Synthetic contracts for one complete, rebuild-only item index."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.index import RebuildIndexError, publish_rebuilt_index
from oa_knowledge.rebuild.markdown import _publication_sha
from oa_knowledge.rebuild.parser import _tree_sha256
from oa_knowledge.rebuild.paths import markdown_item_relpath, resolve_rebuild_path


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
def run_id(session: Session) -> int:
    run = PipelineRun(run_key="synthetic-index-run", pipeline_type="data_rebuild")
    session.add(run)
    session.commit()
    return run.id


@pytest.fixture
def rebuilt_item(session: Session, settings: Settings, run_id: int) -> OAItem:
    item = OAItem(
        oa_item_key="done:synthetic-index",
        workitem_id_text="WORK-INDEX",
        source_channel="done",
        title="合成索引事项",
        document_number="示例〔2026〕12号",
        document_date=date(2026, 8, 20),
        initiated_at=datetime(2026, 8, 19, tzinfo=UTC),
        completed_at=datetime(2026, 8, 21, tzinfo=UTC),
        sender="合成发起人",
        department="合成机构",
        classification_state="confirmed",
        classification_source="manual",
        source_type="internal",
        internal_category="风险管理",
    )
    session.add(item)
    session.commit()
    body = _source(session, settings, run_id, item, "合成正文.pdf", "official_body")
    attachment = _source(
        session, settings, run_id, item, "预算表.xlsx", "direct_attachment"
    )
    _markdown(session, settings, run_id, item, body, kind="body_markdown")
    _markdown(session, settings, run_id, item, attachment, kind="attachment_markdown")
    return item


def _source(
    session: Session,
    settings: Settings,
    run_id: int,
    item: OAItem,
    name: str,
    role: str,
) -> ArchivedFile:
    content = f"synthetic original {name}".encode()
    source = ArchivedFile(
        oa_item_id=item.id,
        original_name=name,
        attachment_key=f"key-{name}",
        file_role=role,
        source_container_key="root",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        download_status="verified",
    )
    session.add(source)
    session.flush()
    relpath = f"archive/synthetic/{item.id}/{source.id}-{name}"
    target = resolve_rebuild_path(settings, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    session.add(
        RebuildOutput(
            run_id=run_id,
            oa_item_id=item.id,
            source_file_id=source.id,
            kind="original",
            target_relpath=relpath,
            sha256=source.sha256,
            status="success",
            error_code=None,
        )
    )
    session.commit()
    return source


def _markdown(
    session: Session,
    settings: Settings,
    run_id: int,
    item: OAItem,
    source: ArchivedFile,
    *,
    kind: str,
) -> RebuildOutput:
    parse_relpath = f"parse/{run_id}/{source.id}/synthetic"
    parse_target = resolve_rebuild_path(settings, parse_relpath)
    parse_target.mkdir(parents=True, exist_ok=True)
    (parse_target / "document.md").write_text("# Synthetic parse\n", encoding="utf-8")
    parse_sha256 = _tree_sha256(parse_target)
    assert parse_sha256 is not None
    session.add(
        RebuildOutput(
            run_id=run_id,
            oa_item_id=item.id,
            source_file_id=source.id,
            kind="parse",
            target_relpath=parse_relpath,
            sha256=parse_sha256,
            status="success",
            error_code=None,
        )
    )
    item_path = markdown_item_relpath(item)
    filename = (
        "示例〔2026〕12号-合成索引事项-正文.md"
        if kind == "body_markdown"
        else f"{source.original_name}.md"
    )
    relpath = (item_path / filename).as_posix()
    target = resolve_rebuild_path(settings, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Synthetic {kind}\n".encode()
    target.write_bytes(content)
    output = RebuildOutput(
        run_id=run_id,
        oa_item_id=item.id,
        source_file_id=source.id,
        kind=kind,
        target_relpath=relpath,
        sha256=_publication_sha(target, None),
        status="success",
        error_code=None,
    )
    session.add(output)
    session.commit()
    return output


def test_numbered_index_links_body_originals_and_markdown(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """Removing any index section leaves a complete rebuilt item undiscoverable."""
    output = publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)

    text = resolve_rebuild_path(settings, output.target_relpath).read_text(
        encoding="utf-8"
    )

    assert output.kind == "item_index"
    assert "## 正文" in text
    assert "示例〔2026〕12号-合成索引事项-正文.md" in text
    assert "## 原始附件" in text
    assert "合成正文.pdf" in text and "预算表.xlsx" in text
    assert "## 附件 Markdown" in text
    assert "预算表.xlsx.md" in text
    assert str(settings.data_root) not in text
    assert str(settings.rebuild.target_root) not in text


def test_index_is_unique_per_run_and_item(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """A replay must return its verified index rather than create a second ledger row."""
    first = publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)
    second = publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)

    assert second.id == first.id


def test_index_rejects_missing_numbered_body(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """A numbered item cannot claim completion until its one required body exists."""
    body = next(
        output
        for output in session.query(RebuildOutput).all()
        if output.kind == "body_markdown"
    )
    resolve_rebuild_path(settings, body.target_relpath).unlink()

    with pytest.raises(RebuildIndexError, match="BODY_MARKDOWN_UNAVAILABLE"):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)


def test_index_rejects_a_body_for_an_unnumbered_item(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """Removing the document number makes every body Markdown an invalid extra output."""
    rebuilt_item.document_number = None
    session.commit()

    with pytest.raises(RebuildIndexError, match="UNNUMBERED_BODY_MARKDOWN"):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)


def test_index_rejects_a_tampered_attachment_markdown(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """A success row cannot hide Markdown that no longer matches its ledger hash."""
    attachment = next(
        output
        for output in session.query(RebuildOutput).all()
        if output.kind == "attachment_markdown"
    )
    resolve_rebuild_path(settings, attachment.target_relpath).write_text(
        "tampered", encoding="utf-8"
    )

    with pytest.raises(RebuildIndexError, match="ATTACHMENT_MARKDOWN_UNAVAILABLE"):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)


def test_index_requires_a_confirmed_classification(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """An index is a formal output, not a draft for an unconfirmed item."""
    rebuilt_item.classification_state = "needs_review"
    session.commit()

    with pytest.raises(
        RebuildIndexError, match="CONFIRMED_CLASSIFICATION_AND_DATE_REQUIRED"
    ):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)


def test_index_requires_an_effective_date(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """A confirmed item without every date fallback has no valid output directory."""
    rebuilt_item.document_date = None
    rebuilt_item.initiated_at = None
    rebuilt_item.completed_at = None
    session.commit()

    with pytest.raises(
        RebuildIndexError, match="CONFIRMED_CLASSIFICATION_AND_DATE_REQUIRED"
    ):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)


def test_index_lists_terminal_unsupported_and_retryable_attachment_failure(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """Unsupported and retryable conversions are visible without faking Markdown."""
    attachment = next(
        source
        for source in rebuilt_item.files
        if source.file_role == "direct_attachment"
    )
    markdown = next(
        output
        for output in session.query(RebuildOutput).all()
        if output.kind == "attachment_markdown"
    )
    session.delete(markdown)
    session.add(
        RebuildOutput(
            run_id=run_id,
            oa_item_id=rebuilt_item.id,
            source_file_id=attachment.id,
            kind="parse",
            target_relpath=f"parse/{run_id}/{attachment.id}/unsupported",
            sha256=None,
            status="failed",
            error_code="UNSUPPORTED_FORMAT",
        )
    )
    retry = _source(
        session, settings, run_id, rebuilt_item, "待重试.docx", "associated_document"
    )
    session.add(
        RebuildOutput(
            run_id=run_id,
            oa_item_id=rebuilt_item.id,
            source_file_id=retry.id,
            kind="parse",
            target_relpath=f"parse/{run_id}/{retry.id}/failed",
            sha256=None,
            status="failed",
            error_code="PARSER_TIMEOUT",
        )
    )
    session.commit()

    output = publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)
    text = resolve_rebuild_path(settings, output.target_relpath).read_text(
        encoding="utf-8"
    )

    assert "预算表.xlsx：暂不支持转换" in text
    assert "待重试.docx：转换失败，等待重试" in text


def test_index_rejects_another_runs_global_target(
    session: Session,
    settings: Settings,
    run_id: int,
    rebuilt_item: OAItem,
) -> None:
    """Only an exact same-run item-index owner may reuse the final target."""
    target_relpath = (markdown_item_relpath(rebuilt_item) / "_index.md").as_posix()
    other = PipelineRun(run_key="synthetic-index-other", pipeline_type="data_rebuild")
    session.add(other)
    session.flush()
    session.add(
        RebuildOutput(
            run_id=other.id,
            oa_item_id=rebuilt_item.id,
            source_file_id=None,
            kind="item_index",
            target_relpath=target_relpath,
            sha256=hashlib.sha256(b"foreign").hexdigest(),
            status="success",
            error_code=None,
        )
    )
    session.commit()

    with pytest.raises(RebuildIndexError, match="TARGET_CONFLICT"):
        publish_rebuilt_index(session, settings, run_id, rebuilt_item.id)
