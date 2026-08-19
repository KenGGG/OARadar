# OARadar V2 Phase 3 Archive Markdown Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让 Done Archive 只对原件完整性负责，并通过现有 PipelineTask 触发独立 Markdown Delivery。

**Architecture:** OA Worker 运行 archive_verify 并移除 Done 路径的 Audit/Knowledge 回调。验证成功后在现有 PipelineTask 中建立 queue_name=markdown_delivery 的事项任务；现有 MarkdownWorker 领取该 queue 并执行 Markdown stage，不新增任务表或协调器。

**Tech Stack:** Python 3.12、SQLAlchemy、pytest

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- 已验证文件不重复下载、不覆盖。
- Markdown 失败不得改变 Done 归档成功。
- 第 10 层仍有子级时必须 depth_limit_reached。
- Markdown Delivery task 仍是 PipelineTask 行。

---

### Task 1: 本地归档验证 stage

**Files:**
- Create: `src/oa_knowledge/archive/verification.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/markdown_worker.py`
- Modify: `src/oa_knowledge/scheduled_sync.py`
- Test: `tests/test_archive.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_scheduled_sync.py`

**Interfaces:**
- Produces: `ArchiveVerification(status: str, verified_files: int, issue_codes: tuple[str, ...], content_signature: str)`
- Produces: `verify_done_archive(session: Session, settings: Settings, oa_item_key: str) -> ArchiveVerification`
- Produces stage: `archive_verify`

- [ ] **Step 1: Write failing verifier tests**

Cover no_attachment, all verified, missing path, size mismatch, SHA mismatch, unsafe relative path, and depth-10 node with children.

~~~python
result = verify_done_archive(session, settings, "done:synthetic-1")
assert result.status == "attention"
assert "DEPTH_LIMIT_REACHED" in result.issue_codes
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_archive.py tests/test_worker.py tests/test_scheduled_sync.py -q`
Expected: FAIL because archive verification is embedded in capture and has no result object.

- [ ] **Step 3: Implement verifier and stage transition**

Implement a pure local verifier using resolve_data_path and sha256_file. Make done_capture_and_archive advance to archive_verify and remove online_audit requeue calls.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_archive.py tests/test_worker.py tests/test_scheduled_sync.py -q`
Expected: PASS; Markdown status never participates in ArchiveVerification.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/archive/verification.py src/oa_knowledge/web/worker.py src/oa_knowledge/scheduled_sync.py tests/test_archive.py tests/test_worker.py tests/test_scheduled_sync.py
git commit -m "refactor: verify done archives from local facts"
~~~

### Task 2: 用现有 PipelineTask 拆分 Markdown Delivery

**Files:**
- Modify: `src/oa_knowledge/production_pipeline.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Test: `tests/test_production_pipeline.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `ArchiveVerification.content_signature`
- Produces: `enqueue_markdown_delivery(queue: ProductionQueue, oa_item_key: str, content_signature: str, schema_version: str) -> int`
- Produces key: `markdown:{oa_item_key}:{content_signature}:{schema_version}`

- [ ] **Step 1: Write failing boundary tests**

Assert archive task completes after creating one PipelineTask at attachment_inventory, repeated verification reuses the same idempotency key, and Markdown failure leaves manifest processing_status downloaded/no_attachment.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_worker.py -q`
Expected: FAIL because the current Done task continues through parse/source_publish/curation.

- [ ] **Step 3: Implement the existing-table handoff**

Add a ProductionQueue helper that inserts PipelineTask(queue_name="markdown_delivery", stage="attachment_inventory") and priority 20. Extend the existing MarkdownWorker to claim only markdown_delivery item tasks before its existing MarkdownTask loop; move the four local Markdown stage handlers from OperationWorker without creating a second coordinator class, model, table, service or OperationJob type.

~~~python
key = f"markdown:{oa_item_key}:{content_signature}:{schema_version}"
return queue.enqueue("markdown_delivery", oa_item_key, "attachment_inventory", key)
~~~

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_worker.py tests/test_archive.py -q`
Expected: PASS; archive task and Markdown task have distinct IDs.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/production_pipeline.py src/oa_knowledge/web/worker.py src/oa_knowledge/markdown_worker.py tests/test_production_pipeline.py tests/test_worker.py tests/test_markdown_queue.py
git commit -m "refactor: separate archive and markdown tasks"
~~~
