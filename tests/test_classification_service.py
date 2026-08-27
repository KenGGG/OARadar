from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.classification.service import (
    ClassificationService,
    CreateClassificationRun,
    ManualDecisionCommand,
)
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ClassificationDecision,
    ClassificationRun,
    ClassificationRunItem,
    OAItem,
    OAManifestItem,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def config() -> PrivateClassificationConfig:
    return PrivateClassificationConfig.model_validate(
        {
            "initiators": {
                "synth.internal": {"role": "internal", "aliases": []},
                "synth.external": {"role": "external", "aliases": []},
                "synth.mixed": {"role": "mixed", "aliases": []},
                "synth.system": {"role": "system", "aliases": []},
                "synth.unknown": {"role": "unknown", "aliases": []},
            },
            "document_number_issuers": [
                {
                    "pattern": r"^SYN-AUTH-[0-9]+$",
                    "canonical_issuer": "Synthetic Authority",
                    "document_type": "notice",
                }
            ],
            "issuer_aliases": {
                "Synthetic Authority": "Synthetic Authority",
            },
            "title_templates": [
                {
                    "pattern": r"^Synthetic internal approval:",
                    "content_origin": "internal",
                    "flow_type": "approval",
                    "business_category": "08_行政采购与信息化",
                }
            ],
        }
    )


def _seed(
    factory: sessionmaker[Session],
    *,
    include_unknown_attachment: bool = True,
) -> None:
    manifests = [
        OAManifestItem(
            oa_item_key="done:excluded",
            title="Synthetic excluded leave form",
            sender="synth.internal",
            list_page=1,
            list_ordinal=1,
            processing_status="skipped",
            matched_exclusion_keyword="Synthetic leave",
        ),
        OAManifestItem(
            oa_item_key="done:internal",
            title="Synthetic internal approval: equipment",
            sender="synth.internal",
            list_page=1,
            list_ordinal=2,
            processing_status="downloaded",
        ),
        OAManifestItem(
            oa_item_key="done:external",
            title="Synthetic incoming notice",
            sender="synth.internal",
            list_page=1,
            list_ordinal=3,
            processing_status="downloaded",
        ),
        OAManifestItem(
            oa_item_key="done:unknown",
            title="Synthetic neutral item",
            sender="synth.unknown",
            list_page=1,
            list_ordinal=4,
            processing_status="downloaded",
        ),
    ]
    items = [
        OAItem(
            oa_item_key="done:internal",
            source_channel="done",
            title=manifests[1].title,
            sender=manifests[1].sender,
            pipeline_status="files_verified",
        ),
        OAItem(
            oa_item_key="done:external",
            source_channel="done",
            title=manifests[2].title,
            sender=manifests[2].sender,
            document_number="SYN-AUTH-7",
            pipeline_status="files_verified",
        ),
        OAItem(
            oa_item_key="done:unknown",
            source_channel="done",
            title=manifests[3].title,
            sender=manifests[3].sender,
            pipeline_status="files_verified",
        ),
    ]
    with factory.begin() as session:
        session.add_all([*manifests, *items])
        session.flush()
        if include_unknown_attachment:
            unknown = next(row for row in items if row.oa_item_key == "done:unknown")
            session.add(
                ArchivedFile(
                    oa_item_id=unknown.id,
                    original_name="synthetic.pdf",
                    local_relpath="originals/done/unknown/synthetic.pdf",
                    size_bytes=10,
                    sha256=HASH_A,
                    attachment_key="synthetic-attachment",
                    file_role="direct_attachment",
                    source_container_key="root",
                    depth=1,
                    download_status="verified",
                )
            )


def _request(run_id: str) -> CreateClassificationRun:
    return CreateClassificationRun(
        run_id=run_id,
        run_kind="full",
        manifest_sha256=HASH_A,
        exclusion_policy_sha256=HASH_B,
        rule_version="rules-v1",
        schema_version="classification-v1",
        prompt_version="qwen-v1",
        model_name="qwen-local-synthetic",
        private_config_sha256=HASH_A,
    )


