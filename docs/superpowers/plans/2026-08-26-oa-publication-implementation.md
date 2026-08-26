# Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote an approved immutable Markdown candidate through one atomic `current` pointer, with deterministic crash recovery, repeatable rollback, permanent `.previous/` retention, and guarded WebUI deletion.

**Architecture:** `PublicationService` is the only writer of release state. It validates and moves an approved candidate into `.versions/<release_id>`, records a durable operation intent, atomically replaces a single `current` symlink, then archives the former successful release in `.previous/`. Stable `internal` and `external` symlinks always resolve through `current`. Startup recovery replays the operation ledger and chooses a complete old or new release; formal publication remains disabled until fault injection passes every checkpoint.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, pathlib/os atomic filesystem operations, FastAPI, React 19, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-oa-markdown-v1-classification-design.md`

## Global Constraints

- Entry requires explicit approval of one exact Phase 2 build ID and QA report hash.
- `.previous/` retains every formerly successful formal release permanently by default; no automatic cleanup exists.
- Deletion is WebUI-only, explicitly confirmed, and limited to derived Markdown. It never affects originals, classification history, manual decisions, or parse cache.
- The active release and the selected rollback target cannot be deleted.
- Rollback only occurs through `PublicationService`; the release being replaced enters `.previous/` and remains recoverable.
- External readers must never be left with mixed internal/external generations after crash or restart.

---

### Task 1: Add migration 0040 and durable publication operations

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0040_oa_markdown_v1_publication.py`
- Modify: `src/oa_knowledge/db/models.py`
- Create: `tests/test_publication_migration.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing migration tests**

Assert `publication_releases` and `publication_operations` upgrade/downgrade cleanly. Release rows include immutable build ID, manifest/QA hashes, relative location, lifecycle `staged|active|previous|deleted`, and successful-publication timestamp. Operation rows include type `publish|rollback|delete`, old/new targets, checkpoint, actor, confirmation token hash, error, recovery state, and timestamps.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_publication_migration.py tests/test_database.py -q --no-cov`

- [ ] **Step 3: Implement constraints**

Enforce at most one active release and at most one running mutation operation. Store all identifiers and relative paths as text. A deleted release retains its audit row. A release cannot become active unless its build QA status/hash is approved.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_publication_migration.py tests/test_database.py -q --no-cov`

```bash
git add src/oa_knowledge/db/migrations/versions/0040_oa_markdown_v1_publication.py src/oa_knowledge/db/models.py tests/test_publication_migration.py tests/test_database.py
git commit -m "feat: add OA markdown publication ledger"
```

### Task 2: Establish immutable release layout and one formal pointer

**Files:**
- Create: `src/oa_knowledge/publication/__init__.py`
- Create: `src/oa_knowledge/publication/layout.py`
- Create: `tests/test_publication_layout.py`

- [ ] **Step 1: Write failing layout tests**

Cover:

```text
data/markdown/.builds/<build_id>/
data/markdown/.versions/<release_id>/
data/markdown/.previous/<release_id>/
data/markdown/current -> .versions/<release_id> or .previous/<release_id>
data/markdown/internal -> current/internal
data/markdown/external -> current/external
```

Assert relative symlinks, safe roots, same-filesystem rename precondition, non-following validation, and rejection of absolute/traversing/unexpected targets. If `internal`, `external`, or `current` already exists in an unrecognized legacy form, initialization must abort without replacing or deleting it and report an operator migration requirement.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_publication_layout.py -q --no-cov`

- [ ] **Step 3: Implement safe layout primitives**

Expose pure validation plus narrowly scoped helpers for creating a temporary relative symlink and replacing `current` with `os.replace`. Creation of stable `internal`/`external` symlinks is idempotent, and they always target `current/...`; no operation replaces them separately.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_publication_layout.py -q --no-cov`

```bash
git add src/oa_knowledge/publication tests/test_publication_layout.py
git commit -m "feat: establish atomic OA release layout"
```

### Task 3: Implement the publication state machine

**Files:**
- Create: `src/oa_knowledge/publication/service.py`
- Create: `tests/test_publication_service.py`

- [ ] **Step 1: Write failing happy-path and guard tests**

Reject unapproved/failed/stale builds, count drift, modified build contents, an existing untracked destination, concurrent operations, and insufficient disk space. Prove the candidate is revalidated immediately before promotion and `current` changes exactly once.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_publication_service.py -q --no-cov`

- [ ] **Step 3: Implement explicit checkpoints**

Expose:

```python
class PublicationService:
    def publish(self, command: PublishCommand) -> PublicationResult: ...
```

Persist and fsync these checkpoints:

1. `intent_recorded`
2. `candidate_revalidated`
3. `release_materialized`
4. `current_next_fsynced`
5. `current_replaced`
6. `old_release_archived`
7. `ledger_committed`

