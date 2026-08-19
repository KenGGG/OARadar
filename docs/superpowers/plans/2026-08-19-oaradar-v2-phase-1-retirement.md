# OARadar V2 Phase 1 Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让非核心能力停止自动入队、执行和 Web 暴露，同时保留历史表和源码。

**Architecture:** 用显式核心 stage allowlist 收敛现有 ProductionQueue/OperationWorker，不创建新队列。FastAPI 与 React 只保留三条业务链所需入口；旧数据原地保留，不增加兼容读取平台。

**Tech Stack:** Python 3.12、SQLAlchemy、FastAPI、pytest、React、TypeScript

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- 只修改调度、注册和入口；不得删除旧表、旧数据或旧源码模块。
- 不新增 oa legacy 命令、API facade、任务表或协调器。
- 核心 stage 为 Pending、Done Archive 和 Markdown Delivery 所需 stage。
- 存量退役任务保留并标记 RETIRED_STAGE，不删除。

---

### Task 1: 收敛 Worker 与自动调度

**Files:**
- Modify: `src/oa_knowledge/production_pipeline.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/scheduled_sync.py`
- Modify: `src/oa_knowledge/cli.py`
- Test: `tests/test_production_pipeline.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_scheduled_sync.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `CORE_PIPELINE_STAGES: frozenset[str]`
- Produces: `retire_non_core_tasks(session: Session) -> int`
- Preserves: `ProductionQueue.claim(...)` and existing PipelineTask schema

- [ ] **Step 1: Write failing retirement tests**

Add tests proving `curation`, `ollama_extract`, online audit, governance and historical knowledge tasks are neither bootstrapped nor executed, while `detail_sync`, `pending_parse`, `pending_summary`, `notify_feishu`, `pending_cleanup`, `done_capture_and_archive`, `archive_verify`, `attachment_inventory`, `parse`, `source_publish`, `classify` and `index_publish` remain claimable.

~~~python
def test_worker_retires_non_core_stage(worker, queued_task):
    queued_task.stage = "curation"
    worker.run_production_once()
    assert queued_task.status == "failed"
    assert queued_task.error_code == "RETIRED_STAGE"
    assert queued_task.recoverable is False
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_worker.py tests/test_scheduled_sync.py tests/test_cli.py -q`
Expected: FAIL because non-core stages are still registered or enqueued.

- [ ] **Step 3: Implement the core allowlist**

Define one allowlist in `production_pipeline.py`, make claim/dispatch reject other stages with RETIRED_STAGE, remove stale-curation/bootstrap enqueue calls, and remove non-core default CLI registrations without creating replacement commands.

~~~python
CORE_PIPELINE_STAGES = frozenset({
    "detail_sync", "pending_parse", "pending_summary", "notify_feishu",
    "pending_cleanup", "done_capture_and_archive", "archive_verify",
    "attachment_inventory", "parse", "source_publish", "classify", "index_publish",
})
~~~

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_worker.py tests/test_scheduled_sync.py tests/test_cli.py -q`
Expected: PASS; no test observes new non-core tasks.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/production_pipeline.py src/oa_knowledge/web/worker.py src/oa_knowledge/scheduled_sync.py src/oa_knowledge/cli.py tests/test_production_pipeline.py tests/test_worker.py tests/test_scheduled_sync.py tests/test_cli.py
git commit -m "refactor: retire non-core production stages"
~~~

### Task 2: 退役非核心 Web 与高级维护前端

**Files:**
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/views/SimpleSettingsView.tsx`
- Test: `tests/test_web.py`
- Test: `tests/test_console_views.py`

**Interfaces:**
- Consumes: CORE_PIPELINE_STAGES from Task 1
- Produces: `create_web_app(settings, config_path)` with only core business routes
- Preserves: JSON 404 fallback and existing loopback/CSRF middleware

- [ ] **Step 1: Write failing route and bundle tests**

Assert audit/governance/review/policy/batch/backfill/maintenance/knowledge-processing URLs return JSON 404, core routes remain 200/202, and built frontend text contains no “高级维护”, “Curated”, “在线核验”, “Policy”, “Review”, “Queue” or “Lease”.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_web.py tests/test_console_views.py -q`
Expected: FAIL because retired routes and AdvancedMaintenance are still registered.

- [ ] **Step 3: Remove registrations and imports**

Delete non-core route declarations/imports from `app.py`; remove `AdvancedMaintenance` import/state/rendering and the settings jump callback. Do not delete retired implementation files.

~~~tsx
type View = "overview" | "pending" | "done" | "markdown" | "settings"
~~~

- [ ] **Step 4: Build and run Web tests**

Run:
~~~bash
cd webui
npm run check
npm run build
cd ..
uv run pytest tests/test_web.py tests/test_console_views.py -q
~~~
Expected: all commands exit 0; retired URLs return JSON 404.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/web/app.py webui/src/App.tsx webui/src/views/SimpleSettingsView.tsx src/oa_knowledge/web/static tests/test_web.py tests/test_console_views.py
git commit -m "refactor: remove non-core web surfaces"
~~~
