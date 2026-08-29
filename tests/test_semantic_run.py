from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from oa_knowledge.classification.agnes_eligibility import AgnesEligibility
from oa_knowledge.classification.semantic_classifier import (
    SemanticClassificationResult,
    SemanticOutcome,
    SemanticPackage,
)
from oa_knowledge.classification.semantic_run import (
    SemanticReviewService,
    semantic_target_keys,
)
from oa_knowledge.db.models import (
    Base,
    ClassificationDecision,
    ClassificationRun,
    ClassificationRunItem,
    OAItem,
    OAManifestItem,
)


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(factory: sessionmaker[Session], *, locked: bool = False) -> None:
    with factory.begin() as session:
        session.add_all((
            OAManifestItem(oa_item_key="done:one", title="公开通知", sender="sender", list_page=1, list_ordinal=1, processing_status="downloaded"),
            OAItem(oa_item_key="done:one", source_channel="done", title="公开通知", sender="sender", pipeline_status="files_verified"),
            ClassificationRun(run_id="prior", run_kind="incremental", status="completed", input_signature="a" * 64, manifest_sha256="a" * 64, exclusion_policy_sha256="b" * 64, rule_version="r", schema_version="s", prompt_version="p", model_name="local", private_config_sha256="c" * 64, target_count=1, excluded_count=0),
        ))
        session.flush()
        prior = session.scalar(select(ClassificationRun).where(ClassificationRun.run_id == "prior"))
        session.add(ClassificationDecision(
            classification_run_id=prior.id, oa_item_key="done:one", version=1, is_current=True,
            decision_input_sha256="d" * 64, decision_source="manual" if locked else "metadata_rule", classification_status="needs_review",
            content_integrity_status="ok", content_origin="external", flow_type="external_inbound",
            initiator="sender", initiator_type="internal", relay_from=None, transfer_chain_json="[]", issuer=None,
            canonical_issuer=None, business_category=None, document_number="穗工信函〔2025〕18号",
            document_type=None, normalized_title="公开通知", classification_confidence=0.5,
            classification_reason_json="{}", rule_version="r", private_config_sha256="c" * 64,
            manual_locked=locked, actor="reviewer" if locked else None,
        ))


@dataclass
class _Classifier:
    result: SemanticClassificationResult
    calls: int = 0

    def classify(self, _package: SemanticPackage, _eligibility: AgnesEligibility) -> SemanticClassificationResult:
        self.calls += 1
        return self.result


def _result(*, confidence: float = 0.96) -> SemanticClassificationResult:
    return SemanticClassificationResult(
        provider="agnes", input_sha256="e" * 64, eligibility_reason="external_public_formal_document",
        cache_hit=False, rejection_code=None, model="agnes-2.0-flash",
        outcome=SemanticOutcome.model_validate({
            "classification_status": "classified", "content_origin": "external", "flow_type": "external_inbound",
            "canonical_issuer": "广州市工业和信息化局", "business_category": None, "document_type": "通知",
            "confidence": confidence, "review_current": "classify",
            "evidence": [{"source": "附件01", "type": "signature", "summary": "文尾落款为广州市工业和信息化局"}],
            "reason": "落款可识别发文机关。",
        }),
    )


def _package(_key: str) -> tuple[SemanticPackage, AgnesEligibility]:
    return (
        SemanticPackage("done:one", "公开通知", "穗工信函〔2025〕18号", ("通知.pdf",), (("附件01", "公开通知\n广州市工业和信息化局"),), ("a" * 64,), None),
        AgnesEligibility("allowed", "external_public_formal_document"),
    )


def test_semantic_run_adopts_high_confidence_review_resolution() -> None:
    factory = _factory()
    _seed(factory)
    classifier = _Classifier(_result())
    service = SemanticReviewService(factory, classifier, _package)

    ref = service.create_run("semantic-v2", ("done:one",), private_config_sha256="c" * 64)
    progress = service.process_next(ref.run_id, limit=1)

    assert (ref.target_count, ref.excluded_count) == (1, 0)
    assert progress.decided == 1
    with factory() as session:
        current = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key == "done:one", ClassificationDecision.is_current.is_(True)))
        assert current.version == 2
        assert current.decision_source == "agnes"
        assert current.canonical_issuer == "广州市工业和信息化局"
        assert json.loads(current.classification_reason_json)["prompt_version"] == "agnes-classifier-v1.1"
        item = session.scalar(
            select(ClassificationRunItem)
            .join(ClassificationRun)
            .where(ClassificationRun.run_id == "semantic-v2")
        )
        assert item is not None
        audit = json.loads(item.last_error_detail)
        assert audit["provider"] == "agnes"
        assert audit["input_sha256"] == "e" * 64
        assert audit["eligibility_reason"] == "external_public_formal_document"
        assert audit["result"] == "classified"


def test_semantic_run_preserves_manual_lock_without_calling_model() -> None:
    factory = _factory()
    _seed(factory, locked=True)
    classifier = _Classifier(_result())
    service = SemanticReviewService(factory, classifier, _package)
    service.create_run("semantic-v2", ("done:one",), private_config_sha256="c" * 64)

    service.process_next("semantic-v2", limit=1)

    assert classifier.calls == 0
    with factory() as session:
        current = session.scalar(select(ClassificationDecision).where(ClassificationDecision.oa_item_key == "done:one", ClassificationDecision.is_current.is_(True)))
        item = session.scalar(select(ClassificationRunItem).join(ClassificationRun).where(ClassificationRun.run_id == "semantic-v2"))
        assert current.version == 1
        assert item.adopted_decision_id == current.id


def test_semantic_run_does_not_send_an_item_without_parsed_content_to_a_model() -> None:
    factory = _factory()
    _seed(factory)
    classifier = _Classifier(_result())

    def empty_package(_key: str) -> tuple[SemanticPackage, AgnesEligibility]:
        return (
            SemanticPackage("done:one", "公开通知", None, (), (), (), None),
            AgnesEligibility("local_only", "no_parseable_content"),
        )

    service = SemanticReviewService(factory, classifier, empty_package)
    service.create_run("semantic-v2", ("done:one",), private_config_sha256="c" * 64)

    progress = service.process_next("semantic-v2", limit=1)

    assert classifier.calls == 0
    assert progress.decided == 1


def test_semantic_target_keys_excludes_gate_zero_items() -> None:
    factory = _factory()
    _seed(factory)
    with factory.begin() as session:
        manifest = session.scalar(
            select(OAManifestItem).where(OAManifestItem.oa_item_key == "done:one")
        )
        assert manifest is not None
        manifest.matched_exclusion_keyword = "旧文件"

    with factory() as session:
        assert semantic_target_keys(session) == ()


def test_semantic_run_recovers_a_worker_interrupted_during_content_stage() -> None:
    factory = _factory()
    _seed(factory)
    service = SemanticReviewService(factory, _Classifier(_result()), _package)
    service.create_run("semantic-v2", ("done:one",), private_config_sha256="c" * 64)
    with factory.begin() as session:
        item = session.scalar(select(ClassificationRunItem))
        assert item is not None
        item.stage = "content"
        item.attempts = 1

    assert service.recover_interrupted("semantic-v2") == 1
    with factory() as session:
        item = session.scalar(select(ClassificationRunItem))
        assert item is not None
        assert item.stage == "queued"
        assert item.last_error_code == "worker_interrupted"