def _current(session: Session, key: str) -> ClassificationDecision:
    return session.scalar(
        select(ClassificationDecision).where(
            ClassificationDecision.oa_item_key == key,
            ClassificationDecision.is_current.is_(True),
        )
    )


def test_run_freezes_manifest_membership_and_produces_one_terminal_decision(
    factory: sessionmaker[Session], config: PrivateClassificationConfig
) -> None:
    _seed(factory)
    service = ClassificationService(factory, config)

    ref = service.create_run(_request("synthetic-run-1"))
    with factory.begin() as session:
        session.add(
            OAManifestItem(
                oa_item_key="done:late",
                title="Synthetic late row",
                sender="synth.internal",
                list_page=2,
                list_ordinal=1,
                processing_status="downloaded",
            )
        )

    progress = service.process_next(ref.run_id, limit=100)

    assert (ref.total_count, ref.target_count, ref.excluded_count) == (4, 3, 1)
    assert progress.decided == 4
    with factory() as session:
        frozen_keys = set(
            session.scalars(
                select(ClassificationRunItem.oa_item_key).where(
                    ClassificationRunItem.classification_run_id == ref.database_id
                )
            )
        )
        assert frozen_keys == {
            "done:excluded",
            "done:internal",
            "done:external",
            "done:unknown",
        }
        decisions = list(
            session.scalars(
                select(ClassificationDecision).where(
                    ClassificationDecision.is_current.is_(True)
                )
            )
        )
        assert len(decisions) == 4
        assert {row.oa_item_key: row.classification_status for row in decisions} == {
            "done:excluded": "excluded",
            "done:internal": "classified",
            "done:external": "classified",
            "done:unknown": "needs_review",
        }
        assert _current(session, "done:internal").content_origin == "internal"
        assert _current(session, "done:external").canonical_issuer == (
            "Synthetic Authority"
        )
        assert _current(session, "done:unknown").content_integrity_status == "ok"


def test_idempotent_rerun_reuses_unchanged_decisions_and_recomputes_one_changed_item(
    factory: sessionmaker[Session], config: PrivateClassificationConfig
) -> None:
    _seed(factory)
    service = ClassificationService(factory, config)
    service.create_run(_request("synthetic-run-a"))
    service.process_next("synthetic-run-a", limit=100)

    service.create_run(_request("synthetic-run-b"))
    service.process_next("synthetic-run-b", limit=100)
    with factory() as session:
        before = {
            key: session.scalar(
                select(ClassificationDecision.version).where(
                    ClassificationDecision.oa_item_key == key,
                    ClassificationDecision.is_current.is_(True),
                )
            )
            for key in ("done:internal", "done:external", "done:unknown")
        }
        internal = session.scalar(
            select(OAItem).where(OAItem.oa_item_key == "done:internal")
        )
        internal.title = "Synthetic internal approval: changed equipment"
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:internal")
        )
        manifest.title = internal.title
        session.commit()

    service.create_run(_request("synthetic-run-c"))
    service.process_next("synthetic-run-c", limit=100)

    with factory() as session:
        after = {
            key: _current(session, key).version
            for key in ("done:internal", "done:external", "done:unknown")
        }
        assert before == {"done:internal": 1, "done:external": 1, "done:unknown": 1}
        assert after == {"done:internal": 2, "done:external": 1, "done:unknown": 1}


def test_resume_retries_only_failed_items(
    factory: sessionmaker[Session], config: PrivateClassificationConfig
) -> None:
    _seed(factory)
    failures = {"done:external": 1}

    def fail_once(item, evidence, outcome):
        del evidence
        if failures.get(item.item_key, 0):
            failures[item.item_key] -= 1
            raise RuntimeError("synthetic classifier interruption")
        return outcome

    service = ClassificationService(factory, config, outcome_hook=fail_once)
    service.create_run(_request("synthetic-resume"))

    first = service.process_next("synthetic-resume", limit=100)
    resumed = service.resume("synthetic-resume")

    assert first.failed == 1
    assert resumed.failed == 0
    assert resumed.decided == 4
    with factory() as session:
        item = session.scalar(
            select(ClassificationRunItem).where(
                ClassificationRunItem.oa_item_key == "done:external"
            )
        )
        assert item.stage == "decided"
        assert item.attempts == 2