Materialize the immutable release by same-filesystem rename only after validation. Create `current.next`, fsync the link and parent directory as supported, and atomically `os.replace` it over `current`. The DB operation ledger must contain enough old/new target data for recovery before filesystem mutation.

- [ ] **Step 4: Verify external-reader consistency**

A reader resolving `internal` and `external` before or after the one `current` replacement sees one release. Tests repeatedly resolve and read release marker hashes during publication; no observed pair may come from different release IDs.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_publication_service.py -q --no-cov`

```bash
git add src/oa_knowledge/publication/service.py tests/test_publication_service.py
git commit -m "feat: publish OA markdown through one atomic pointer"
```

### Task 4: Recover automatically from every interrupted checkpoint

**Files:**
- Create: `src/oa_knowledge/publication/recovery.py`
- Modify: `src/oa_knowledge/publication/service.py`
- Create: `tests/test_publication_recovery.py`
- Create: `tests/test_publication_fault_injection.py`

- [ ] **Step 1: Build a parameterized failure matrix**

Inject process failure immediately before and after every checkpoint and every rename/fsync boundary. Reopen a fresh DB session and service instance to simulate restart. Cover first publication, subsequent publication, and failure while archiving the old release.

- [ ] **Step 2: Define the only acceptable post-recovery outcomes**

After `recover()`:

- `current`, DB active release, release marker, and both stable views identify the complete old release; or
- all identify the complete new release.

No mixed, missing, unrecognized, or double-active result is allowed. Repeated recovery is idempotent.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_publication_recovery.py tests/test_publication_fault_injection.py -q --no-cov`

- [ ] **Step 4: Implement recovery before enabling startup publication**

Expose:

```python
class PublicationRecoveryService:
    def recover(self) -> RecoveryReport: ...
```

Inspect the ledger, `current`, release markers, and directory hashes without trusting any one source alone. Complete or compensate a known operation deterministically. Unknown filesystem state disables publication and reports manual intervention; it must never guess or delete data.

- [ ] **Step 5: Pass the Publication atomicity Gate**

Run the failure matrix repeatedly on the same filesystem type used by production:

```bash
uv run pytest tests/test_publication_recovery.py tests/test_publication_fault_injection.py -q --no-cov --count=20
```

If `pytest-repeat` is not a project dependency, run an equivalent checked shell loop without adding a runtime dependency. Expected: every iteration passes. Until this does, keep all publish endpoints and controls disabled.

- [ ] **Step 6: Commit**

```bash
git add src/oa_knowledge/publication/recovery.py src/oa_knowledge/publication/service.py tests/test_publication_recovery.py tests/test_publication_fault_injection.py
git commit -m "feat: recover interrupted OA publications"
```

### Task 5: Implement repeatable rollback through PublicationService

**Files:**
- Modify: `src/oa_knowledge/publication/service.py`
- Modify: `src/oa_knowledge/publication/recovery.py`
- Create: `tests/test_publication_rollback.py`

- [ ] **Step 1: Write failing rollback tests**

Rollback requires an existing successful release, exact target ID/hash, explicit actor/reason/confirmation, and full validation. Assert the pre-rollback active release moves to `.previous/`, the target becomes active, and a second rollback can restore the former version. Fault-inject rollback at all publication checkpoints.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_publication_rollback.py -q --no-cov`

- [ ] **Step 3: Reuse the same state machine**

Expose `PublicationService.rollback(command: RollbackCommand)`. Do not implement a separate symlink shortcut. The operation differs only in source release location and audit type; validation, atomic pointer replacement, archive handling, and recovery are shared.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_publication_rollback.py tests/test_publication_recovery.py tests/test_publication_fault_injection.py -q --no-cov`

```bash
git add src/oa_knowledge/publication/service.py src/oa_knowledge/publication/recovery.py tests/test_publication_rollback.py
git commit -m "feat: add recoverable OA markdown rollback"
```

### Task 6: Guard explicit deletion of previous derived releases

**Files:**
- Modify: `src/oa_knowledge/publication/service.py`
- Create: `tests/test_publication_deletion.py`

- [ ] **Step 1: Write failing deletion tests**

Reject automatic/age-based cleanup, active release deletion, configured rollback-target deletion, unknown paths, symlinks, paths outside `.previous/`, missing confirmation, and release hash mismatch. Prove originals, DB classification/manual history, and ParseArtifact rows are unchanged.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_publication_deletion.py -q --no-cov`

- [ ] **Step 3: Implement delete as an audited PublicationService operation**

Expose `PublicationService.delete_previous(command: DeletePreviousCommand)`. Resolve the exact ledger-owned release directory without following symlinks, rename it to an operation-specific tombstone inside `data/markdown/`, fsync, delete only that verified tree, and retain the release/operation audit rows as `deleted`. A crash resumes or compensates the known tombstone.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_publication_deletion.py tests/test_publication_recovery.py -q --no-cov`

