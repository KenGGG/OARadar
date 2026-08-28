from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from oa_knowledge.classified_candidate_build import (
    ClassifiedCandidateBuildService,
    freeze_publishable_snapshot,
    run_attachment_worker,
)
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ClassificationDecision,
    ClassificationRun,
    OAItem,
    OAManifestItem,
)


def _run(session: Session) -> ClassificationRun:
    run = ClassificationRun(
        run_id="synthetic-classification",
        run_kind="full",
        status="completed",
        input_signature="a" * 64,
        manifest_sha256="b" * 64,
        exclusion_policy_sha256="c" * 64,
        rule_version="test",
        schema_version="test",
        prompt_version="test",
        model_name="test",
        private_config_sha256="d" * 64,
    )
    session.add(run)
    session.flush()
    return run


def _decision(
    session: Session,
    run: ClassificationRun,
    key: str,
    *,
    status: str = "classified",
    origin: str | None = "internal",
    integrity: str = "ok",
    category: str | None = "08_行政采购与信息化",
    issuer: str | None = None,
    current: bool = True,
    reason: str = "{}",
    version: int = 1,
) -> ClassificationDecision:
    if session.scalar(select(OAItem).where(OAItem.oa_item_key == key)) is None:
        session.add(OAItem(oa_item_key=key, source_channel="done", title=key))
    row = ClassificationDecision(
        classification_run_id=run.id,
        oa_item_key=key,
        version=version,
        is_current=current,
        decision_input_sha256="e" * 64,
        decision_source="metadata_rule",
        classification_status=status,
        content_integrity_status=integrity,
        content_origin=origin,
        initiator_type="internal",
        canonical_issuer=issuer,
        business_category=category,
        normalized_title=key,
        classification_confidence=1.0,
        classification_reason_json=reason,
        rule_version="test",
        private_config_sha256="d" * 64,
    )
    session.add(row)
    session.flush()
    return row


def test_freeze_publishable_snapshot_includes_only_current_publishable_decisions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            run = _run(session)
            internal = _decision(session, run, "done:internal")
            external = _decision(
                session,
                run,
                "done:external",
                origin="external",
                category=None,
                issuer="Synthetic Authority",
            )
            _decision(session, run, "done:review", status="needs_review", origin=None, category=None)
            _decision(session, run, "done:blocked", integrity="sha256_mismatch")
            _decision(session, run, "done:no-issuer", origin="external", category=None, issuer=None, status="needs_review")
            _decision(session, run, "done:conflict", reason='{"conflict_codes": ["issuer_conflict"]}')
            prior = _decision(session, run, "done:old", current=False)
            current = _decision(session, run, "done:old", current=True, version=2)
            ids = (internal.id, external.id, prior.id, current.id)
            session.commit()

            snapshot = freeze_publishable_snapshot(session)

        assert {(row.oa_item_key, row.decision_id) for row in snapshot} == {
            ("done:internal", ids[0]),
            ("done:external", ids[1]),
            ("done:old", ids[3]),
        }
        assert ids[2] not in {row.decision_id for row in snapshot}
    finally:
        engine.dispose()


