# OARadar V2 Phase 2 Pending Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 让 Pending 在 baseline 后对新增/变化版本最多通知一次，并在成功后可靠清理业务内容。

**Architecture:** 继续使用 ItemOccurrence、ItemSnapshot、SummaryVersion、NotificationDelivery 和 PipelineTask。摘要采用本地模型可选、规则兜底；清理成为同一 PipelineTask 的显式 stage。

**Tech Stack:** Python 3.12、SQLAlchemy、Pydantic、pytest

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- Pending 不产生永久归档、Source Markdown 或 Curated 数据。
- llm.enabled=false 时模型客户端调用次数必须为 0。
- unknown_outcome 不自动重发。
- 飞书 sent 后的任何恢复路径都不得再次发送。

---

### Task 1: Baseline、版本幂等与规则摘要

**Files:**
- Modify: `src/oa_knowledge/scheduled_sync.py`
- Modify: `src/oa_knowledge/pending_sync.py`
- Modify: `src/oa_knowledge/pending_summary.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Test: `tests/test_scheduled_sync.py`
- Test: `tests/test_pending_summary.py`
- Test: `tests/test_e2e_autorun.py`

**Interfaces:**
- Produces: `deterministic_pending_fallback(payload: str) -> PendingSummary`
- Produces task key: `pending:{occurrence_key}:{discovery_hash}:v2`
- Uses existing Run with stage `scheduled_bootstrap` as baseline fact

- [ ] **Step 1: Write failing baseline and LLM-off tests**

Add tests proving bootstrap creates/updates occurrences without PipelineTask, the next changed hash creates exactly one task, repeated scans create none, and monkeypatched model construction raises if touched while llm.enabled is false.

~~~python
def test_llm_disabled_uses_rule_summary_without_client(config_file, monkeypatch):
    monkeypatch.setattr("oa_knowledge.pending_summary.create_llm_client",
                        lambda *_: (_ for _ in ()).throw(AssertionError("model called")))
    version = summarize_pending(settings, engine, logical_item_id)
    assert version.provider_name == "deterministic-fallback"
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_scheduled_sync.py tests/test_pending_summary.py tests/test_e2e_autorun.py -q`
Expected: FAIL on baseline enqueue, v1 task key, or model initialization.

- [ ] **Step 3: Implement minimal behavior**

Gate model creation before provider construction, build fallback from title/sender/current_node/deadline, change Pending task key to v2, and make scheduled_bootstrap use notification_mode disabled.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_scheduled_sync.py tests/test_pending_summary.py tests/test_e2e_autorun.py -q`
Expected: PASS; same hash produces one delivery path.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/scheduled_sync.py src/oa_knowledge/pending_sync.py src/oa_knowledge/pending_summary.py src/oa_knowledge/web/worker.py tests/test_scheduled_sync.py tests/test_pending_summary.py tests/test_e2e_autorun.py
git commit -m "fix: make pending baseline and summary deterministic"
~~~

### Task 2: 显式投递与清理恢复

**Files:**
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/pending_cleanup.py`
- Modify: `src/oa_knowledge/web/console_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_pending_cleanup.py`
- Test: `tests/test_console_views.py`

**Interfaces:**
- Produces Worker handler: `_pipeline_pending_cleanup(task: PipelineTask) -> None`
- Preserves delivery key: `feishu:pending:{logical_item_id}:{version.input_hash}`
- Reuses: `perform_cleanup(...)` and ItemOccurrence cleanup fields

- [ ] **Step 1: Write failing crash-recovery tests**

Cover sent delivery before Worker crash, cleanup failure, retry cleanup, already-cleaned idempotence, explicit failed delivery retry, and unknown_outcome returning 409.

~~~python
def test_sent_delivery_advances_to_cleanup_without_resend(worker, sent_delivery, send_spy):
    worker._pipeline_notify_feishu(task)
    assert task.stage == "pending_cleanup"
    assert send_spy.call_count == 0
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_worker.py tests/test_pending_cleanup.py tests/test_console_views.py -q`
Expected: FAIL because notify currently completes after swallowing cleanup failure.

- [ ] **Step 3: Implement the cleanup stage**

Make notify_feishu upsert/read NotificationDelivery, advance sent outcomes to pending_cleanup, and implement the handler by calling perform_cleanup in its own transaction. Reuse existing retry endpoints; enforce 409 for cleaned sync and unknown delivery retry.

- [ ] **Step 4: Run focused and E2E tests**

Run: `uv run pytest tests/test_worker.py tests/test_pending_cleanup.py tests/test_console_views.py tests/test_e2e_autorun.py -q`
Expected: PASS; send spy remains one across crash/retry scenarios.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/web/worker.py src/oa_knowledge/pending_cleanup.py src/oa_knowledge/web/console_views.py src/oa_knowledge/web/app.py tests/test_worker.py tests/test_pending_cleanup.py tests/test_console_views.py tests/test_e2e_autorun.py
git commit -m "fix: make pending delivery cleanup resumable"
~~~