```bash
git add src/oa_knowledge/publication/service.py tests/test_publication_deletion.py
git commit -m "feat: guard deletion of previous markdown releases"
```

### Task 7: Expose publication, rollback, and deletion controls in WebUI

**Files:**
- Create: `src/oa_knowledge/web/publication_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `webui/src/views/MarkdownBuildView.tsx`
- Modify: `webui/src/types/simple-status.ts`
- Create: `tests/test_publication_views.py`
- Modify: `tests/test_web_security.py`
- Create: `tests/test_frontend_publication_assets.py`

- [ ] **Step 1: Write failing API/security tests**

Specify status/history endpoints and mutation endpoints for publish, rollback, select rollback target, and delete previous. Require CSRF, local authorization, a short-lived server-issued confirmation challenge bound to operation/target/hash, and exact user-entered release suffix. Endpoints return operation IDs and never perform filesystem mutation inside the request thread.

- [ ] **Step 2: Write failing frontend contract tests**

Show active release, approved candidate, permanent history, protected rollback target, operation/recovery state, QA hash, and explicit danger confirmations. Hide/disable publication until the server reports the atomicity Gate passed. There is no bulk delete or retention-days setting.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_publication_views.py tests/test_web_security.py tests/test_frontend_publication_assets.py tests/test_worker.py -q --no-cov`

- [ ] **Step 4: Implement queued operations and startup recovery**

Run `recover()` before the worker accepts publication jobs. Serialize all publication mutations. Surface recovery-required state prominently and refuse new mutation until resolved.

- [ ] **Step 5: Type-check, build, and test**

Run:

```bash
cd webui && npm run check && npm run build
cd .. && uv run pytest tests/test_publication_views.py tests/test_web_security.py tests/test_frontend_publication_assets.py tests/test_worker.py -q --no-cov
```

- [ ] **Step 6: Commit source only**

```bash
git add src/oa_knowledge/web/publication_views.py src/oa_knowledge/web/app.py src/oa_knowledge/web/worker.py webui/src tests/test_publication_views.py tests/test_web_security.py tests/test_frontend_publication_assets.py tests/test_worker.py
git commit -m "feat: manage OA markdown releases in WebUI"
```

### Task 8: Operational rehearsal and first formal switch

**Files:**
- Create: `docs/runbooks/oa-markdown-v1-publication.md`

- [ ] **Step 1: Write the runbook before touching production state**

Document backup, disk-space check, exact approved build/hash, recovery preflight, atomicity Gate evidence, publish confirmation, external-reader verification, rollback, service restart, and guarded deletion. State that `.previous/` is permanent by default.

- [ ] **Step 2: Run the full automated Phase 3 suite**

Run:

```bash
uv run pytest tests/test_publication_migration.py tests/test_publication_layout.py tests/test_publication_service.py tests/test_publication_recovery.py tests/test_publication_fault_injection.py tests/test_publication_rollback.py tests/test_publication_deletion.py tests/test_publication_views.py tests/test_web_security.py tests/test_worker.py tests/test_frontend_publication_assets.py -q --no-cov
cd webui && npm run check && npm run build
```

- [ ] **Step 3: Rehearse publish and rollback on a production-filesystem clone**

Use synthetic or copied derived Markdown only—never copied OA originals in a tracked location. Kill the process at every checkpoint, restart, run recovery, and verify both stable views always share the release marker. Retain the local Gate report outside Git.

- [ ] **Step 4: Obtain explicit final confirmation**

Present approved build ID, QA hash, reconciled counts, active old release, rollback target, disk use, and atomicity Gate result. Do not switch `current` until the user confirms this exact release.

- [ ] **Step 5: Publish and verify**

Publish through `PublicationService`, restart the web/worker service once, and verify recovery is a no-op; `current`, DB active release, `internal`, and `external` agree; the prepared `_index.md` count matches the approved publishable count; and `.previous/` contains only previously successful releases.

- [ ] **Step 6: Commit the runbook**

```bash
git add docs/runbooks/oa-markdown-v1-publication.md
git commit -m "docs: add OA markdown publication runbook"
```

## Phase 3 Exit Gate

- [ ] Every publication and rollback fault-injection point recovers to a complete old or new release.
- [ ] One atomic `current` replacement is the only visibility boundary.
- [ ] Active and rollback-target releases are deletion-protected.
- [ ] `.previous/` has no automatic cleanup and retains formally successful releases.
- [ ] Deletion affects only an explicitly selected derived Markdown release.
- [ ] The first formal switch used the exact user-approved build and QA hash.
- [ ] Post-switch restart and recovery verification pass with no mixed state.
