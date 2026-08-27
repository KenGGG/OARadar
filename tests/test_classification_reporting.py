from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from test_classification_service import _request, _seed

from oa_knowledge.classification.reporting import build_classification_run_report
from oa_knowledge.classification.schemas import PrivateClassificationConfig
from oa_knowledge.classification.service import ClassificationService
from oa_knowledge.db.models import Base


def test_report_reconciles_mutually_exclusive_terminal_sets_and_role_groups() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = PrivateClassificationConfig.model_validate(
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
    _seed(factory)
    service = ClassificationService(factory, config)
    service.create_run(_request("synthetic-report"))
    service.process_next("synthetic-report", limit=100)

    report = build_classification_run_report(factory, "synthetic-report", config)

    assert report.total == 4
    assert report.excluded == 1
    assert report.classification_target == 3
    assert report.total == (
        report.excluded
        + report.publishable
        + report.integrity_blocked
        + report.needs_review
    )
    assert report.classification_target == (
        report.publishable + report.integrity_blocked + report.needs_review
    )
    assert (report.internal, report.external, report.needs_review) == (1, 1, 1)
    assert report.initiator_roles == {
        "internal": ("synth.internal",),
        "external": ("synth.external",),
        "mixed": ("synth.mixed",),
        "system": ("synth.system",),
        "unknown": ("synth.unknown",),
    }
    assert report.unknown_initiators == ("synth.unknown",)
    assert report.decision_sources == {"metadata_rule": 3}
    assert report.needs_parse == 1
    assert report.actual_parse_count == 0
    assert report.expected_qwen_calls == 0
    assert report.actual_qwen_calls == 0
    assert report.conflicts == 0
    assert report.unrecognized_issuers == 0
    assert report.canonical_document_deduplications == 0
    engine.dispose()


def test_complete_persists_the_same_reconciled_summary() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = PrivateClassificationConfig.model_validate(
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
            "issuer_aliases": {"Synthetic Authority": "Synthetic Authority"},
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
    _seed(factory)
    service = ClassificationService(factory, config)
    service.create_run(_request("synthetic-complete"))
    service.process_next("synthetic-complete", limit=100)

    report = service.complete("synthetic-complete")

    assert report.reconciled is True
    with factory() as session:
        from sqlalchemy import select

        from oa_knowledge.db.models import ClassificationRun

        run = session.scalar(
            select(ClassificationRun).where(
                ClassificationRun.run_id == "synthetic-complete"
            )
        )
        assert run.status == "completed"
        assert run.finished_at is not None
        assert '"reconciled":true' in run.summary_json
    engine.dispose()
