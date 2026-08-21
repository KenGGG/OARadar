"""Synthetic contracts for read-only rebuilt-library acceptance validation."""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    now = datetime.now(timezone.utc)
    run = PipelineRun(
        run_key="synthetic-validation-run",
        pipeline_type="data_rebuild",
        status="completed",
        total_tasks=2,
        completed_tasks=2,
        failed_tasks=0,
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
    )
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
    _write_acceptance_evidence(session, settings, run.id)
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


def _frontmatter(item: OAItem) -> str:
    classification_key = "internal_category" if item.source_type == "internal" else "external_issuer"
    classification_value = item.internal_category if item.source_type == "internal" else item.external_issuer
    return (
        "---\n"
        f"title: {item.title}\n"
        f"oa_item_id: {item.oa_item_key}\n"
        f"document_number: {item.document_number or ''}\n"
        f"effective_date: {item.document_date.isoformat()}\n"
        f"source_type: {item.source_type}\n"
        f"{classification_key}: {classification_value}\n"
        "---\n\n# synthetic\n"
    )


def _evidence_fingerprint(session: Session, settings: Settings, run_id: int) -> str:
    """Independently derive the redacted evidence binding used by this fixture."""
    records: list[dict[str, object]] = []
    rows = session.scalars(select(RebuildOutput).where(
        RebuildOutput.run_id == run_id,
        RebuildOutput.status == "success",
    )).all()
    for output in rows:
        target = resolve_rebuild_path(settings, output.target_relpath)
        if output.kind == "parse":
            artifact_sha256 = _tree_sha256(target)
        elif output.kind in {"body_markdown", "attachment_markdown"}:
            assets = target.parent / "assets" / str(output.source_file_id)
            artifact_sha256 = _publication_sha(target, assets if assets.is_dir() else None)
        else:
            artifact_sha256 = _digest(target.read_bytes())
        records.append({
            "artifact_sha256": artifact_sha256,
            "kind": output.kind,
            "ledger_sha256": output.sha256,
            "oa_item_id": output.oa_item_id,
            "source_file_id": output.source_file_id,
            "target_relpath": output.target_relpath,
        })
    payload = {
        "run_id": run_id,
        "schema_version": 1,
        "outputs": sorted(
            records,
            key=lambda row: (
                str(row["kind"]), int(row["oa_item_id"]),
                -1 if row["source_file_id"] is None else int(row["source_file_id"]),
                str(row["target_relpath"]),
            ),
        ),
    }
    return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _write_acceptance_evidence(
    session: Session, settings: Settings, ledger_run_id: int, **changes: object,
) -> None:
    payload = {
        "schema_version": 1,
        "run_id": ledger_run_id,
        "fingerprint": _evidence_fingerprint(session, settings, ledger_run_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "producer": "oaradar.rebuild.acceptance.v1",
        "webui_filter_contract": True,
        "internal_sample_count": 100,
        "external_sample_count": 100,
        "automated_tests_passed": True,
        "frontend_check_passed": True,
        "build_passed": True,
        "synthetic_smoke_passed": True,
    } | changes
    target = settings.rebuild.target_root / "state" / "acceptance-evidence.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _seed_publications(session: Session, settings: Settings, run_id: int, item: OAItem, source: ArchivedFile, *, body: bool) -> None:
    item_relpath = markdown_item_relpath(item)
    item_dir = resolve_rebuild_path(settings, item_relpath); item_dir.mkdir(parents=True, exist_ok=True)
    if body:
        filename = body_markdown_filename(item); assert filename is not None
        kind = "body_markdown"
    else:
        filename = _attachment_markdown_filename(source); kind = "attachment_markdown"
    markdown_relpath = (item_relpath / filename).as_posix()
    markdown = resolve_rebuild_path(settings, markdown_relpath); markdown.write_text(_frontmatter(item), encoding="utf-8")
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=source.id, kind=kind, target_relpath=markdown_relpath, sha256=_publication_sha(markdown, None), status="success"))
    original_relpath = archive_file_relpath(item, source).as_posix()
    index_relpath = (item_relpath / "_index.md").as_posix()
    index = resolve_rebuild_path(settings, index_relpath)
    relative_original = posixpath.relpath(original_relpath, item_relpath.as_posix())
    index.write_text(
        _frontmatter(item) + f"[original]({relative_original})\n[markdown]({filename})\n",
        encoding="utf-8",
    )
    session.add(RebuildOutput(run_id=run_id, oa_item_id=item.id, source_file_id=None, kind="item_index", target_relpath=index_relpath, sha256=_digest(index.read_bytes()), status="success"))


