"""Synthetic tests for isolated parsing of rebuilt originals."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.parsers.router import ParseResult
from oa_knowledge.rebuild.parser import parse_rebuilt_source
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
def run_id(session: Session) -> int:
    run = PipelineRun(run_key="synthetic-rebuild-parse", pipeline_type="data_rebuild")
    session.add(run)
    session.commit()
    return run.id


def _rebuilt_original(
    session: Session, settings: Settings, run_id: int, *, name: str, copied: bytes,
) -> RebuildOutput:
    item = OAItem(
        oa_item_key=f"done:parse:{name}", source_channel="done", title="Synthetic parse item",
        initiated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    digest = hashlib.sha256(copied).hexdigest()
    file = ArchivedFile(
        oa_item_id=item.id, original_name=name, attachment_key=f"attachment:{name}",
        file_role="direct_attachment", source_container_key="root",
        local_relpath=f"archive/raw/oa/done/synthetic/{name}", size_bytes=len(copied),
        sha256=digest, download_status="verified",
    )
    session.add(file)
    session.flush()
    relpath = f"archive/oa/done/synthetic/{file.id}-{name}"
    target = resolve_rebuild_path(settings, relpath)
    target.parent.mkdir(parents=True)
    target.write_bytes(copied)
    output = RebuildOutput(
        run_id=run_id, oa_item_id=item.id, source_file_id=file.id, kind="original",
        target_relpath=relpath, sha256=digest, status="success", error_code=None,
    )
    session.add(output)
    session.commit()
    return output


def _stub_parser(source: Path, _settings: Settings, *, output_dir: Path | None = None, **_kwargs) -> ParseResult:
    assert output_dir is not None
    product = output_dir / "stub-v1" / f"{source.stem}.md"
    product.parent.mkdir(parents=True)
    product.write_text(f"# {source.read_text(encoding='utf-8')}\n", encoding="utf-8")
    (product.parent / "asset.txt").write_text("synthetic asset\n", encoding="utf-8")
    return ParseResult(product, "stub", "1", 1.0)


def test_parser_reads_rebuilt_original_not_live_file(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int,
) -> None:
    """Changing the live archive cannot affect the rebuilt parse product."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")
    source = session.get(ArchivedFile, original.source_file_id)
    assert source is not None
    live = settings.data_root / source.local_relpath
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live bytes must never be parsed")
    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", _stub_parser)

    result = parse_rebuilt_source(session, settings, run_id, source.id)

    assert result.status == "success"
    assert result.source_sha256 == original.sha256
    assert result.output_relpath is not None and result.output_relpath.startswith("parse/")
    target = resolve_rebuild_path(settings, result.output_relpath)
    markdown = next((target / "stub-v1").glob("*.md"))
    assert markdown.read_text(encoding="utf-8") == "# copied bytes\n"
    assert not (settings.data_root / "parse").exists()


def test_unsupported_is_explicit(session: Session, settings: Settings, run_id: int) -> None:
    """Unsupported copied evidence is terminally recorded without invoking an engine."""
    original = _rebuilt_original(session, settings, run_id, name="source.exe", copied=b"synthetic")

    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "unsupported"
    assert result.engine == "none"
    assert result.error_code == "UNSUPPORTED_FILE_TYPE"
    output = session.scalar(select(RebuildOutput).where(RebuildOutput.kind == "parse"))
    assert output is not None and output.status == "success"


def test_parser_reuses_verified_current_run_product(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int,
) -> None:
    """A repeat validates and reuses its own final directory rather than reparsing."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")
    calls = 0

    def parse_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _stub_parser(*args, **kwargs)

    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", parse_once)
    first = parse_rebuilt_source(session, settings, run_id, original.source_file_id)
    second = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert first.status == second.status == "success"
    assert second.output_relpath == first.output_relpath
    assert calls == 1
    assert len(list(session.scalars(select(RebuildOutput).where(RebuildOutput.kind == "parse")))) == 1


def test_invalid_rebuilt_original_never_falls_back_to_live_file(
    session: Session, settings: Settings, run_id: int,
) -> None:
    """A stale copied-original success is read-only validation, not a live fallback."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")
    source = session.get(ArchivedFile, original.source_file_id)
    assert source is not None
    live = settings.data_root / source.local_relpath
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live archive must remain unread")
    resolve_rebuild_path(settings, original.target_relpath).unlink()

    result = parse_rebuilt_source(session, settings, run_id, source.id)

    assert result.status == "failed"
    assert result.error_code == "REBUILT_ORIGINAL_UNAVAILABLE"
    session.expire_all()
    assert session.get(RebuildOutput, original.id).status == "success"
    assert not (settings.data_root / "parse").exists()


def test_engine_failure_is_recorded_without_downgrading_the_original(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int,
) -> None:
    """One parser fault remains a failed parse result for that source only."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")

    def fail_parser(*_args, **_kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", fail_parser)
    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "failed"
    assert result.error_code == "RUNTIMEERROR"
    session.expire_all()
    assert session.get(RebuildOutput, original.id).status == "success"
    parse = session.scalar(select(RebuildOutput).where(RebuildOutput.kind == "parse"))
    assert parse is not None and parse.status == "failed"