def test_manual_lock_is_never_superseded_by_automation_but_new_manual_version_can_replace_it(
    factory: sessionmaker[Session], config: PrivateClassificationConfig
) -> None:
    _seed(factory)
    service = ClassificationService(factory, config)
    service.create_run(_request("synthetic-manual-base"))
    service.process_next("synthetic-manual-base", limit=100)

    first_manual = service.set_manual_decision(
        ManualDecisionCommand(
            run_id="synthetic-manual-base",
            oa_item_key="done:internal",
            actor="synthetic-reviewer",
            reason="Synthetic reviewed category",
            classification_status="classified",
            content_origin="internal",
            business_category="01_公司治理与决策",
            canonical_issuer=None,
            flow_type="manual_review",
            initiator_type="internal",
        )
    )
    with factory.begin() as session:
        item = session.scalar(
            select(OAItem).where(OAItem.oa_item_key == "done:internal")
        )
        item.title = "Synthetic external circulation: changed after manual lock"

    service.create_run(_request("synthetic-manual-rerun"))
    service.process_next("synthetic-manual-rerun", limit=100)

    with factory() as session:
        current = _current(session, "done:internal")
        assert current.id == first_manual.decision_id
        assert current.manual_locked is True
        assert current.business_category == "01_公司治理与决策"

    second_manual = service.set_manual_decision(
        ManualDecisionCommand(
            run_id="synthetic-manual-rerun",
            oa_item_key="done:internal",
            actor="synthetic-reviewer-2",
            reason="Synthetic explicit reclassification",
            classification_status="classified",
            content_origin="internal",
            business_category="03_风险合规审计法务",
            canonical_issuer=None,
            flow_type="manual_review",
            initiator_type="internal",
        )
    )

    assert second_manual.version == first_manual.version + 1
    with factory() as session:
        current = _current(session, "done:internal")
        assert current.id == second_manual.decision_id
        assert current.actor == "synthetic-reviewer-2"
        assert current.supersedes_decision_id == first_manual.decision_id


def test_failed_new_decision_does_not_clear_existing_current_decision(
    factory: sessionmaker[Session], config: PrivateClassificationConfig
) -> None:
    _seed(factory)
    service = ClassificationService(factory, config)
    service.create_run(_request("synthetic-atomic-base"))
    service.process_next("synthetic-atomic-base", limit=100)
    with factory() as session:
        previous_id = _current(session, "done:internal").id
        item = session.scalar(
            select(OAItem).where(OAItem.oa_item_key == "done:internal")
        )
        item.title = "Synthetic internal approval: force changed fingerprint"
        session.commit()

    def invalid_external(item, evidence, outcome):
        del item, evidence
        return replace(
            outcome,
            classification_status="classified",
            content_origin="external",
            canonical_issuer=None,
            business_category=None,
        )

    broken = ClassificationService(factory, config, outcome_hook=invalid_external)
    broken.create_run(_request("synthetic-atomic-failure"))
    progress = broken.process_next("synthetic-atomic-failure", limit=100)

    assert progress.failed >= 1
    with factory() as session:
        assert _current(session, "done:internal").id == previous_id
        assert (
            session.scalar(
                select(ClassificationRunItem.stage).where(
                    ClassificationRunItem.classification_run_id
                    == session.scalar(
                        select(ClassificationRun.id).where(
                            ClassificationRun.run_id == "synthetic-atomic-failure"
                        )
                    ),
                    ClassificationRunItem.oa_item_key == "done:internal",
                )
            )
            == "failed"
        )
