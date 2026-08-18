from oa_knowledge.curation.package import OAPackage, PackageSource, extract_source_document_body, package_signature


def test_package_manifest_is_stable_and_source_ordered() -> None:
    sources = (
        PackageSource(source_key="file:2", title="B", markdown_relpath="parse/b.md", content_sha256="b" * 64, markdown_sha256="2" * 64, text="B", ordinal=2),
        PackageSource(source_key="file:1", title="A", markdown_relpath="parse/a.md", content_sha256="a" * 64, markdown_sha256="1" * 64, text="A", ordinal=1),
    )
    package = OAPackage(package_key="oa:synthetic", title="Synthetic", completed_at="2026-08-15", sources=sources)

    assert [row["source_key"] for row in package.manifest()] == ["file:1", "file:2"]
    assert package.source_keys == frozenset({"file:1", "file:2"})


def test_package_signature_invalidates_on_source_or_version_change() -> None:
    source = PackageSource(source_key="file:1", title="A", markdown_relpath="parse/a.md", content_sha256="a" * 64, markdown_sha256="1" * 64, text="A")
    package = OAPackage(package_key="oa:synthetic", title="Synthetic", completed_at=None, sources=(source,))

    baseline = package_signature(package, rules_version="r1", prompt_version="p1", schema_version="s1", model="qwen3.5:9b", config_signature="c1")
    changed = package_signature(package, rules_version="r2", prompt_version="p1", schema_version="s1", model="qwen3.5:9b", config_signature="c1")

    assert baseline != changed


def test_depth_limit_is_never_complete() -> None:
    package = OAPackage(package_key="oa:synthetic", title="Synthetic", completed_at=None, sources=(), depth_limit_reached=True)
    assert package.completable is False


def test_source_markdown_wrapper_is_removed_before_classification_and_publication() -> None:
    wrapped = """---
source_sha256: synthetic
---

# 原始文件.docx

> [!info] 来源信息
> - 转换引擎：synthetic

## 文档内容

# 正文标题

这是需要保留的正文。

## 转换说明

- 转换状态：success
"""

    assert extract_source_document_body(wrapped) == "# 正文标题\n\n这是需要保留的正文。"
