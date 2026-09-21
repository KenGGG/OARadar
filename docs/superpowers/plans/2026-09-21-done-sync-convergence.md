# Done Sync Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every non-excluded Done item converge through durable download and Markdown tasks, show truthful UI states, and run the complete workflow every day at 23:30 Asia/Shanghai.

**Architecture:** Add a database-driven convergence planner that creates or revives existing `PipelineTask` rows without bypassing the production worker. Make simple status classification consume the latest task facts so only actionable queued/running work is called “waiting”; terminal failures become attention. Keep systemd scheduling in the committed timer template and remove the deployed 06:00-only override.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite, Typer, pytest, systemd user units, React/TypeScript WebUI.

**Spec:** `docs/superpowers/specs/2026-09-21-done-sync-convergence-design.md`

## Global Constraints

- OA access remains read-only; never approve, reply, delete, forward, or alter OA records.
- Existing excluded items remain excluded and receive no download or Markdown tasks.
- Container traversal remains capped at depth 10; `depth_limit_reached` is never complete.
- OA identifiers remain text and archive paths remain relative to `data_root`.
- Tests use synthetic fixtures only; no OA content, browser profile, runtime database, logs, or downloaded files enter Git.
- Existing user changes in the working tree must be preserved.

## Review Focus

- A non-excluded item with a terminal failed download task must display attention and be requeued only when the failure is recoverable.
- An archived item with no pipeline history must receive exactly one Markdown task across repeated planner runs.
- An excluded item with stale historical tasks must remain excluded and must not be revived.
- An item with a failed Markdown task and no successful index must display attention rather than waiting.
- A timer reinstall with a stale local 06:00 drop-in must remove the override and schedule the next run at 23:30.

---

### Task 1: Durable convergence planner

**Files:**
- Create: `src/oa_knowledge/done_convergence.py`
- Modify: `src/oa_knowledge/production_pipeline.py`
- Test: `tests/test_done_convergence.py`

**Interfaces:**
- Consumes: `ProductionQueue.enqueue(queue_name, logical_item_key, stage, idempotency_key, payload=None) -> int` and existing manifest, item, export, and pipeline tables.
- Produces: `DoneConvergenceReport` and `DoneConvergencePlanner.plan(*, apply: bool) -> DoneConvergenceReport`.

- [ ] **Step 1: Write failing planner tests**

Create synthetic database fixtures for: excluded, missing download, recoverable failed download, archived-without-Markdown-task, terminal Markdown failure, and already active tasks. Assert literal report counts and task stages. The idempotency test calls `plan(apply=True)` twice and asserts the second call creates zero rows.

```python
def test_planner_skips_excluded_and_creates_missing_pipeline_tasks(factory, settings):
    seed_manifest(factory, key="done:excluded", status="skipped", excluded="synthetic")
    seed_manifest(factory, key="done:download", status="pending_download")
    seed_archived_item(factory, key="done:markdown", complete_index=False)

    first = DoneConvergencePlanner(factory, settings).plan(apply=True)
    second = DoneConvergencePlanner(factory, settings).plan(apply=True)

    assert first.download_created == 1
    assert first.markdown_created == 1
    assert first.excluded == 1
    assert second.download_created == 0
    assert second.markdown_created == 0
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_done_convergence.py`

Expected: collection fails because `oa_knowledge.done_convergence` does not exist.

- [ ] **Step 3: Implement the report and planner**

Use immutable counters and one transaction. Select only manifests without exclusions. For each item, inspect the newest task for its logical key and the delivery facts. Create a download task when reliable archive evidence is absent; create a Markdown task when archive evidence exists but the item index is incomplete. Revive only failed rows whose `recoverable` flag is true. Insert a `PipelineEvent(event_type="convergence_requeued")` for every revived row.

```python
@dataclass(frozen=True)
class DoneConvergenceReport:
    eligible: int
    excluded: int
    download_created: int
    download_requeued: int
    markdown_created: int
    markdown_requeued: int
    attention: int
```

Use deterministic idempotency keys:

```python
f"converge-download:{oa_item_key}:v1"
f"converge-markdown:{oa_item_key}:{archive_signature}:v1"
```

