from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oa_knowledge.archive import sha256_file
from oa_knowledge.classification.semantic_package_loader import (
    DatabaseSemanticPackageLoader,
)
from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    Base,
    ClassificationDecision,
    ClassificationRun,
    ContentObject,
    OAItem,
    OAManifestItem,
    ParseArtifact,
    ParseJob,
)


def test_loader_reuses_existing_parse_artifact_without_opening_originals(tmp_path: Path) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    cache_root = tmp_path / "cache"
    artifact_path = cache_root / "parsed.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("广州市工业和信息化局\n公开通知", encoding="utf-8")
    settings = Settings(runtime={"state_root": tmp_path / "state", "cache_root": cache_root})
    with factory.begin() as session:
        session.add_all((
            OAManifestItem(oa_item_key="done:one", title="【文件传阅】公开通知", sender="sender", list_page=1, list_ordinal=1, processing_status="downloaded"),
            OAItem(oa_item_key="done:one", source_channel="done", title="【文件传阅】公开通知", sender="sender", document_number="穗工信函〔2025〕18号", pipeline_status="files_verified"),
            ClassificationRun(run_id="prior", run_kind="incremental", status="completed", input_signature="a" * 64, manifest_sha256="a" * 64, exclusion_policy_sha256="b" * 64, rule_version="r", schema_version="s", prompt_version="p", model_name="local", private_config_sha256="c" * 64, target_count=1, excluded_count=0),
        ))
        session.flush()
        item = session.query(OAItem).filter_by(oa_item_key="done:one").one()
        run = session.query(ClassificationRun).filter_by(run_id="prior").one()
        content = ContentObject(sha256="d" * 64)
        session.add(content)
        session.flush()
        file = ArchivedFile(oa_item_id=item.id, original_name="公开通知.pdf", attachment_key="a1", file_role="attachment", source_container_key="root", depth=1, download_status="verified", sha256="d" * 64, content_object_id=content.id)
        session.add(file)
        session.flush()
        job = ParseJob(file_id=file.id, engine="mineru", engine_version="1", config_hash="e" * 64, status="completed", attempts=1)
        session.add(job)
        session.flush()
        session.add(ParseArtifact(parse_job_id=job.id, content_object_id=content.id, engine="mineru", engine_version="1", profile_version="v", output_relpath="parsed.md", source_sha256="d" * 64, product_sha256="f" * 64, config_hash="e" * 64, lifecycle_status="valid"))
        session.add(ClassificationDecision(classification_run_id=run.id, oa_item_key="done:one", version=1, is_current=True, decision_input_sha256="a" * 64, decision_source="metadata_rule", classification_status="needs_review", content_integrity_status="ok", content_origin="external", flow_type="external_inbound", initiator="sender", initiator_type="internal", relay_from=None, transfer_chain_json="[]", issuer=None, canonical_issuer=None, business_category=None, document_number="穗工信函〔2025〕18号", document_type=None, normalized_title="公开通知", classification_confidence=0.5, classification_reason_json="{}", rule_version="r", private_config_sha256="c" * 64))

    package, eligibility = DatabaseSemanticPackageLoader(factory, settings)("done:one")

    assert package.parsed_attachments == (("公开通知.pdf", "广州市工业和信息化局\n公开通知"),)
    assert package.parse_artifact_hashes == ("f" * 64,)
    assert eligibility.status == "allowed"


def test_loader_reads_verified_direct_text_only_when_no_parse_artifact_exists(tmp_path: Path) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    data_root = tmp_path / "data"
    source = data_root / "originals" / "public.txt"
    source.parent.mkdir(parents=True)
    source.write_text("广州市工业和信息化局\n关于公开事项的通知", encoding="utf-8")
    settings = Settings(app={"data_root": data_root}, runtime={"state_root": tmp_path / "state", "cache_root": tmp_path / "cache"})
    with factory.begin() as session:
        session.add_all((
            OAManifestItem(oa_item_key="done:text", title="【文件传阅】公开通知", sender="sender", list_page=1, list_ordinal=1, processing_status="downloaded"),
            OAItem(oa_item_key="done:text", source_channel="done", title="【文件传阅】公开通知", sender="sender", document_number="穗工信函〔2025〕18号", pipeline_status="files_verified"),
            ClassificationRun(run_id="prior", run_kind="incremental", status="completed", input_signature="a" * 64, manifest_sha256="a" * 64, exclusion_policy_sha256="b" * 64, rule_version="r", schema_version="s", prompt_version="p", model_name="local", private_config_sha256="c" * 64, target_count=1, excluded_count=0),
        ))
        session.flush()
        item = session.query(OAItem).filter_by(oa_item_key="done:text").one()
        run = session.query(ClassificationRun).filter_by(run_id="prior").one()
        session.add(ArchivedFile(oa_item_id=item.id, original_name="public.txt", attachment_key="a1", file_role="attachment", source_container_key="root", depth=1, download_status="verified", local_relpath="originals/public.txt", size_bytes=source.stat().st_size, sha256=sha256_file(source)))
        session.add(ClassificationDecision(classification_run_id=run.id, oa_item_key="done:text", version=1, is_current=True, decision_input_sha256="a" * 64, decision_source="metadata_rule", classification_status="needs_review", content_integrity_status="ok", content_origin="external", flow_type="external_inbound", initiator="sender", initiator_type="internal", relay_from=None, transfer_chain_json="[]", issuer=None, canonical_issuer=None, business_category=None, document_number="穗工信函〔2025〕18号", document_type=None, normalized_title="公开通知", classification_confidence=0.5, classification_reason_json="{}", rule_version="r", private_config_sha256="c" * 64))

    package, eligibility = DatabaseSemanticPackageLoader(factory, settings)("done:text")

    assert package.parsed_attachments == (("public.txt", "广州市工业和信息化局\n关于公开事项的通知"),)
    assert package.parse_artifact_hashes == (sha256_file(source),)
    assert eligibility.status == "allowed"
