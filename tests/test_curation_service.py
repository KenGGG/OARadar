from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.curation.service import plan_curation, run_curation
from oa_knowledge.db.engine import create_db_engine
from oa_knowledge.db.migrate import upgrade_database
from oa_knowledge.db.models import (
    ArchivedFile, ContentObject, CuratedDecision, CuratedRun, KnowledgeDocument,
    OAItem, ParseArtifact, ParseJob, SourceReference,
)
from oa_knowledge.enrich.context_budget import LocalModelProfile
from oa_knowledge.resources import ResourceCoordinator
from oa_knowledge.source_markdown.service import publish_active_artifact


class FakeClient:
    def chat(self, _system: str, _user: str, **_kwargs) -> dict:
        return {"error": None, "content": json.dumps({"documents": [{
            "document_kind": "formal", "normalized_title": "关于合成测试的通知", "issuer": "示例集团",
            "document_number": "示例发〔2026〕1号", "publication_date": "2026-08-01", "topic": "",
            "customer": "", "project": "", "stage": "", "confidence": 0.95,
            "sources": [{"source_key": "file:1", "role": "body"}], "evidence_source_keys": ["file:1"],
        }]}, ensure_ascii=False)}


class FailingClient:
    def chat(self, *_args, **_kwargs):
        return {"error": "synthetic_timeout", "content": None}


class InternalClient:
    def chat(self, _system: str, user: str, **_kwargs):
        match = re.search(r"source_key=(S\d+)", user)
        key = match.group(1) if match else "S1"
        title = "内部事项乙" if "事项乙" in user else "内部事项甲"
        return {"error": None, "content": json.dumps({"documents": [{
            "document_kind": "internal", "normalized_title": title, "issuer": "", "document_number": "",
            "publication_date": "2026-08-01", "topic": "综合管理", "customer": "", "project": "", "stage": "",
            "confidence": 0.9, "sources": [{"source_key": key, "role": "body"}], "evidence_source_keys": [key],
        }]}, ensure_ascii=False)}


def setup_source(tmp_path: Path):
    settings = Settings(
        app={"data_root": tmp_path / "data"},
        runtime={"state_root": tmp_path / "state", "cache_root": tmp_path / "cache"},
        curation={"enabled": True}, llm={"enabled": True},
    )
    upgrade_database(settings.database_path)
    engine = create_db_engine(settings.database_path)
    text = "示例集团文件\n示例发〔2026〕1号\n关于合成测试的通知"
    path = settings.parse_work_root / "synthetic" / "synthetic.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    original = settings.data_root / "originals/2026/08/2026-08-15_合成测试通知/source.docx"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"source")
    with Session(engine) as session:
        item = OAItem(
            oa_item_key="done:synthetic", source_channel="done", title="合成测试通知",
            completed_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
        session.add(item); session.flush()
        content = ContentObject(sha256=hashlib.sha256(b"source").hexdigest(), size_bytes=6, detected_type="docx")
        session.add(content); session.flush()
        source_file = ArchivedFile(
            oa_item_id=item.id, original_name="示例发〔2026〕1号.md", attachment_key="synthetic",
            file_role="direct_attachment", source_container_key="root", depth=1,
            content_object_id=content.id, sha256=content.sha256, download_status="verified",
            local_relpath="originals/2026/08/2026-08-15_合成测试通知/source.docx", size_bytes=6,
        )
        session.add(source_file); session.flush()
        job = ParseJob(file_id=source_file.id, engine="synthetic", engine_version="1", config_hash="a" * 64, status="completed")
        session.add(job); session.flush()
        artifact = ParseArtifact(
            parse_job_id=job.id, content_object_id=content.id, engine="synthetic", engine_version="1",
            output_relpath="work/synthetic/synthetic.md", source_sha256=content.sha256, product_sha256=hashlib.sha256(text.encode()).hexdigest(),
            config_hash="a" * 64, lifecycle_status="valid",
        )
        session.add(artifact); session.flush(); content.active_parse_artifact_id = artifact.id
        publish_active_artifact(session, settings, source_file.id)
        session.commit()
    return settings, engine, original


def test_plan_is_read_only_and_run_is_incremental(monkeypatch, tmp_path: Path) -> None:
    settings, engine, source_path = setup_source(tmp_path)
    original_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "oa_knowledge.curation.service.discover_ollama_profile",
        lambda *_args, **_kwargs: LocalModelProfile("qwen3.5:9b", 32_768, True),
    )

    planned = plan_curation(settings, engine)
    with Session(engine) as session:
        assert session.query(CuratedRun).count() == 0

    first = run_curation(settings, engine, client=FakeClient())
    second = run_curation(settings, engine, client=FakeClient())

    assert planned.packages == 1 and planned.candidate_sources == 1
    assert first.completed == 1 and first.failed == 0
    assert second.skipped == 1
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == original_hash
    assert {path.name for path in settings.data_root.iterdir()} == {"originals", "markdown"}
    curated_body = next((settings.markdown_root / "curated/oa").rglob("正文.md")).read_text(encoding="utf-8")
    assert "示例集团文件" in curated_body
    assert "转换说明" not in curated_body
    assert "转换引擎" not in curated_body
    with Session(engine) as session:
        assert session.query(CuratedRun).count() == 1
        assert session.query(CuratedDecision).one().status == "published"
        assert session.query(KnowledgeDocument).count() == 1
        assert session.query(SourceReference).count() == 1


