from oa_knowledge.curation.package import PackageSource
from oa_knowledge.curation.rules import SourceDisposition, classify_source


def source(name: str, text: str) -> PackageSource:
    return PackageSource(
        source_key="file:1", title=name, markdown_relpath="parse/1.md",
        content_sha256="a" * 64, markdown_sha256="b" * 64, text=text,
    )


def test_rules_remove_obvious_workflow_shell() -> None:
    result = classify_source(source("传阅单.md", "已阅。流程节点：发起、部门负责人、办结。"))
    assert result.disposition == SourceDisposition.NOISE
    assert result.reason_code == "workflow_shell"


def test_rules_keep_formal_document_candidate() -> None:
    result = classify_source(source("示例发〔2026〕12号.md", "示例集团文件\n示例发〔2026〕12号\n关于安全工作的通知"))
    assert result.disposition == SourceDisposition.CANDIDATE
    assert result.reason_code == "formal_document_signal"


def test_rules_route_ambiguous_material_to_model() -> None:
    result = classify_source(source("附件.md", "关于某项工作的具体安排和说明。"))
    assert result.disposition == SourceDisposition.AMBIGUOUS