- [ ] **Step 4: Run planner tests and verify GREEN**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_done_convergence.py tests/test_production_pipeline.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the planner**

```bash
git add src/oa_knowledge/done_convergence.py src/oa_knowledge/production_pipeline.py tests/test_done_convergence.py
git commit -m "feat: converge eligible Done pipeline tasks"
```

### Task 2: CLI and nightly integration

**Files:**
- Modify: `src/oa_knowledge/cli.py`
- Modify: `scripts/daily_done_delivery.py`
- Test: `tests/test_done_convergence_cli.py`
- Test: `tests/test_daily_done_delivery.py`

**Interfaces:**
- Consumes: `DoneConvergencePlanner.plan(apply: bool)` from Task 1.
- Produces: `oa done-converge [--dry-run] --config PATH` and a `convergence` section in the daily summary.

- [ ] **Step 1: Write failing CLI and daily integration tests**

Invoke the Typer command with a synthetic config. Assert dry-run creates no task, apply creates the task, JSON contains only aggregate counts, and the daily caller invokes convergence after nightly discovery but before queue draining.

```python
def test_done_converge_dry_run_is_read_only(runner, config_file):
    result = runner.invoke(app, ["done-converge", "--dry-run", "--config", str(config_file)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["applied"] is False
    assert pipeline_task_count(config_file) == 0
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_done_convergence_cli.py tests/test_daily_done_delivery.py`

Expected: CLI command is unknown and the daily summary lacks `convergence`.

- [ ] **Step 3: Implement CLI and daily call**

Add a Typer command that upgrades/opens the configured database, constructs the planner, prints `json.dumps(asdict(report) | {"applied": not dry_run})`, and disposes the engine. In `daily_done_delivery.py`, run the planner after `run_nightly_scan` and before `drain`, storing only the aggregate report in `summary["convergence"]`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_done_convergence_cli.py tests/test_daily_done_delivery.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit CLI integration**

```bash
git add src/oa_knowledge/cli.py scripts/daily_done_delivery.py tests/test_done_convergence_cli.py tests/test_daily_done_delivery.py
git commit -m "feat: run Done convergence after daily scan"
```

### Task 3: Truthful simple status mapping

**Files:**
- Modify: `src/oa_knowledge/web/simple_status.py`
- Modify: `src/oa_knowledge/web/delivery_facts.py`
- Test: `tests/test_simple_delivery_facts.py`
- Test: `tests/test_simple_status.py`
- Test: `tests/test_simple_done_status.py`

**Interfaces:**
- Consumes: newest `PipelineTask` per logical item key and delivery facts.
- Produces: `_done_simple_status_map(session, *, items=None, facts=None)` states where waiting always implies an active task.

- [ ] **Step 1: Write failing status tests**

Add literal cases for failed download, missing task, active download, failed Markdown, active Markdown, excluded-with-stale-task, and depth-limit. Assert both state and attention reason.

```python
def test_failed_download_task_is_attention_not_waiting(factory):
    manifest = seed_manifest(factory, status="pending_download")
    seed_task(factory, manifest.oa_item_key, queue="realtime_done", status="failed",
              error_code="PIPELINE_TASK_FAILED")
    state, _, reason = _done_simple_status_map(factory())[manifest.id]
    assert state == "attention"
    assert reason == "原件下载失败"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_simple_delivery_facts.py tests/test_simple_status.py tests/test_simple_done_status.py`

Expected: failed and missing-task cases incorrectly return a waiting state.

- [ ] **Step 3: Implement latest-task-aware classification**

Load the newest relevant `PipelineTask` for each manifest in one query. Pass a small immutable task fact into `_classify_done_item`. Apply this precedence:

```text
excluded > depth-limit/review > terminal failure > verified complete > active download
> active Markdown > missing-task attention
```

Do not query per row. Preserve server-side pagination and existing delivery fact behavior.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_simple_delivery_facts.py tests/test_simple_status.py tests/test_simple_done_status.py tests/test_webui_workflows.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit status mapping**

```bash
git add src/oa_knowledge/web/simple_status.py src/oa_knowledge/web/delivery_facts.py tests/test_simple_delivery_facts.py tests/test_simple_status.py tests/test_simple_done_status.py
git commit -m "fix: report actionable Done workflow states"
```

