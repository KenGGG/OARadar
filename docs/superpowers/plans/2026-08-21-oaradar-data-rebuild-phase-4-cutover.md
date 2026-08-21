# OARadar Data Rebuild Phase 4 Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the rebuilt archive and Markdown library complete, copy current operational state safely, switch directories only with explicit authorization, and preserve a tested rollback path.

**Architecture:** A read-only validator produces a local redacted acceptance report. A cutover command defaults to dry-run, requires all gates plus an explicit flag, stops only known user units, uses same-filesystem renames, and automatically restores the legacy directory if post-switch smoke fails. Permanent deletion is intentionally absent.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite backup API, Typer, systemd user units, pytest, shell smoke scripts.

**Spec:** `docs/superpowers/specs/2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md`

## Global Constraints

- Do not call OA, Feishu, LLM, or MinerU during validation or cutover smoke.
- Do not print titles, document numbers, attachment names, or paths in reports.
- Cutover requires a clean Git worktree, all five known user units discovered, a current external backup, and explicit user authorization.
- Never use recursive deletion in the cutover implementation.
- The rollback renames directories back; it never deletes V2 or legacy business data.
- Permanent legacy deletion, Git tag push, and branch deletion remain outside this plan.

---

### Task 1: Validate complete rebuilt evidence and knowledge output

**Files:**
- Create: `src/oa_knowledge/rebuild/validation.py`
- Test: `tests/test_rebuild_validation.py`

**Interfaces:**
- Produces: `ValidationCheck(code: str, ok: bool, expected: int | None, actual: int | None)`
- Produces: `validate_rebuild(session: Session, settings: Settings, run_id: int) -> list[ValidationCheck]`
- Produces: `validation_passed(checks: Sequence[ValidationCheck]) -> bool`

- [ ] **Step 1: Write one failing test per acceptance invariant**

```python
def test_numbered_item_without_body_fails_validation(rebuild_fixture):
    checks = validate_rebuild(*rebuild_fixture)
    assert failed(checks, "NUMBERED_BODY_COMPLETE")

def test_unconfirmed_item_is_not_counted_as_published(rebuild_fixture):
    checks = validate_rebuild(*rebuild_fixture)
    assert passed(checks, "NO_UNCONFIRMED_OUTPUT")

def test_broken_relative_link_fails_validation(rebuild_fixture):
    break_one_index_link(rebuild_fixture)
    assert failed(validate_rebuild(*rebuild_fixture), "ALL_LINKS_RESOLVE")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_validation.py -v`

- [ ] **Step 3: Implement all 15 spec acceptance checks**

Hash every original, parse Markdown frontmatter, resolve every relative link under the permitted roots, ensure exactly one index per published item, enforce numbered-body parity, and reject unexpected top-level directories. Return only aggregate counts and stable error codes.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_validation.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/validation.py tests/test_rebuild_validation.py
git commit -m "feat: validate rebuilt data acceptance gates"
```

### Task 2: Snapshot current state and apply deterministic rebuilt paths

**Files:**
- Create: `src/oa_knowledge/rebuild/state_copy.py`
- Test: `tests/test_rebuild_state_copy.py`

**Interfaces:**
- Produces: `backup_live_database(source: Path, target: Path) -> None`
- Produces: `apply_rebuilt_ledger(database_path: Path, run_id: int) -> dict[str, int]`
- Produces: `validate_database_copy(database_path: Path) -> list[ValidationCheck]`

- [ ] **Step 1: Write snapshot and remapping tests**

```python
def test_database_backup_is_consistent_while_source_is_open(live_database, target):
    backup_live_database(live_database, target)
    assert sqlite_integrity_check(target) == "ok"

def test_apply_ledger_updates_paths_only_in_copy(live_database, copied_database, rebuild_run):
    before = source_path(live_database)
    apply_rebuilt_ledger(copied_database, rebuild_run.id)
    assert source_path(live_database) == before
    assert source_path(copied_database).startswith("archive/")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_state_copy.py -v`

- [ ] **Step 3: Implement SQLite backup and ledger application**

Use `sqlite3.Connection.backup`, write to a temporary target, run `PRAGMA integrity_check` and Alembic revision verification, then atomically promote. Apply only successful current-run `RebuildOutput` paths in the copy. Preserve pending notification dedupe and current operational facts; do not carry retired filesystem paths into active file/export rows.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_state_copy.py tests/test_database.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/state_copy.py tests/test_rebuild_state_copy.py
git commit -m "feat: prepare rebuilt runtime database"
```

### Task 3: Add dry-run cutover and automatic rollback

**Files:**
- Create: `src/oa_knowledge/rebuild/cutover.py`
- Modify: `src/oa_knowledge/cli.py`
- Test: `tests/test_rebuild_cutover.py`
- Modify: `tests/test_rebuild_cli.py`

**Interfaces:**
- Produces: `CutoverPlan(live_root: Path, rebuilt_root: Path, legacy_root: Path, units: tuple[str, str, str, str, str])`
- Produces: `build_cutover_plan(settings: Settings, now: datetime) -> CutoverPlan`
- Produces: `execute_cutover(plan: CutoverPlan, *, authorized: bool) -> dict[str, str]`
- Produces CLI: `oa rebuild cutover --config config.yaml` as dry-run.
- Produces CLI: `oa rebuild cutover --config config.yaml --execute --authorization-token <token>`.

