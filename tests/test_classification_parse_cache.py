"""Tests for the classification-only, versioned parse artifact cache."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ContentObject,
    OAItem,
    ParseArtifact,
    ReviewEntry,
)
from oa_knowledge.parsers.router import ParseResult


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "app": {"data_root": str(tmp_path / "data")},
            "runtime": {
                "state_root": str(tmp_path / "state"),
                "cache_root": str(tmp_path / "cache"),
            },
        }
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_file(
    session: Session,
    settings: Settings,
    *,
    key: str,
    filename: str,
    payload: bytes,
    depth: int = 1,
    download_status: str = "verified",
) -> ArchivedFile:
    item = session.scalar(select(OAItem).where(OAItem.oa_item_key == key))
    if item is None:
        item = OAItem(oa_item_key=key, source_channel="done", title="Synthetic item")
        session.add(item)
        session.flush()
    relpath = f"originals/{key}/{filename}"
    source = settings.data_root / relpath
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    file = ArchivedFile(
        oa_item_id=item.id,
        original_name=filename,
        local_relpath=relpath,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        attachment_key=f"att-{filename}",
        file_role="attachment",
        source_container_key="synthetic",
        depth=depth,
        download_status=download_status,
    )
    session.add(file)
    session.commit()
    return file


def _request(file: ArchivedFile, **overrides: object):
    from oa_knowledge.classification.parse_cache import ParseRequest

    values: dict[str, object] = {
        "file_id": file.id,
        "content_sha256": file.sha256,
        "parser_name": "markitdown",
        "parser_version": "test-parser-v1",
        "parse_profile_version": "classification-v1",
        "parse_config_sha256": "a" * 64,
        "metadata_unresolved": True,
    }
    values.update(overrides)
    return ParseRequest(**values)


def _fake_router(calls: list[int]):
    def parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> ParseResult:
        calls.append(1)
        output = output_dir / "staged.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"# Parsed {source.name}\n", encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "test-parser-v1",
            1.0,
            text_length=20,
            profile_version=profile_version,
        )

    return parse


def test_same_versioned_identity_reuses_one_artifact_for_two_files(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing cache lookup to use file ID would parse identical content twice."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        first = _seed_file(
            session, settings, key="oa:one", filename="one.txt", payload=b"same"
        )
        second = _seed_file(
            session, settings, key="oa:two", filename="two.txt", payload=b"same"
        )
    calls: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", _fake_router(calls)
    )
    service = ParseCacheService(session_factory, settings)

    first_ref = service.get_or_parse(_request(first))
    second_ref = service.get_or_parse(_request(second))

    assert calls == [1]
    assert first_ref.artifact_id == second_ref.artifact_id
    assert first_ref.consulted is True
    with session_factory() as session:
        assert len(session.scalars(select(ContentObject)).all()) == 1
        assert len(session.scalars(select(ParseArtifact)).all()) == 1


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("parser_name", "mineru"),
        ("parser_version", "test-parser-v2"),
        ("parse_profile_version", "classification-v2"),
        ("parse_config_sha256", "b" * 64),
    ],
)
def test_versioned_cache_key_creates_a_new_artifact_for_each_identity_change(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    value: str,
) -> None:
    """Dropping any version/key component would silently reuse obsolete parses."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session, settings, key="oa:one", filename="one.txt", payload=b"same"
        )
    calls: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", _fake_router(calls)
    )
    service = ParseCacheService(session_factory, settings)

    original = service.get_or_parse(_request(file))
    changed = service.get_or_parse(_request(file, **{change: value}))

    assert original.artifact_id != changed.artifact_id
    assert calls == [1, 1]


def test_metadata_resolved_request_does_not_open_or_parse_an_attachment(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling a parser for a metadata-resolved package wastes confidential work."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session, settings, key="oa:resolved", filename="body.txt", payload=b"same"
        )
    calls: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", _fake_router(calls)
    )

    ref = ParseCacheService(session_factory, settings).get_or_parse(
        _request(file, metadata_unresolved=False)
    )

    assert ref.consulted is False
    assert ref.artifact_id is None
    assert ref.status == "not_required"
    assert calls == []


def test_depth_limited_required_attachment_is_blocked_and_queued_for_review(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the depth guard could send an incomplete container to content rules."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session,
            settings,
            key="oa:deep",
            filename="nested.txt",
            payload=b"same",
            depth=10,
        )
    calls: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", _fake_router(calls)
    )

    ref = ParseCacheService(session_factory, settings).get_or_parse(
        _request(file, depth_limit_reached=True, container_key="container:ten")
    )

    assert ref.consulted is False
    assert ref.status == "integrity_blocked"
    assert ref.error_code == "depth_limit_reached"
    assert calls == []
    with session_factory() as session:
        review = session.scalar(
            select(ReviewEntry).where(ReviewEntry.kind == "depth_limit_reached")
        )
        assert review is not None
        assert review.file_id == file.id
        assert review.container_key == "container:ten"


