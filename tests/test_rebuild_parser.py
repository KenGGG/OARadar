"""Synthetic tests for isolated parsing of rebuilt originals."""

from __future__ import annotations

import hashlib
import json
import os
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
    manifest = json.loads((target / ".oaradar-parse.json").read_text(encoding="utf-8"))
    assert manifest == {
        "engine": "stub", "engine_version": "1",
        "source_file_id": source.id, "source_sha256": original.sha256,
    }
    assert not (settings.data_root / "parse").exists()


def test_unsupported_is_explicit(session: Session, settings: Settings, run_id: int) -> None:
    """Unsupported copied evidence is terminally recorded without invoking an engine."""
    original = _rebuilt_original(session, settings, run_id, name="source.exe", copied=b"synthetic")

    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "unsupported"
    assert result.engine == "none"
    assert result.error_code == "UNSUPPORTED_FORMAT"
    output = session.scalar(select(RebuildOutput).where(RebuildOutput.kind == "parse"))
    assert output is not None
    assert output.status == "failed"
    assert output.error_code == "UNSUPPORTED_FORMAT"
    assert output.sha256 is None


def test_unsupported_converges_legacy_success_ledger_row(
    session: Session, settings: Settings, run_id: int,
) -> None:
    """A legacy success row without an artifact becomes terminal unsupported."""
    original = _rebuilt_original(session, settings, run_id, name="source.exe", copied=b"synthetic")
    relpath = f"parse/{run_id}/{original.source_file_id}/{original.sha256}/unsupported"
    legacy = RebuildOutput(
        run_id=run_id, oa_item_id=original.oa_item_id, source_file_id=original.source_file_id,
        kind="parse", target_relpath=relpath, sha256="legacy-product-sha",
        status="success", error_code=None,
    )
    session.add(legacy)
    session.commit()

    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "unsupported"
    session.expire_all()
    persisted = session.get(RebuildOutput, legacy.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error_code == "UNSUPPORTED_FORMAT"
    assert persisted.sha256 is None


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


def test_pre_promotion_fence_rejects_same_original_row_repointed_mid_parse(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int,
) -> None:
    """A current-row mutation must fence a stale parser before directory promotion."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"first copied bytes")
    source = session.get(ArchivedFile, original.source_file_id)
    assert source is not None

    def mutate_original_then_parse(path: Path, parser_settings: Settings, **kwargs) -> ParseResult:
        replacement = b"second copied bytes"
        replacement_sha = hashlib.sha256(replacement).hexdigest()
        replacement_relpath = f"archive/oa/done/synthetic/replaced-{source.id}.txt"
        replacement_path = resolve_rebuild_path(parser_settings, replacement_relpath)
        replacement_path.parent.mkdir(parents=True, exist_ok=True)
        replacement_path.write_bytes(replacement)
        with Session(session.get_bind()) as concurrent:
            fresh_source = concurrent.get(ArchivedFile, source.id)
            fresh_original = concurrent.get(RebuildOutput, original.id)
            assert fresh_source is not None and fresh_original is not None
            fresh_source.size_bytes, fresh_source.sha256 = len(replacement), replacement_sha
            fresh_original.target_relpath, fresh_original.sha256 = replacement_relpath, replacement_sha
            concurrent.commit()
        return _stub_parser(path, parser_settings, **kwargs)

    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", mutate_original_then_parse)
    result = parse_rebuilt_source(session, settings, run_id, source.id)

    assert result.status == "failed"
    assert result.error_code == "REBUILT_ORIGINAL_CHANGED"
    assert not list(resolve_rebuild_path(settings, "parse").glob("**/stub"))


@pytest.mark.parametrize("inside", (True, False))
def test_parser_output_symlink_is_rejected_without_promotion(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int, inside: bool,
) -> None:
    """Both internal and escaping parser symlinks are unsafe output products."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")

    def parse_with_symlink(source: Path, parser_settings: Settings, *, output_dir: Path | None = None, **kwargs) -> ParseResult:
        result = _stub_parser(source, parser_settings, output_dir=output_dir, **kwargs)
        assert output_dir is not None
        link_target = result.output_path if inside else source
        (output_dir / "unexpected-link").symlink_to(link_target)
        return result

    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", parse_with_symlink)
    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "failed"
    assert result.error_code == "VALUEERROR"
    assert not list(resolve_rebuild_path(settings, "parse").glob("**/stub"))


def test_parser_special_output_entry_is_rejected_without_promotion(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings, run_id: int,
) -> None:
    """A FIFO is neither a parser asset nor a regular file and cannot publish."""
    original = _rebuilt_original(session, settings, run_id, name="source.txt", copied=b"copied bytes")

    def parse_with_fifo(source: Path, parser_settings: Settings, *, output_dir: Path | None = None, **kwargs) -> ParseResult:
        result = _stub_parser(source, parser_settings, output_dir=output_dir, **kwargs)
        assert output_dir is not None
        os.mkfifo(output_dir / "unexpected-fifo")
        return result

    monkeypatch.setattr("oa_knowledge.rebuild.parser.parse_file", parse_with_fifo)
    result = parse_rebuilt_source(session, settings, run_id, original.source_file_id)

    assert result.status == "failed"
    assert result.error_code == "VALUEERROR"
    assert not list(resolve_rebuild_path(settings, "parse").glob("**/stub"))
