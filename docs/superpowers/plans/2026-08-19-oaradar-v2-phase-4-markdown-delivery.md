# OARadar V2 Phase 4 Markdown Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 固化 ParseArtifact 到附件 Markdown 的唯一发布链，并为每个 Done 事项生成稳定索引和最小分类 Frontmatter。

**Architecture:** 对现有 OAItem 和 MarkdownExport 做一次最小 Alembic 增量。分类是 OAItem 上四个当前值；附件发布复用 source_markdown/service.py，索引发布使用独立小服务。

**Tech Stack:** Python 3.12、SQLAlchemy、Alembic、Pydantic、pytest、MarkItDown、MinerU

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- 不新增 MarkdownDocument、分类历史、Review 或任务表。
- 已成功且哈希有效的历史 Markdown 不重解析。
- 新附件 Markdown 必须关联 active ParseArtifact。
- 分类不影响路径；索引不生成 AI 知识摘要。

---

### Task 1: 最小数据库迁移与分类服务

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0034_v2_markdown_index.py`
- Modify: `src/oa_knowledge/db/models.py`
- Create: `src/oa_knowledge/classifier.py`
- Modify: `src/oa_knowledge/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_database.py`
- Create: `tests/test_classifier.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ClassificationResult(source_type: str, internal_category: str | None, external_issuer: str | None, classification_version: str)`
- Produces: `classify_item(settings: Settings, item: OAItem, evidence: str) -> ClassificationResult`
- Adds OAItem fields: source_type, internal_category, external_issuer, classification_version
- Adds MarkdownExport fields: oa_item_id, document_kind

- [ ] **Step 1: Write failing migration and classifier tests**

Test upgrade from revision 0033, existing rows unchanged, partial unique item_index constraint, internal/external/unknown invariants, configured group-company scope, rule-first behavior and local-model failure fallback.

~~~python
result = classify_item(settings, item, "synthetic evidence")
assert result.source_type in {"internal", "external", "unknown"}
assert not (result.internal_category and result.external_issuer)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_database.py tests/test_classifier.py tests/test_config.py -q`
Expected: FAIL because revision 0034, fields and classifier do not exist.

- [ ] **Step 3: Implement migration and classifier**

Add nullable columns with check constraints/default-safe migration. Implement deterministic rules first; only construct the local model client when rules are insufficient and llm.enabled is true. Persist only the four current values.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_database.py tests/test_classifier.py tests/test_config.py -q`
Expected: PASS on both fresh and upgraded synthetic databases.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/db/migrations/versions/0034_v2_markdown_index.py src/oa_knowledge/db/models.py src/oa_knowledge/classifier.py src/oa_knowledge/config.py config.example.yaml tests/test_database.py tests/test_classifier.py tests/test_config.py
git commit -m "feat: add minimal item classification metadata"
~~~

### Task 2: 唯一附件发布链与事项索引

**Files:**
- Modify: `src/oa_knowledge/parsers/router.py`
- Modify: `src/oa_knowledge/pipeline.py`
- Modify: `src/oa_knowledge/source_markdown/service.py`
- Create: `src/oa_knowledge/source_markdown/index.py`
- Modify: `src/oa_knowledge/markdown_worker.py`
- Test: `tests/test_parsers.py`
- Test: `tests/test_source_markdown_service.py`
- Create: `tests/test_source_markdown_index.py`
- Test: `tests/test_markdown_queue.py`
- Test: `tests/test_markdown_export.py`

**Interfaces:**
- Produces: `publish_item_index(session: Session, settings: Settings, oa_item_id: int) -> MarkdownExport`
- Consumes: `classify_item(...)` from Task 1
- Stage order: attachment_inventory → parse → source_publish → classify → index_publish

- [ ] **Step 1: Write failing router/publisher/index tests**

Cover Office→MarkItDown, text PDF→MarkItDown, scan/image/complex PDF→MinerU, low-quality fallback, MinerU unavailable retry, active artifact requirement, no reparse of valid legacy success, no-attachment index, multi-attachment links, unsupported status, stable path and unchanged mtime.

~~~python
record = publish_item_index(session, settings, item.id)
assert record.document_kind == "item_index"
assert record.source_file_id is None
assert (settings.markdown_root / record.markdown_relpath).name == "_index.md"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_parsers.py tests/test_source_markdown_service.py tests/test_source_markdown_index.py tests/test_worker.py tests/test_markdown_export.py -q`
Expected: FAIL because classify/index stages and item index publisher do not exist.

- [ ] **Step 3: Implement the minimum delivery chain**

Make the MarkdownWorker source_publish handler call only publish_active_artifact, remove ReviewEntry creation and curation advance, add classify and index_publish handlers, and atomically render _index.md from OA metadata/classification/attachment ledger. Do not call markdown_export.service.parse_file from any production path.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_parsers.py tests/test_source_markdown_service.py tests/test_source_markdown_index.py tests/test_markdown_queue.py tests/test_markdown_export.py -q`
Expected: PASS; new successful exports have parse_artifact_id and every completed item has one index.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/parsers/router.py src/oa_knowledge/pipeline.py src/oa_knowledge/source_markdown/service.py src/oa_knowledge/source_markdown/index.py src/oa_knowledge/markdown_worker.py tests/test_parsers.py tests/test_source_markdown_service.py tests/test_source_markdown_index.py tests/test_markdown_queue.py tests/test_markdown_export.py
git commit -m "feat: publish indexed source markdown from parse artifacts"
~~~