def _check(checks, code: str):
    return next(check for check in checks if check.code == code)


def test_valid_synthetic_rebuild_passes_all_acceptance_checks(rebuild_fixture: RebuildFixture) -> None:
    checks = validate_rebuild(rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id)
    assert validation_passed(checks), " ".join(f"{check.code}={check.expected}/{check.actual}" for check in checks)
    assert len(checks) == 15
    assert all(check.expected is None or check.expected >= 0 for check in checks)


@pytest.mark.parametrize("status", ("pending", "failed"))
def test_nonterminal_or_failed_current_run_cannot_pass_artifact_gates(
    rebuild_fixture: RebuildFixture, status: str,
) -> None:
    run = rebuild_fixture.session.get(PipelineRun, rebuild_fixture.run_id)
    assert run is not None
    run.status = status
    run.failed_tasks = 1 if status == "failed" else 0
    run.finished_at = datetime.now(timezone.utc) if status == "failed" else None
    rebuild_fixture.session.commit()

    checks = validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    )

    assert not validation_passed(checks)
    assert not _check(checks, "ORIGINALS_COMPLETE").ok


def test_empty_completed_current_run_cannot_pass_artifact_gates(
    rebuild_fixture: RebuildFixture,
) -> None:
    rebuild_fixture.session.query(RebuildOutput).filter_by(
        run_id=rebuild_fixture.run_id,
    ).delete(synchronize_session=False)
    rebuild_fixture.session.commit()
    _write_acceptance_evidence(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    )

    checks = validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    )

    assert not validation_passed(checks)
    assert not _check(checks, "ORIGINALS_COMPLETE").ok


@pytest.mark.parametrize("state", ("pending", "failed"))
def test_non_success_current_original_output_or_source_fails_validation(
    rebuild_fixture: RebuildFixture, state: str,
) -> None:
    original = next(rebuild_fixture.session.scalars(select(RebuildOutput).where(
        RebuildOutput.run_id == rebuild_fixture.run_id,
        RebuildOutput.kind == "original",
    )))
    source = rebuild_fixture.session.get(ArchivedFile, original.source_file_id)
    assert source is not None
    if state == "pending":
        original.status = "pending"
    else:
        source.download_status = "failed"
    rebuild_fixture.session.commit()

    checks = validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    )

    assert not _check(checks, "ORIGINALS_COMPLETE").ok
    assert not _check(checks, "ORIGINAL_HASHES_MATCH").ok


@pytest.mark.parametrize("damage", (
    "missing", "wrong_schema", "wrong_run", "stale", "before_finished", "wrong_producer",
    "wrong_fingerprint",
))
def test_run_specific_acceptance_evidence_rejects_missing_stale_or_fabricated_payloads(
    rebuild_fixture: RebuildFixture, damage: str,
) -> None:
    target = rebuild_fixture.settings.rebuild.target_root / "state" / "acceptance-evidence.json"
    if damage == "missing":
        target.unlink()
    elif damage == "wrong_schema":
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            schema_version=999,
        )
    elif damage == "wrong_run":
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            run_id=rebuild_fixture.run_id + 1,
        )
    elif damage == "stale":
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            generated_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        )
    elif damage == "before_finished":
        run = rebuild_fixture.session.get(PipelineRun, rebuild_fixture.run_id)
        assert run is not None and run.finished_at is not None
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            generated_at=(run.finished_at - timedelta(seconds=1)).isoformat(),
        )
    elif damage == "wrong_producer":
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            producer="other-process",
        )
    else:
        _write_acceptance_evidence(
            rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
            fingerprint="0" * 64,
        )

    checks = validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    )

    assert not _check(checks, "WEBUI_FILTER_CONTRACT").ok
    assert not _check(checks, "SAMPLE_EVIDENCE_COMPLETE").ok
    assert not _check(checks, "AUTOMATED_GATES_CONFIRMED").ok


