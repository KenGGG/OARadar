# Audit Repair Pipelines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn online audit into concurrent attachment repair and Markdown conversion pipelines with independent processes, controls, progress, and logs.

**Architecture:** Keep the existing OA-only operation worker responsible for scanning and safe attachment repair. Add a dedicated durable Markdown task table, control row, service module, CLI worker, and systemd unit; audit transactions enqueue verified attachments idempotently while the Markdown worker converts local files independently.

**Tech Stack:** Python 3.12, SQLAlchemy/Alembic, Typer, FastAPI, React/TypeScript, pytest, systemd user services.

## Global Constraints

- OA access remains read-only and local-only.
- Never delete local extras or expose confidential content in logs.
- Store OA identifiers as text and paths relative to configured data roots.
- Scan and Markdown conversion must pause and resume independently.

---

### Task 1: Durable Markdown queue and controls

**Files:** create migration `0023_markdown_queue.py`; modify models; create `markdown_queue.py`; test `test_markdown_queue.py`.

- [ ] Write failing tests for idempotent enqueue, successful-export skipping, atomic claim, independent pause, retry, and lease recovery.
- [ ] Run the focused tests and confirm failure because queue interfaces do not exist.
- [ ] Add task/control/event models, migration, and queue service with stable status transitions and sanitized errors.
- [ ] Run focused tests and confirm pass.

### Task 2: Dedicated Markdown worker and CLI

**Files:** create `markdown_worker.py`; modify `cli.py` and Markdown conversion service; test worker and CLI tests.

- [ ] Write failing tests proving the worker consumes only Markdown tasks, converts one verified local source, records failure without stopping, and obeys pause.
- [ ] Confirm red tests.
- [ ] Expose one-file conversion and implement `oa markdown-worker` loop with heartbeat/runtime state.
- [ ] Confirm green tests.

### Task 3: Audit discovery and repair integration

**Files:** modify `online_audit.py`, `web/worker.py`; extend online-audit and worker tests.

- [ ] Write failing tests showing verified non-converted attachments are queued after each observation and repaired downloads are queued after commit.
- [ ] Confirm red tests.
- [ ] Enqueue Markdown gaps idempotently without waiting for conversion and record repair counts/errors separately.
- [ ] Confirm green tests.

### Task 4: Independent API controls and combined view

**Files:** modify `web/app.py`, audit view/service; extend `test_web.py`.

- [ ] Write failing API tests for Markdown queue status, pause, resume, retry-failed, progress, and separate event logs.
- [ ] Confirm red tests.
- [ ] Add endpoints and combined audit response while preserving item pagination.
- [ ] Confirm green tests.

### Task 5: WebUI dual pipeline controls

**Files:** modify `webui/src/App.tsx` and `styles.css`.

- [ ] Add typed Markdown pipeline data and independent buttons, progress metrics, worker state, and logs.
- [ ] Preserve silent polling, current page, and scroll position.
- [ ] Run TypeScript check and production build.

### Task 6: Deployment, docs, and live verification

**Files:** modify README files/config example as needed; create user systemd Markdown worker unit outside repository.

- [ ] Run database migration only after pausing/restarting affected services safely.
- [ ] Install/enable/start the dedicated Markdown worker and restart Web/scan worker with current code.
- [ ] Run focused and full relevant tests, public-release check, frontend build, and `git diff --check`.
- [ ] Verify both processes are active, both API progress streams advance, and pausing one does not stop the other.
