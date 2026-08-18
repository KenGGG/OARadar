from oa_knowledge.curation.canonical import canonical_key, publication_relpath, sanitize_component
from oa_knowledge.curation.schemas import DocumentDecision


def decision(**overrides) -> DocumentDecision:
    payload = {
        "document_kind": "formal", "normalized_title": "关于测试/安全工作的通知", "issuer": "示例集团",
        "document_number": "示例发〔2026〕12号", "publication_date": "2026-08-01", "topic": "",
        "customer": "", "project": "", "stage": "", "confidence": 0.9,
        "sources": [{"source_key": "file:1", "role": "body"}], "evidence_source_keys": ["file:1"],
    }
    payload.update(overrides)
    return DocumentDecision.model_validate(payload)


def test_formal_canonical_path_uses_issuer_year_number_and_title() -> None:
    item = decision()
    relpath = publication_relpath(item, fallback_date="2026-08-15", collision_key="abc123")
    assert relpath.as_posix() == "workspace/curated/oa/正式文件/示例集团/2026/示例发〔2026〕12号_关于测试_安全工作的通知"
    assert canonical_key(item, ["a" * 64]).startswith("formal:")


def test_internal_and_project_paths_follow_business_taxonomy() -> None:
    internal = decision(document_kind="internal", document_number="", issuer="", topic="预算管理")
    project = decision(document_kind="project", document_number="", issuer="", customer="示例客户", project="示例项目", stage="投标")
    assert publication_relpath(internal, fallback_date="2026-08-15", collision_key="x").parts[3:6] == ("公司内部", "预算管理", "2026-08")
    assert publication_relpath(project, fallback_date="2026-08-15", collision_key="x").parts[3:7] == ("项目资料", "示例客户", "示例项目", "投标")


def test_path_sanitization_is_stable_and_length_bounded() -> None:
    assert sanitize_component(" a/b:*? ") == "a_b"
    assert len(sanitize_component("甲" * 200, collision_key="abcdef12")) <= 80