- [ ] **Step 1: Write destructive-safety tests**

```python
def test_cutover_without_execute_changes_nothing(layout, runner):
    result = runner.invoke(app, ["rebuild", "cutover", "--config", str(layout.config)])
    assert result.exit_code == 0
    assert layout.live.exists() and layout.rebuilt.exists()

def test_failed_smoke_restores_live_directory(layout, fake_systemd):
    fake_systemd.smoke_ok = False
    with pytest.raises(CutoverSmokeError):
        execute_cutover(layout.plan, authorized=True)
    assert layout.live_marker.read_text() == "legacy"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_cutover.py tests/test_rebuild_cli.py -v`

- [ ] **Step 3: Implement bounded rename-only cutover**

Require a freshly generated authorization token that includes the exact resolved live, rebuilt, and legacy paths. Stop and restart only `oaradar-web.service`, `oaradar-worker.service`, `oaradar-markdown-worker.service`, `oaradar-hourly.timer`, and `oaradar-nightly.timer`. Refuse cross-filesystem moves, existing legacy targets, dirty validation, or missing database backup. On failure, stop units, reverse the two completed renames, and restart the legacy deployment.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_cutover.py tests/test_rebuild_cli.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/cutover.py src/oa_knowledge/cli.py tests/test_rebuild_cutover.py tests/test_rebuild_cli.py
git commit -m "feat: add authorized rebuild cutover"
```

### Task 4: Add acceptance report, runbook, and full synthetic smoke

**Files:**
- Modify: `src/oa_knowledge/web/rebuild_views.py`
- Modify: `webui/src/views/RebuildClassificationView.tsx`
- Create: `scripts/smoke-data-rebuild.sh`
- Modify: `docs/runbook-oaradar-ops.md`
- Test: `tests/test_rebuild_e2e.py`
- Modify: `tests/test_web_rebuild_classification.py`

**Interfaces:**
- Produces: `GET /api/rebuild/validation` with aggregate checks only.
- Produces: a local synthetic end-to-end smoke covering classification, copy, body selection, parsing, Markdown, index, validation, cutover, and rollback.

- [ ] **Step 1: Write the synthetic end-to-end test**

```python
def test_rebuild_from_synthetic_live_data_to_valid_new_root(tmp_path):
    fixture = seed_internal_external_and_review_items(tmp_path)
    confirm_fixture_classifications(fixture)
    run_rebuild_until_idle(fixture)
    checks = validate_rebuild(fixture.session, fixture.settings, fixture.run_id)
    assert validation_passed(checks)
    assert fixture.numbered_body_count == 1
    assert fixture.no_number_body_count == 0
```

- [ ] **Step 2: Run test and confirm failure**

Run: `uv run pytest tests/test_rebuild_e2e.py -v`

- [ ] **Step 3: Implement report, smoke script, and runbook**

The smoke script must use a temporary directory and synthetic data only. The runbook must spell out inventory, classification, build, validation, authorization, cutover, rollback, 24-hour observation, 7-day observation, and separate legacy-deletion approval.

- [ ] **Step 4: Run the final automated gate**

Run: `uv run pytest tests/test_rebuild_e2e.py tests/test_rebuild_validation.py tests/test_rebuild_cutover.py -v`

Run: `uv run python scripts/check_public_release.py`

Run: `uv run pytest`

Run: `cd webui && npm ci && npm run check && npm run build`

Run: `uv run pytest tests/test_web_security.py tests/test_web_v2_contract.py tests/test_web_rebuild_classification.py tests/test_frontend_v2_assets.py -v`

Expected: all commands PASS and the Git worktree contains no `data/`, database, real filename, log, or browser artifact.

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/web/rebuild_views.py webui/src/views/RebuildClassificationView.tsx scripts/smoke-data-rebuild.sh docs/runbook-oaradar-ops.md tests
git commit -m "test: verify safe data rebuild and cutover"
```

### Task 5: Perform the real local dry run without cutover

**Files:**
- No tracked files unless redacted documentation needs correction.
- Create only ignored/private runtime outputs below the configured rebuild target and an out-of-repo backup directory.

**Interfaces:**
- Consumes all prior phase commands.
- Produces a redacted pass/fail summary for user review; does not perform cutover.

- [ ] **Step 1: Verify clean release candidate**

Run: `git status --short --branch && git diff --check`

- [ ] **Step 2: Create out-of-repo backups and run inventory**

Use SQLite backup API for the database and copy the local config and user unit files into a mode-`0700` temporary backup directory. Run `oa rebuild inventory`; report only aggregate counts.

- [ ] **Step 3: Require classification completion**

Run `oa rebuild status`. Expected: `needs_review=0`, `date_missing=0`, and every published candidate has `classification_state=confirmed`. If not, stop for WebUI review.

- [ ] **Step 4: Build and validate without switching**

Run the authorized non-destructive archive/Markdown rebuild commands, then `oa rebuild validate`. Expected: every required check is green. Do not run `oa rebuild cutover --execute`.

- [ ] **Step 5: Present evidence and request separate cutover authorization**

Report aggregate original counts, matching hashes, internal/external item counts, indexes, numbered bodies, attachment Markdown, unsupported, retryable failures, disk use, and validation results. Do not expose titles or paths. Wait for explicit authorization before Phase 4 Task 3's real `--execute` command.