### Task 4: Single-source nightly schedule

**Files:**
- Modify: `scripts/systemd/templates/oaradar-nightly.timer.in`
- Modify: `scripts/install-systemd-user.sh`
- Modify: `tests/test_systemd_render.py`

**Interfaces:**
- Consumes: existing systemd rendering context.
- Produces: a daily 23:30 timer and removal of the obsolete `daily-0600.conf` during install.

- [ ] **Step 1: Write failing render/install behavior tests**

Assert the rendered timer contains the literal daily calendar, does not contain `Mon..Fri`, and an install fixture with `oaradar-nightly.timer.d/daily-0600.conf` removes that exact obsolete file without deleting unrelated drop-ins.

```python
def test_nightly_timer_is_daily_at_2330(rendered):
    timer = rendered["oaradar-nightly.timer"]
    assert "OnCalendar=*-*-* 23:30:00 Asia/Shanghai" in timer
    assert "Mon..Fri" not in timer
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_systemd_render.py`

Expected: current template still renders a weekday-only calendar.

- [ ] **Step 3: Implement the template and targeted cleanup**

Change only the nightly timer calendar to `*-*-* 23:30:00 {{TIMEZONE}}`. In the installer, remove only `$SYSTEMD_DIR/oaradar-nightly.timer.d/daily-0600.conf` before daemon reload, then remove the directory only when empty. Do not delete arbitrary operator drop-ins.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q tests/test_systemd_render.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit schedule unification**

```bash
git add scripts/systemd/templates/oaradar-nightly.timer.in scripts/install-systemd-user.sh tests/test_systemd_render.py
git commit -m "fix: schedule daily Done convergence at 2330"
```

### Task 5: Deploy, backfill, and verify convergence

**Files:**
- Modify runtime only: user systemd units under `~/.config/systemd/user`
- Modify runtime only: configured local database and `data_root`
- Test: existing complete Python test suite

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: deployed 23:30 schedule, populated durable queues, and an evidence-backed convergence report.

- [ ] **Step 1: Run complete automated verification**

Run: `PYTHONPATH=src:. .venv/bin/pytest -o addopts='' -q`

Expected: zero failures. If an unrelated existing failure remains, record its exact test name and do not claim a green suite.

- [ ] **Step 2: Run dry-run convergence against local facts**

Run: `PYTHONPATH=src:. .venv/bin/oa done-converge --dry-run --config config.yaml`

Expected: aggregate counts only; excluded count is nonzero and excluded task creation is zero.

- [ ] **Step 3: Back up runtime configuration and apply convergence**

Copy the current timer/service unit and local database to a timestamped directory under `data_root/runs/deploy-backups/`. Then run:

```bash
PYTHONPATH=src:. .venv/bin/oa done-converge --config config.yaml
```

Expected: missing and recoverable tasks are durably queued; excluded items are unchanged.

- [ ] **Step 4: Deploy the single timer source**

Render/install the units, remove only the obsolete 06:00 drop-in, reload user systemd, and restart the timer. Verify with:

```bash
systemctl --user list-timers oaradar-nightly.timer --all --no-pager
systemctl --user cat oaradar-nightly.timer
```

Expected: next trigger is 23:30 Asia/Shanghai and no 06:00 override remains.

- [ ] **Step 5: Monitor the durable queues to terminal or actionable states**

Query aggregate counts only. Wait while active tasks are progressing. Completion criteria:

```text
non-excluded waiting-without-task = 0
queued/running items have a live worker and lease
terminal failures display attention with a reason
new Markdown records match files and SHA-256
```

- [ ] **Step 6: Verify services and output integrity**

Confirm `oaradar-worker.service`, `oaradar-markdown-worker.service`, and `oaradar-nightly.timer` are active. For Markdown records created during the run, verify every file exists under `markdown_root`, resides in an internal/external classified directory, and matches its stored SHA-256.

- [ ] **Step 7: Commit final operational documentation if it changed**

```bash
git add docs/runbook-oaradar-ops.md
git commit -m "docs: document Done convergence operations"
```

Skip this commit when the runbook needs no change.