@pytest.mark.parametrize(("code", "damage"), [
    ("ORIGINALS_COMPLETE", "nondeterministic_original_path"),
    ("ORIGINAL_HASHES_MATCH", "tamper_original"),
    ("NO_UNKNOWN_ORIGINALS", "unknown_archive"),
    ("CONFIRMED_OUTPUTS_ONLY", "unconfirmed_output"),
    ("INDEX_EXACTLY_ONE", "remove_index_output"),
    ("NUMBERED_BODY_COMPLETE", "remove_body_output"),
    ("UNNUMBERED_BODY_ABSENT", "add_unnumbered_body"),
    ("SUPPORTED_ATTACHMENTS_ACCOUNTED", "supported_retry_without_index"),
    ("UNSUPPORTED_ATTACHMENTS_INDEXED", "unsupported_without_index"),
    ("ALL_LINKS_RESOLVE", "broken_link"),
    ("WEBUI_FILTER_CONTRACT", "webui_contract_missing"),
    ("OBSIDIAN_SEARCH_FIELDS", "remove_frontmatter"),
    ("SAMPLE_EVIDENCE_COMPLETE", "sample_evidence_short"),
    ("REBUILD_ROOT_LAYOUT_CLEAN", "unexpected_root"),
    ("AUTOMATED_GATES_CONFIRMED", "automation_gate_missing"),
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


def test_later_verified_done_file_is_outside_the_current_run_baseline(rebuild_fixture: RebuildFixture) -> None:
    """A later Done file must not enlarge this run's original acceptance set."""
    later = ArchivedFile(
        oa_item_id=rebuild_fixture.numbered.id, original_name="later.txt", attachment_key="later",
        file_role="direct_attachment", source_container_key="root", local_relpath="archive/raw/oa/done/later.txt",
        size_bytes=5, sha256=_digest(b"later"), download_status="verified",
    )
    rebuild_fixture.session.add(later); rebuild_fixture.session.commit()

    assert validation_passed(validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    ))


@pytest.mark.parametrize("damage", ("wrong_original_owner", "nondeterministic_original_path"))
def test_current_run_original_requires_source_owner_and_deterministic_path(
    rebuild_fixture: RebuildFixture, damage: str,
) -> None:
    _damage(rebuild_fixture, damage)

    assert not _check(validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    ), "ORIGINALS_COMPLETE").ok


def test_supported_attachment_retryable_failure_is_accepted_when_indexed(
    rebuild_fixture: RebuildFixture,
) -> None:
    _damage(rebuild_fixture, "supported_retry_indexed")

    assert _check(validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    ), "SUPPORTED_ATTACHMENTS_ACCOUNTED").ok


def test_links_to_parse_are_not_permitted(rebuild_fixture: RebuildFixture) -> None:
    index = next(rebuild_fixture.session.scalars(select(RebuildOutput).where(
        RebuildOutput.run_id == rebuild_fixture.run_id, RebuildOutput.kind == "item_index",
    )))
    path = resolve_rebuild_path(rebuild_fixture.settings, index.target_relpath)
    path.write_text(_frontmatter(rebuild_fixture.numbered) + "[parse](../../../../../../parse/1/1/synthetic/result.md)\n", encoding="utf-8")
    index.sha256 = _digest(path.read_bytes()); rebuild_fixture.session.commit()

    assert not _check(validate_rebuild(
        rebuild_fixture.session, rebuild_fixture.settings, rebuild_fixture.run_id,
    ), "ALL_LINKS_RESOLVE").ok


