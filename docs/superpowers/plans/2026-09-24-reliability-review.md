# OARadar Reliability Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make scheduled OA processing complete, recoverable, and accurately visible without leaking OA content.

**Architecture:** Keep the existing SQLite queues and local workers. Repair evidence handoffs and restart semantics at their current boundaries; use item-scoped recovery and file checks outside frequent status refreshes.

**Tech Stack:** Python, SQLAlchemy, Playwright, FastAPI, React, pytest, systemd user services.

**Scope update:** The user explicitly deferred backups on 2026-09-24. No database, private-configuration, or originals backup is to be made in this change. Live OA/Feishu acceptance remains distinct from synthetic test results.

**Spec:** User-provided review attached to this thread, based on `4cf44ee`.

## Global Constraints

- Work on `main` as explicitly requested earlier by the user.
- OA content stays confidential and local; OA integration remains read-only.
- Excluded items are not processed. Identifiers remain text and archive paths relative to `data_root`.
- Container traversal reaches depth 10; further children enqueue `depth_limit_reached`.

## Review Focus

- A parsed attachment exists only under `cache_root`: pending summary must include it.
- An active parse artifact is missing or unreadable: pending reminder must disclose incomplete evidence.
- A parse task failed before summary: a bounded retry must not suppress the basic reminder indefinitely.
- A Feishu delivery remains `sending` after restart: recovery must not send twice.
- A classified item's evidence changes: automatic classification refreshes only that item, except when manually locked.

---

### Task 1: Pending evidence and degraded reminder

**Files:** `src/oa_knowledge/pending_summary.py`, `src/oa_knowledge/web/worker.py`, `src/oa_knowledge/pipeline.py`, `tests/test_pending_summary.py`, `tests/test_worker.py`.

**Interfaces:** Parse artifacts use `resolve_cache_path(settings, output_relpath)`. `summarize_pending` consumes a snapshot plus readable attachment text and produces a `SummaryVersion`.

- [ ] Add a synthetic test proving a cache-root attachment enters the summary input and a missing artifact produces an explicit missing-evidence marker. Run it red.
- [ ] Resolve paths through `resolve_cache_path` and include the marker in the model and deterministic fallback input. Run it green.
- [ ] Add a worker test proving a failed parse is retried only within a bound, then advances to `pending_summary`. Run it red, implement, and run it green.
- [ ] Run pending summary, worker, and end-to-end autorun tests; commit the focused change.

### Task 2: Classification and Markdown recovery

**Files:** `src/oa_knowledge/web/worker.py`, `src/oa_knowledge/done_convergence.py`, `src/oa_knowledge/classification/service.py`, `src/oa_knowledge/web/delivery_facts.py`, corresponding tests.

**Interfaces:** `needs_review` stops publication but is recoverable; an item-scoped classified decision re-enters `classify`/publish/index without downloading again.

- [ ] Add a test for review → classified → item-scoped continuation and another for missing Markdown output; run red.
- [ ] Separate excluded from review outcomes, repair convergence idempotency on changed classification evidence, and add lightweight output existence checks at convergence. Run green.
- [ ] Add a changed-input fingerprint test for classified decisions while preserving manual locks. Run red and green.
- [ ] Run classification, convergence, and Markdown delivery tests; commit.

### Task 3: Feishu interruption recovery

**Files:** `src/oa_knowledge/web/worker.py`, `src/oa_knowledge/notifications/feishu_service.py`, `tests/test_worker.py`, `tests/test_feishu_service.py`.

**Interfaces:** A durable `NotificationDelivery` with status `sending` is an unknown outcome after worker restart and must not trigger another POST.

- [ ] Add a synthetic interrupted-send test; run red.
- [ ] Map stale `sending` to the existing manual-check state and park its queue task; run green.
- [ ] Verify confirmed `sent`, explicit retryable rejection, and unknown outcome retain their existing behavior; commit.

### Task 4: Classification rules and UI

**Files:** `src/oa_knowledge/classification/per_item_classifier.py`, `webui/src/App.tsx`, relevant UI components and tests.

**Interfaces:** One business primary category plus auxiliary process tags; external files remain grouped by actual issuer. UI exposes original, Markdown, and classification status separately.

- [ ] Add synthetic rule tests for financing report, pension stamping, lease inspection, and union activity; run red.
- [ ] Prioritize specific subject evidence and limit `09_对外报送与监管反馈` to reporting-centric work; run green.
- [ ] Add compact per-item status, classification filters/edit action, rendered Markdown with source toggle, and quieter refresh. Verify UI behavior and build; commit.

### Task 5: CI, runtime, and backup acceptance

**Files:** `.github/workflows/ci.yml`, affected tests, backup configuration/scripts, runtime reports.

- [ ] Reproduce all current CI failures; fix import, help-text, and download-test issues without removing recovery assertions.
- [ ] Run the full synthetic Python suite, UI tests/build, and the eight user-specified acceptance scenarios.
- [ ] Verify deployed timers/workers and OA read-only runtime behavior; check database, private configuration, and originals backup coverage with a recoverability test.
- [ ] Inspect staged files for OA content/secrets and verify final clean `main`; commit and report exact remaining risks.
