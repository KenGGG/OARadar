# Online Done Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable, pausable, read-only online audit with per-item counts, timings, errors, logs, APIs, and WebUI.

**Architecture:** Three audit ledger tables are controlled by a focused service. The existing single durable worker invokes a read-only browser runner. FastAPI exposes control/read endpoints and React renders a polling audit page.

**Tech Stack:** Python 3.12, SQLAlchemy, Alembic, FastAPI, Playwright, React, TypeScript, Vite, pytest.

## Global Constraints

- OA access is read-only; never approve, reply, delete, forward, or alter OA records.
- Never store credentials, cookies, tokens, complete OA responses, or machine-sensitive paths in audit events.
- Audit must not download/write archive files, trigger conversion, or change manifest processing states.
- One active or paused online audit is allowed at a time.

---

### Task 1: Durable audit ledger and service

**Files:** create `src/oa_knowledge/online_audit.py`, create migration `0022_online_audit.py`, modify `db/models.py`, test `tests/test_online_audit.py`.

**Interfaces:** produce `start_audit`, `pause_audit`, `resume_audit`, `audit_view`, and `execute_audit`.

- [ ] Write failing service tests for creation, uniqueness, counts, pause/resume, timing and sanitized failures.
- [ ] Run `uv run pytest tests/test_online_audit.py -q` and confirm missing interfaces fail.
- [ ] Add models/migration and minimal service implementation.
- [ ] Re-run the focused tests and confirm pass.

### Task 2: Worker and API integration

**Files:** modify `web/worker.py`, `web/app.py`; test `tests/test_web.py`, `tests/test_worker.py`.

**Interfaces:** endpoints `GET/POST /api/audits/online`, `/pause`, `/resume`; worker job type `online_audit`.

- [ ] Write failing API and worker-route tests.
- [ ] Run focused tests and confirm contract failures.
- [ ] Add routes and dispatch to `execute_audit`.
- [ ] Re-run focused tests and confirm pass.

### Task 3: Audit WebUI and documentation

**Files:** modify `webui/src/App.tsx`, `webui/src/styles.css`, `README.md`, `README.zh-CN.md`.

**Interfaces:** Audit view polls the read API and invokes CSRF-protected control endpoints.

- [ ] Add the typed audit payload, navigation, controls, metrics, item table, error summary and event log.
- [ ] Run `npm run check` and correct all type errors.
- [ ] Build static assets with `npm run build`.
- [ ] Document the online-only, read-only behavior and pause semantics.

### Task 4: Verification and local handoff

**Files:** no new production interfaces.

- [ ] Run focused audit tests, then `uv run pytest`.
- [ ] Run `npm run check`, `npm run build`, public-release check and `git diff --check`.
- [ ] Upgrade the local database, restart WebUI, and verify the audit endpoint without starting a real audit.
