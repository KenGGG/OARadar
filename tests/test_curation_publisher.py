from __future__ import annotations

import hashlib
import json
from pathlib import Path

from oa_knowledge.curation.package import PackageSource
from oa_knowledge.curation.publisher import publish_document, remove_managed_publication, validate_publication
from oa_knowledge.curation.schemas import DocumentDecision


def test_publisher_assembles_verbatim_body_and_ordered_attachments(tmp_path: Path) -> None:
    body = "# 原始正文\n\n金额为100元。"
    attachment = "# 原始附件\n\n附件事实。"
    sources = {
        "file:1": PackageSource("file:1", "正文.md", "parse/1.md", "a" * 64, hashlib.sha256(body.encode()).hexdigest(), body, ordinal=1),
        "file:2": PackageSource("file:2", "附件表.md", "parse/2.md", "b" * 64, hashlib.sha256(attachment.encode()).hexdigest(), attachment, ordinal=2),
    }
    decision = DocumentDecision.model_validate({
        "document_kind": "formal", "normalized_title": "关于测试的通知", "issuer": "示例集团",
        "document_number": "示例发〔2026〕1号", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.95,
        "sources": [{"source_key": "file:1", "role": "body"}, {"source_key": "file:2", "role": "attachment"}],
        "evidence_source_keys": ["file:1", "file:2"],
    })

    result = publish_document(tmp_path, decision, sources, canonical_id="formal:test", decision_version=1, fallback_date="2026-08-15")

    root = tmp_path / result.relpath
    assert (root / "正文.md").read_text(encoding="utf-8").endswith(body + "\n")
    assert (root / "附件01_附件表.md").read_text(encoding="utf-8").endswith(attachment + "\n")
    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    assert [source["source_key"] for source in manifest["sources"]] == ["file:1", "file:2"]
    assert validate_publication(tmp_path, result.relpath) == []


def test_republish_removes_stale_managed_attachment(tmp_path: Path) -> None:
    body = "正文"
    sources = {"file:1": PackageSource("file:1", "正文.md", "parse/1.md", "a" * 64, hashlib.sha256(body.encode()).hexdigest(), body)}
    decision = DocumentDecision.model_validate({
        "document_kind": "internal", "normalized_title": "内部事项", "issuer": "", "document_number": "",
        "publication_date": "2026-08-01", "topic": "综合管理", "customer": "", "project": "", "stage": "",
        "confidence": 0.9, "sources": [{"source_key": "file:1", "role": "body"}], "evidence_source_keys": ["file:1"],
    })
    first = publish_document(tmp_path, decision, sources, canonical_id="content:test", decision_version=1, fallback_date="2026-08-15")
    stale = tmp_path / first.relpath / "附件99_旧.md"
    stale.write_text("stale", encoding="utf-8")

    publish_document(tmp_path, decision, sources, canonical_id="content:test", decision_version=2, fallback_date="2026-08-15")

    assert not stale.exists()


def test_stale_path_cleanup_only_removes_matching_managed_publication(tmp_path: Path) -> None:
    managed = tmp_path / "workspace/curated/oa/old"
    managed.mkdir(parents=True)
    (managed / "_manifest.json").write_text(json.dumps({
        "schema_version": "curated-manifest-v1", "canonical_id": "content:test",
    }), encoding="utf-8")
    unmanaged = tmp_path / "workspace/curated/oa/user-folder"
    unmanaged.mkdir(parents=True)
    (unmanaged / "notes.md").write_text("user", encoding="utf-8")

    assert remove_managed_publication(tmp_path, "workspace/curated/oa/old", canonical_id="content:test")
    assert not managed.exists()
    assert not remove_managed_publication(tmp_path, "workspace/curated/oa/user-folder", canonical_id="content:test")
    assert unmanaged.exists()