def test_failed_model_call_retries_same_signature(monkeypatch, tmp_path: Path) -> None:
    settings, engine, _source_path = setup_source(tmp_path)
    monkeypatch.setattr(
        "oa_knowledge.curation.service.discover_ollama_profile",
        lambda *_args, **_kwargs: LocalModelProfile("qwen3.5:9b", 32_768, True),
    )

    failed = run_curation(settings, engine, client=FailingClient())
    retried = run_curation(settings, engine, client=FakeClient())

    assert failed.failed == 1
    assert retried.completed == 1
    with Session(engine) as session:
        assert session.query(CuratedRun).count() == 1
        assert session.query(CuratedRun).one().status == "completed"


def test_local_model_call_respects_shared_gpu_lease(monkeypatch, tmp_path: Path) -> None:
    settings, engine, _source_path = setup_source(tmp_path)
    monkeypatch.setattr(
        "oa_knowledge.curation.service.discover_ollama_profile",
        lambda *_args, **_kwargs: LocalModelProfile("qwen3.5:9b", 32_768, True),
    )
    coordinator = ResourceCoordinator(engine)
    lease_id = coordinator.acquire("local_llm", "synthetic-holder", ttl_seconds=60, uses_local_gpu=True)
    assert lease_id is not None

    class UnexpectedClient:
        def chat(self, *_args, **_kwargs):
            raise AssertionError("model must not run while the shared GPU lease is held")

    try:
        result = run_curation(settings, engine, client=UnexpectedClient())
    finally:
        coordinator.release(lease_id, "synthetic-holder")

    assert result.failed == 1
    with Session(engine) as session:
        run = session.query(CuratedRun).one()
        assert run.status == "failed"
        assert run.error_detail == "local model resource is busy"


def test_exact_content_is_globally_deduplicated_across_oa_packages(monkeypatch, tmp_path: Path) -> None:
    settings, engine, _source_path = setup_source(tmp_path)
    with Session(engine) as session:
        first_file = session.get(ArchivedFile, 1)
        assert first_file is not None
        second = OAItem(oa_item_key="done:synthetic-2", source_channel="done", title="事项乙")
        session.add(second); session.flush()
        second_file = ArchivedFile(
            oa_item_id=second.id, original_name="相同内容.md", attachment_key="same",
            file_role="direct_attachment", source_container_key="root", depth=1,
            content_object_id=first_file.content_object_id, sha256=first_file.sha256, download_status="verified",
            local_relpath="originals/2026/08/2026-08-15_事项乙/source.docx", size_bytes=6,
        )
        session.add(second_file); session.flush()
        second_original = settings.data_root / second_file.local_relpath
        second_original.parent.mkdir(parents=True)
        second_original.write_bytes(b"source")
        artifact_path = settings.cache_root / "work/synthetic/synthetic.md"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("示例集团文件\n示例发〔2026〕1号\n关于合成测试的通知", encoding="utf-8")
        publish_active_artifact(session, settings, second_file.id)
        first = session.get(OAItem, 1); assert first is not None; first.title = "事项甲"
        session.commit()
    monkeypatch.setattr(
        "oa_knowledge.curation.service.discover_ollama_profile",
        lambda *_args, **_kwargs: LocalModelProfile("qwen3.5:9b", 32_768, True),
    )

    result = run_curation(settings, engine, limit=2, client=InternalClient())

    assert result.completed == 2
    with Session(engine) as session:
        assert session.query(KnowledgeDocument).count() == 1
        assert session.query(SourceReference).count() == 2
    assert len(list((settings.markdown_root / "curated/oa").rglob("_manifest.json"))) == 1
