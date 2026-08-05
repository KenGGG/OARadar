# Initiation-Date Archive Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile every Done-item raw archive and Markdown mirror to the OA initiation month, with resumable WebUI progress and safe handling of unknown dates.

**Architecture:** Persist the canonical initiation timestamp on `OAItem`, collect it from the Done list, and centralize target-path selection in an archive-date module. A persistent reconciliation run plans and migrates one item at a time, atomically updating filesystem artifacts and every database path reference; the WebUI controls the run through the existing local worker.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, FastAPI, React/TypeScript, pytest, SQLite, systemd user services.

## Global Constraints

- OA access is read-only and all OA content remains local-only.
- Archive dates use `Asia/Shanghai` and never fall back to completion/current time.
- Unknown initiation dates are stored under `raw/done/unknown/`.
- Existing Markdown content is moved and path metadata rewritten, not reconverted.
- Every migration operation is idempotent and validates paths before mutation.

---

### Task 1: Canonical initiation date and future archive placement

**Files:**
- Modify: `src/oa_knowledge/db/models.py`
- Create: `src/oa_knowledge/db/migrations/versions/0026_initiation_archive_reconciliation.py`
- Modify: `src/oa_knowledge/collector/done.py`
- Modify: `src/oa_knowledge/full_manifest.py`
- Modify: `src/oa_knowledge/detail_archive.py`
- Test: `tests/test_detail_archive.py`
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: `OAItem.initiated_at: datetime | None`
- Produces: `done_archive_directory(title, workitem_id_text, initiated_at) -> PurePosixPath`

- [ ] Write tests proving initiation month selection, Shanghai month boundaries, and `unknown` placement.
- [ ] Run focused tests and observe failures caused by the missing field/rule.
- [ ] Add migration/model field and propagate Done-list `created_at` into `OAItem.initiated_at`.
- [ ] Replace completion/current-time path selection with the centralized helper.
- [ ] Run focused tests and commit the independently working rule change.

### Task 2: Safe, idempotent reconciliation engine

**Files:**
- Create: `src/oa_knowledge/archive_reconciliation.py`
- Modify: `src/oa_knowledge/db/models.py`
- Modify: `src/oa_knowledge/db/migrations/versions/0026_initiation_archive_reconciliation.py`
- Test: `tests/test_archive_reconciliation.py`

**Interfaces:**
- Produces: `plan_reconciliation(session, settings) -> ReconciliationSummary`
- Produces: `reconcile_one(session, settings, item_id) -> ReconciliationResult`
- Produces persistent run/item statuses for pause, resume, retry, and audit display.

- [ ] Write synthetic tests for correct placement, raw/Markdown/assets movement, JSON and DB path rewrites, conflict quarantine, idempotence, unknown dates, and rollback.
- [ ] Run tests and confirm the reconciliation interfaces are absent/failing.
- [ ] Implement read-only planning with path containment and collision checks.
- [ ] Implement per-item staging, validation, atomic publication, and database reference updates.
- [ ] Run focused tests and commit the reconciliation engine.

### Task 3: Online date refresh and worker controls

**Files:**
- Modify: `src/oa_knowledge/online_audit.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `src/oa_knowledge/web/status.py`
- Test: `tests/test_online_audit.py`
- Test: `tests/test_web.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Produces API endpoints under `/api/audits/archive-dates` for start, pause, resume, retry, and status.
- Consumes reconciliation planner/executor from Task 2.

- [ ] Write failing API/worker tests for date refresh, independent pause/resume, progress, and sanitized errors.
- [ ] Extend the read-only Done scan to persist initiation dates and enqueue reconciliation items.
- [ ] Add worker dispatch and resilient per-item execution.
- [ ] Run focused tests and commit worker/API behavior.

### Task 4: WebUI progress panel and documentation

**Files:**
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/styles.css`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Consumes `/api/audits/archive-dates` status/control endpoints.

- [ ] Add the archive-date reconciliation panel and controls without resetting table pagination or scroll state.
- [ ] Show scanned, dated, unknown, correct, pending, running, succeeded, failed, and sanitized event logs.
- [ ] Document date semantics, `unknown`, recovery, and local-only behavior.
- [ ] Build the WebUI, run frontend validation, and commit UI/docs.

### Task 5: Production migration and verification

**Files:**
- Runtime database and files under configured `data_root` only; never commit them.

**Interfaces:**
- Consumes all prior tasks.

- [ ] Back up the SQLite database and generate a dry-run reconciliation summary.
- [ ] Pause audit and Markdown queues, upgrade schema, then start the online date refresh.
- [ ] Execute the planned migration with live progress visible in WebUI.
- [ ] Verify the reported case resolves to `2022/04`.
- [ ] Verify all dated items, raw/Markdown mirrors, hashes, JSON references, and database paths; report unknown/conflict counts.
- [ ] Run the full test suite, production frontend build, `git diff --check`, and confirm all services are active.
