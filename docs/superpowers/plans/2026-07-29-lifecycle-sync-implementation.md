# OA Lifecycle Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Pending reflect the current OA snapshot, reconcile Pending into Done, expose full-dataset Done metrics, and add three-page incremental plus manual full synchronization controls.

**Architecture:** Treat a successful Pending discovery as an authoritative snapshot and close missing active occurrences transactionally. Keep the Done manifest authoritative for total counts, connect manifest discovery to strict lifecycle reconciliation and priority queues, and expose server-side pagination/statistics and durable synchronization jobs to the React UI.

**Tech Stack:** Python 3.11, SQLAlchemy, SQLite, FastAPI, Typer, Playwright, React, TypeScript, Vite, pytest.

## Global Constraints

- OA access is read-only and all OA content remains local under `data_root`.
- Tests use only synthetic or irreversibly redacted fixtures.
- OA identifiers remain text and archive paths remain relative to `data_root`.
- Attachment traversal remains bounded at depth 10.
- Normal Done synchronization scans exactly the newest three pages; full synchronization is user-triggered.

---

### Task 1: Authoritative Pending Snapshot

**Files:**
- Modify: `src/oa_knowledge/pending_sync.py`
- Modify: `src/oa_knowledge/cli.py`
- Test: `tests/test_pending_sync.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Extend `sync_pending_discovery(session, items, *, authoritative=True)` so a successful authoritative call closes active Pending occurrences absent from `items` and reactivates rediscovered occurrences.
- Extend `PendingSyncResult` with `closed` and `reactivated` counts without changing existing created/updated/unchanged semantics.

- [ ] Write synthetic tests proving a 33-to-5 refresh closes 28 rows, rediscovery reactivates a row, and failed discovery never calls persistence.
- [ ] Run the focused tests and confirm failures are caused by missing snapshot behavior.
- [ ] Implement transactional closing/reactivation and include the new counters in CLI output.
- [ ] Run focused tests and commit the independently working Pending fix.

### Task 2: Pending Markdown and Summary Completion

**Files:**
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/production_pipeline.py`
- Test: `tests/test_production_pipeline.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Pending `detail_sync` advances to parsing of captured source files before `pending_summary`.
- `pending_summary` completes when notification is disabled; if notification is enabled, use a supported notification handler or complete with a durable `notification_skipped` event rather than an unknown stage.

- [ ] Write tests that exercise the Pending stage sequence and prove no task reaches an unsupported stage.
- [ ] Verify the tests fail at the current missing stage/ordering.
- [ ] Add the minimum supported stage transitions, reusing the existing parse pipeline and Pending summary generator.
- [ ] Run focused tests and commit.

### Task 3: Pending-to-Done Reconciliation

**Files:**
- Modify: `src/oa_knowledge/reconcile.py`
- Modify: `src/oa_knowledge/full_manifest.py`
- Modify: `src/oa_knowledge/production_pipeline.py`
- Test: `tests/test_reconcile.py`
- Test: `tests/test_full_manifest.py`

**Interfaces:**
- On Done upsert, call strict reconciliation with `affair_id_text`, `summary_id_text`, and `process_id_text` when the Done detail provides them.
- Exact matches close Pending and enqueue idempotent `realtime_done` work; incomplete/unmatched identities create one deduplicated review entry.

- [ ] Write tests for exact match, incomplete identity, conflicting identity, idempotent repeat, and `realtime_done` priority.
- [ ] Run tests to observe the missing production wiring.
- [ ] Wire reconciliation into Done discovery/detail persistence and enqueue final-version processing.
- [ ] Run focused tests and commit.

### Task 4: Full-Dataset Done API and Synchronization Controls

**Files:**
- Modify: `src/oa_knowledge/web/lifecycle_views.py`
- Modify: `src/oa_knowledge/web/status.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- `GET /api/lifecycle/done?page=&page_size=&query=&status=` returns `items`, `total`, `page`, `page_size`, and `metrics` containing `oa_done_total`, `downloaded_items`, `verified_attachments`.
- Add an idempotent endpoint for three-page incremental Done refresh and retain the existing single-instance full-manifest endpoint for manual full reconciliation.

- [ ] Write API tests using more than 100 synthetic rows and mixed file roles/statuses.
- [ ] Verify current API fails full-dataset metric and pagination expectations.
- [ ] Implement aggregate queries, pagination/search, and durable incremental job creation fixed to three pages.
- [ ] Run focused tests and commit.

### Task 5: WebUI Done Dashboard

**Files:**
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/types.ts`
- Modify: `webui/src/styles.css`
- Test: existing frontend build/type-check

**Interfaces:**
- Consume the paginated Done payload and metrics from Task 4.
- Provide “增量刷新” and “全量核对” buttons with disabled/running/error states and page navigation.

- [ ] Update TypeScript contracts first so the current component fails type-check.
- [ ] Replace recent-100 metrics with the three full-dataset metrics and add controls/pagination.
- [ ] Build the frontend and fix only errors related to this change.
- [ ] Commit the WebUI change.

### Task 6: End-to-End Verification and Local Refresh

**Files:**
- Modify only tests or implementation needed to correct verified defects.

- [ ] Run the full Python test suite and frontend production build.
- [ ] Inspect `git diff` and secret/data ignore rules; verify no OA content, configuration, profiles, downloads, databases, or logs are tracked.
- [ ] Run one local read-only Pending refresh and one three-page Done refresh through the shared browser resource path.
- [ ] Confirm WebUI/API Pending count matches OA, inspect current Pending attachment paths and file size/SHA verification, and report any OA/authentication-dependent failures precisely.
- [ ] Commit final corrections and document observed local counts without committing confidential runtime data.