def test_candidate_build_renders_confirmed_no_attachment_without_creating_decisions(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings.model_validate(
        {
            "app": {"data_root": str(tmp_path / "data")},
            "runtime": {"state_root": str(tmp_path / "state"), "cache_root": str(tmp_path / "cache")},
        }
    )
    try:
        with factory.begin() as session:
            run = _run(session)
            _decision(
                session,
                run,
                "done:no-attachment",
                integrity="no_attachment_confirmed",
            )
            session.add(
                OAManifestItem(
                    oa_item_key="done:no-attachment",
                    title="Synthetic confirmed empty",
                    list_page=1,
                    processing_status="no_attachment",
                    no_attachment_confirmed=True,
                )
            )
        with factory() as session:
            before = session.scalar(select(func.count()).select_from(ClassificationDecision))

        service = ClassifiedCandidateBuildService(settings, factory)
        result = service.build("synthetic-candidate")

        assert result.target_total == 1
        assert result.package_success == 1
        assert result.package_partial == result.package_failed == 0
        index = result.output_root / "packages" / result.package_relpaths["done:no-attachment"] / "_index.md"
        assert index.is_file()
        assert "无附件（OA 清单已确认）" in index.read_text(encoding="utf-8")
        with factory() as session:
            after = session.scalar(select(func.count()).select_from(ClassificationDecision))
        assert after == before
        qa = service.validate("synthetic-candidate")
        assert qa.passed
        assert qa.index_count == 1
    finally:
        engine.dispose()


def test_candidate_build_keeps_package_when_an_attachment_is_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings.model_validate(
        {
            "app": {"data_root": str(tmp_path / "data")},
            "runtime": {"state_root": str(tmp_path / "state"), "cache_root": str(tmp_path / "cache")},
        }
    )
    payload = b"synthetic video payload"
    source = settings.data_root / "originals" / "done" / "unsupported" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    monkeypatch.setattr(
        "oa_knowledge.classified_candidate_build.run_attachment_worker",
        lambda *_args, **_kwargs: ("skipped", None, ("metadata_only", "mp4")),
    )
    try:
        with factory.begin() as session:
            run = _run(session)
            _decision(session, run, "done:unsupported")
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == "done:unsupported"))
            assert item is not None
            session.add(
                OAManifestItem(
                    oa_item_key="done:unsupported",
                    title="Synthetic unsupported attachment",
                    list_page=1,
                    processing_status="downloaded",
                )
            )
            session.add(
                ArchivedFile(
                    oa_item_id=item.id,
                    original_name="clip.mp4",
                    local_relpath="originals/done/unsupported/clip.mp4",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    attachment_key="synthetic-video",
                    file_role="direct_attachment",
                    source_container_key="root",
                    depth=1,
                    download_status="verified",
                )
            )

        result = ClassifiedCandidateBuildService(settings, factory).build("synthetic-partial")

        assert result.target_total == 1
        assert result.package_partial == 1
        index = result.output_root / "packages" / result.package_relpaths["done:unsupported"] / "_index.md"
        assert "metadata_only" in index.read_text(encoding="utf-8")
        build_manifest = json.loads(
            (result.output_root / "build_manifest.json").read_text(encoding="utf-8")
        )
        exception = build_manifest["items"][0]["exceptions"][0]
        assert exception["original_name"] == "clip.mp4"
        assert exception["sha256"] == hashlib.sha256(payload).hexdigest()
        assert exception["actual_file_type"] == "mp4"
        assert (result.output_root / "exceptions.csv").is_file()
    finally:
        engine.dispose()


def test_candidate_build_resumes_from_the_frozen_work_snapshot(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = Settings.model_validate(
        {
            "app": {"data_root": str(tmp_path / "data")},
            "runtime": {"state_root": str(tmp_path / "state"), "cache_root": str(tmp_path / "cache")},
        }
    )
    try:
        with factory.begin() as session:
            run = _run(session)
            for key in ("done:first", "done:second"):
                _decision(session, run, key, integrity="no_attachment_confirmed")
                session.add(
                    OAManifestItem(
                        oa_item_key=key,
                        title=key,
                        list_page=1,
                        processing_status="no_attachment",
                        no_attachment_confirmed=True,
                    )
                )
        service = ClassifiedCandidateBuildService(settings, factory)

        service.start("synthetic-resume")
        first = service.process("synthetic-resume", limit=1)
        assert first.package_success == 1
        assert first.queued == 1

        resumed = ClassifiedCandidateBuildService(settings, factory)
        second = resumed.process("synthetic-resume", limit=1)
        assert second.package_success == 2
        assert second.queued == 0
        result = resumed.finalize("synthetic-resume")

        assert result.output_root.is_dir()
        assert result.target_total == result.package_success == 2
    finally:
        engine.dispose()


def test_attachment_worker_timeout_becomes_a_single_terminal_attachment_failure(
    monkeypatch,
) -> None:
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["worker"], 120)

    monkeypatch.setattr("oa_knowledge.classified_candidate_build.subprocess.run", _timeout)

    outcome, filename, problem = run_attachment_worker(["worker"], timeout_seconds=120)

    assert outcome == "failed"
    assert filename is None
    assert problem == ("attachment_worker_timeout", "120 seconds")
