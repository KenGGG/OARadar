"""Synthetic contracts for read-only rebuilt-library acceptance validation."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import ArchivedFile, OAItem, PipelineRun, RebuildOutput
from oa_knowledge.rebuild.body_source import body_markdown_filename
from oa_knowledge.rebuild.markdown import (
    _attachment_markdown_filename,
    _publication_sha,
)
from oa_knowledge.rebuild.parser import _tree_sha256
from oa_knowledge.rebuild.paths import (
    archive_file_relpath,
    markdown_item_relpath,
    resolve_rebuild_path,
)
from oa_knowledge.rebuild.validation import validate_rebuild, validation_passed


@dataclass
class RebuildFixture:
    session: Session
    settings: Settings
    run_id: int
    numbered: OAItem
    unnumbered: OAItem
    files: dict[str, ArchivedFile]


@pytest.fixture
def rebuild_fixture(tmp_path: Path) -> RebuildFixture:
    settings = Settings(
        app={"data_root": tmp_path / "live-data"},
        rebuild={"target_root": tmp_path / "clean-rebuild"},
    )
    settings.data_root.mkdir(parents=True)
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    session = Session(engine)
    run = PipelineRun(run_key="synthetic-validation-run", pipeline_type="data_rebuild")
    numbered = OAItem(
        oa_item_key="done:synthetic-numbered", source_channel="done", title="Synthetic numbered",
        document_number="SYN-1", document_date=date(2026, 8, 20),
        classification_state="confirmed", source_type="internal", internal_category="风险管理",
    )
    unnumbered = OAItem(
        oa_item_key="done:synthetic-unnumbered", source_channel="done", title="Synthetic unnumbered",
        document_date=date(2026, 8, 21), classification_state="confirmed", source_type="external",
        external_issuer="Synthetic issuer",
    )
    session.add_all((run, numbered, unnumbered)); session.flush()
    body = _seed_file(session, settings, run.id, numbered, "body.txt", "official_body")
    attachment = _seed_file(session, settings, run.id, unnumbered, "attachment.txt", "direct_attachment")
    session.commit()
    _seed_publications(session, settings, run.id, numbered, body, body=True)
    _seed_publications(session, settings, run.id, unnumbered, attachment, body=False)
    session.commit()
    value = RebuildFixture(session, settings, run.id, numbered, unnumbered, {"body": body, "attachment": attachment})
    yield value
    session.close(); engine.dispose()


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _seed_file(session: Session, settings: Settings, run_id: int, item: OAItem, name: str, role: str) -> ArchivedFile:
    content = f"synthetic original {name}".encode()
    source = ArchivedFile(
        oa_item_id=item.id, original_name=name, attachment_key=f"key:{name}", file_role=role,
        source_container_key="root", local_relpath=f"archive/raw/oa/done/{name}", size_bytes=len(content),
        sha256=_digest(content), download_status="verified",
    )
    session.add(source); session.flush()
    relpath = archive_file_relpath(item, source).as_posix()
    target = resolve_rebuild_path(settings, relpath); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=source.id, kind="original", target_relpath=relpath, sha256=source.sha256, status="success"))
    parse_relpath = f"parse/{run_id}/{source.id}/synthetic"
    parse_root = resolve_rebuild_path(settings, parse_relpath); parse_root.mkdir(parents=True)
    (parse_root / "result.md").write_text("# parsed synthetic\n", encoding="utf-8")
    (parse_root / ".oaradar-parse.json").write_text(json.dumps({"engine": "synthetic", "engine_version": "1", "source_file_id": source.id, "source_sha256": source.sha256}), encoding="utf-8")
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=source.id, kind="parse", target_relpath=parse_relpath, sha256=_tree_sha256(parse_root), status="success"))
    return source


def _frontmatter() -> str:
    return "---\noa_item_id: synthetic\nsource_type: internal\n---\n\n# synthetic\n"


def _seed_publications(session: Session, settings: Settings, run_id: int, item: OAItem, source: ArchivedFile, *, body: bool) -> None:
    item_relpath = markdown_item_relpath(item)
    item_dir = resolve_rebuild_path(settings, item_relpath); item_dir.mkdir(parents=True, exist_ok=True)
    if body:
        filename = body_markdown_filename(item); assert filename is not None
        kind = "body_markdown"
    else:
        filename = _attachment_markdown_filename(source); kind = "attachment_markdown"
    markdown_relpath = (item_relpath / filename).as_posix()
    markdown = resolve_rebuild_path(settings, markdown_relpath); markdown.write_text(_frontmatter(), encoding="utf-8")
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=source.id, kind=kind, target_relpath=markdown_relpath, sha256=_publication_sha(markdown, None), status="success"))
    original_relpath = archive_file_relpath(item, source).as_posix()
    index_relpath = (item_relpath / "_index.md").as_posix()
    index = resolve_rebuild_path(settings, index_relpath)
    relative_original = posixpath.relpath(original_relpath, item_relpath.as_posix())
    index.write_text(f"# index\n\n[original]({relative_original})\n[markdown]({filename})\n", encoding="utf-8")
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=None, kind="item_index", target_relpath=index_relpath, sha256=_digest(index.read_bytes()), status="success"))


def _check(checks, code: str):
    return next(check for check in checks if check.code == code)


def test_valid_synthetic_rebuild_passes_all_acceptance_checks(rebuild_fixture: RebuildFixture) -> None:
    checks = validate_rebuild(rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id)
    assert validation_passed(checks), " ".join(f"{check.code}={check.expected}/{check.actual}" for check in checks)
    assert len(checks) == 15
    assert all(check.expected is None or check.expected >= 0 for check in checks)


@pytest.mark.parametrize(("code", "damage"), [
    ("ORIGINALS_COMPLETE", "remove_original_output"),
    ("ORIGINAL_HASHES_MATCH", "tamper_original"),
    ("NO_UNKNOWN_ORIGINALS", "unknown_archive"),
    ("CONFIRMED_OUTPUTS_ONLY", "unconfirmed_output"),
    ("INDEX_EXACTLY_ONE", "remove_index_output"),
    ("NUMBERED_BODY_COMPLETE", "remove_body_output"),
    ("UNNUMBERED_BODY_ABSENT", "add_unnumbered_body"),
    ("PARSE_PRODUCTS_VALID", "tamper_parse"),
    ("SUPPORTED_ATTACHMENTS_ACCOUNTED", "remove_attachment_output"),
    ("UNSUPPORTED_ATTACHMENTS_INDEXED", "unsupported_without_index"),
    ("MARKDOWN_FRONTMATTER_VALID", "remove_frontmatter"),
    ("ALL_LINKS_RESOLVE", "broken_link"),
    ("MARKDOWN_OUTPUT_PATHS_VALID", "wrong_markdown_path"),
    ("REBUILD_ROOT_LAYOUT_CLEAN", "unexpected_root"),
    ("CURRENT_RUN_OUTPUTS_FINAL", "pending_output"),
])
def test_each_acceptance_invariant_reports_its_stable_code(rebuild_fixture: RebuildFixture, code: str, damage: str) -> None:
    _damage(rebuild_fixture, damage)
    assert not _check(validate_rebuild(rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id), code).ok


def test_validation_is_read_only_and_redacted(rebuild_fixture: RebuildFixture) -> None:
    before = [(row.id, row.status, row.error_code) for row in rebuild_fixture.session.scalars(select(RebuildOutput).order_by(RebuildOutput.id))]
    checks = validate_rebuild(rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id)
    after = [(row.id, row.status, row.error_code) for row in rebuild_fixture.session.scalars(select(RebuildOutput).order_by(RebuildOutput.id))]
    assert before == after
    assert all(set(check.__dict__) == {"code", "ok", "expected", "actual"} for check in checks)
    assert all("Synthetic" not in repr(check) and "/" not in check.code for check in checks)


def _damage(fixture: RebuildFixture, kind: str) -> None:
    session, settings = fixture.session, fixture.settings
    rows = list(session.scalars(select(RebuildOutput).where(RebuildOutput.run_id == fixture.run_id)))
    by_kind = {row.kind: row for row in rows}
    if kind == "remove_original_output": session.delete(by_kind["original"])
    elif kind == "tamper_original": resolve_rebuild_path(settings, by_kind["original"].target_relpath).write_bytes(b"tampered")
    elif kind == "unknown_archive":
        path = resolve_rebuild_path(settings, "archive/oa/done/unknown.bin"); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"unknown")
    elif kind == "unconfirmed_output":
        fixture.unnumbered.classification_state = "needs_review"
    elif kind == "remove_index_output": session.delete(by_kind["item_index"])
    elif kind == "remove_body_output": session.delete(next(row for row in rows if row.kind == "body_markdown"))
    elif kind == "add_unnumbered_body":
        path = markdown_item_relpath(fixture.unnumbered) / "unexpected-正文.md"; target = resolve_rebuild_path(settings, path); target.write_text(_frontmatter(), encoding="utf-8")
        session.add(RebuildOutput(run_id=fixture.run_id, oa_item_id=fixture.unnumbered.id, source_file_id=fixture.files["attachment"].id, kind="body_markdown", target_relpath=path.as_posix(), sha256=_publication_sha(target, None), status="success"))
    elif kind == "tamper_parse": resolve_rebuild_path(settings, by_kind["parse"].target_relpath).joinpath("result.md").write_text("tampered", encoding="utf-8")
    elif kind == "remove_attachment_output": session.delete(next(row for row in rows if row.kind == "attachment_markdown"))
    elif kind == "unsupported_without_index":
        fixture.files["attachment"].original_name = "unsupported.exe"
        parse = next(row for row in rows if row.kind == "parse" and row.source_file_id == fixture.files["attachment"].id); parse.status = "failed"; parse.error_code = "UNSUPPORTED_FORMAT"; parse.sha256 = None
    elif kind == "remove_frontmatter": resolve_rebuild_path(settings, next(row for row in rows if row.kind == "body_markdown").target_relpath).write_text("# missing\n", encoding="utf-8")
    elif kind == "broken_link": resolve_rebuild_path(settings, by_kind["item_index"].target_relpath).write_text("[bad](missing.md)\n", encoding="utf-8")
    elif kind == "wrong_markdown_path": next(row for row in rows if row.kind == "attachment_markdown").target_relpath = "markdown/wrong.md"
    elif kind == "unexpected_root": (settings.rebuild.target_root / "logs").mkdir(parents=True)
    elif kind == "pending_output": by_kind["item_index"].status = "pending"
    session.commit()