def _damage(fixture: RebuildFixture, kind: str) -> None:
    session, settings = fixture.session, fixture.settings
    rows = list(session.scalars(select(RebuildOutput).where(RebuildOutput.run_id == fixture.run_id)))
    by_kind = {row.kind: row for row in rows}
    if kind == "tamper_original": resolve_rebuild_path(settings, by_kind["original"].target_relpath).write_bytes(b"tampered")
    elif kind == "unknown_archive":
        path = resolve_rebuild_path(settings, "archive/oa/done/unknown.bin"); path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"unknown")
    elif kind == "unconfirmed_output":
        fixture.unnumbered.classification_state = "needs_review"
    elif kind == "remove_index_output": session.delete(by_kind["item_index"])
    elif kind == "remove_body_output": session.delete(next(row for row in rows if row.kind == "body_markdown"))
    elif kind == "add_unnumbered_body":
        path = markdown_item_relpath(fixture.unnumbered) / "unexpected-正文.md"; target = resolve_rebuild_path(settings, path); target.write_text(_frontmatter(fixture.unnumbered), encoding="utf-8")
        session.add(RebuildOutput(run_id=fixture.run_id, oa_item_id=fixture.unnumbered.id, source_file_id=fixture.files["attachment"].id, kind="body_markdown", target_relpath=path.as_posix(), sha256=_publication_sha(target, None), status="success"))
    elif kind == "supported_retry_without_index":
        attachment = next(row for row in rows if row.kind == "attachment_markdown")
        attachment.status, attachment.error_code = "failed", "RETRYABLE_CONVERSION_FAILED"
    elif kind == "unsupported_without_index":
        source = fixture.files["attachment"]
        source.original_name = "unsupported.exe"
        original = next(row for row in rows if row.kind == "original" and row.source_file_id == source.id)
        old_target = resolve_rebuild_path(settings, original.target_relpath)
        original.target_relpath = archive_file_relpath(fixture.unnumbered, source).as_posix()
        target = resolve_rebuild_path(settings, original.target_relpath)
        target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(old_target.read_bytes()); old_target.unlink()
        parse = next(row for row in rows if row.kind == "parse" and row.source_file_id == source.id); parse.status = "failed"; parse.error_code = "UNSUPPORTED_FORMAT"; parse.sha256 = None
    elif kind == "webui_contract_missing": _write_acceptance_evidence(session, settings, fixture.run_id, webui_filter_contract=False)
    elif kind == "remove_frontmatter":
        index = next(row for row in rows if row.kind == "item_index")
        target = resolve_rebuild_path(settings, index.target_relpath); target.write_text("# missing\n", encoding="utf-8")
        index.sha256 = _digest(target.read_bytes())
    elif kind == "sample_evidence_short": _write_acceptance_evidence(session, settings, fixture.run_id, internal_sample_count=99)
    elif kind == "broken_link": resolve_rebuild_path(settings, by_kind["item_index"].target_relpath).write_text("[bad](missing.md)\n", encoding="utf-8")
    elif kind == "unexpected_root": (settings.rebuild.target_root / "logs").mkdir(parents=True)
    elif kind == "automation_gate_missing": _write_acceptance_evidence(session, settings, fixture.run_id, build_passed=False)
    elif kind == "wrong_original_owner":
        next(row for row in rows if row.kind == "original" and row.source_file_id == fixture.files["body"].id).oa_item_id = fixture.unnumbered.id
    elif kind == "nondeterministic_original_path": by_kind["original"].target_relpath = "archive/oa/done/not-deterministic.bin"
    elif kind == "supported_retry_indexed":
        attachment = next(row for row in rows if row.kind == "attachment_markdown")
        attachment.status, attachment.error_code = "failed", "RETRYABLE_CONVERSION_FAILED"
        index = next(row for row in rows if row.kind == "item_index" and row.oa_item_id == fixture.unnumbered.id)
        target = resolve_rebuild_path(settings, index.target_relpath)
        target.write_text(_frontmatter(fixture.unnumbered) + "转换失败，等待重试\n", encoding="utf-8")
        index.sha256 = _digest(target.read_bytes())
    session.commit()