def test_missing_cached_product_is_atomically_rebuilt_under_the_same_identity(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleaned derived product must not leave its unique artifact row unusable."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session, settings, key="oa:rebuild", filename="body.txt", payload=b"same"
        )
    calls: list[int] = []
    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", _fake_router(calls)
    )
    service = ParseCacheService(session_factory, settings)

    first = service.get_or_parse(_request(file))
    assert first.output_relpath is not None
    (settings.cache_root / first.output_relpath).unlink()
    rebuilt = service.get_or_parse(_request(file))

    assert rebuilt.artifact_id == first.artifact_id
    assert rebuilt.status == "parsed"
    assert (settings.cache_root / rebuilt.output_relpath).is_file()
    assert calls == [1, 1]


def test_first_same_sha_requests_converge_when_content_object_insert_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A competing first ContentObject insert must not fail an otherwise valid parse."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        future=True,
        connect_args={"timeout": 10},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        first = _seed_file(
            session, settings, key="oa:race-one", filename="one.txt", payload=b"same"
        )
        second = _seed_file(
            session, settings, key="oa:race-two", filename="two.txt", payload=b"same"
        )

    insert_barrier = threading.Barrier(2)

    def align_first_content_inserts(*args: object) -> None:
        statement = str(args[2])
        if "INSERT INTO content_objects" in statement:
            insert_barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", align_first_content_inserts)
    calls: list[int] = []
    calls_lock = threading.Lock()

    def parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> ParseResult:
        with calls_lock:
            calls.append(1)
        output = output_dir / "staged.md"
        output.write_text(f"# {source.name}\n", encoding="utf-8")
        return ParseResult(
            output,
            engine,
            "test-parser-v1",
            1.0,
            profile_version=profile_version,
        )

    monkeypatch.setattr("oa_knowledge.classification.parse_cache.parse_file", parse)
    service = ParseCacheService(factory, settings)
    start = threading.Barrier(2)
    refs: list[object] = []
    errors: list[BaseException] = []

    def run(file: ArchivedFile) -> None:
        try:
            start.wait(timeout=10)
            refs.append(service.get_or_parse(_request(file)))
        except (OSError, RuntimeError, SQLAlchemyError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(file,)) for file in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    event.remove(engine, "before_cursor_execute", align_first_content_inserts)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(refs) == 2
    with factory() as session:
        assert len(session.scalars(select(ContentObject)).all()) == 1


def test_parser_result_with_a_different_engine_or_version_is_never_cached_as_requested(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trusting a stale requested identity would label new parser output incorrectly."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session, settings, key="oa:identity", filename="body.txt", payload=b"same"
        )

    def mismatched_parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> ParseResult:
        output = output_dir / "staged.md"
        output.write_text("# mismatched\n", encoding="utf-8")
        return ParseResult(output, "mineru", "unexpected-v2", 1.0)

    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", mismatched_parse
    )

    result = ParseCacheService(session_factory, settings).get_or_parse(_request(file))

    assert result.status == "parse_failed"
    assert result.error_code == "parser_identity_mismatch"
    with session_factory() as session:
        assert len(session.scalars(select(ParseArtifact)).all()) == 0


def test_parser_result_with_a_different_profile_is_never_cached_as_requested(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignoring the returned profile would make a profile upgrade reuse stale output."""
    from oa_knowledge.classification.parse_cache import ParseCacheService

    settings = _settings(tmp_path)
    with session_factory() as session:
        file = _seed_file(
            session, settings, key="oa:profile", filename="body.txt", payload=b"same"
        )

    def mismatched_parse(
        source: Path,
        settings: Settings,
        *,
        engine: str,
        output_dir: Path,
        profile_version: str,
    ) -> SimpleNamespace:
        output = output_dir / "staged.md"
        output.write_text("# mismatched profile\n", encoding="utf-8")
        return SimpleNamespace(
            output_path=output,
            engine="markitdown",
            engine_version="test-parser-v1",
            profile_version="classification-v2",
            quality_score=1.0,
        )

    monkeypatch.setattr(
        "oa_knowledge.classification.parse_cache.parse_file", mismatched_parse
    )

    result = ParseCacheService(session_factory, settings).get_or_parse(_request(file))

    assert result.status == "parse_failed"
    assert result.error_code == "parser_identity_mismatch"
    with session_factory() as session:
        assert len(session.scalars(select(ParseArtifact)).all()) == 0
