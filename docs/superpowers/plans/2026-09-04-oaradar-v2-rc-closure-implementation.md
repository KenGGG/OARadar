# OARadar V2 RC 发布收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保护当前本地候选代码，消除已知阻塞，完成三条核心流程的全量自动化验证和有界本机冒烟，并输出唯一的 RC 结论。

**Architecture:** 不新增产品架构或流水线；以当前本地 HEAD 为唯一代码事实源，依次执行工作区保护、已知阻塞修复、三线验收、隔离冒烟和证据收口。先验证现有行为，只有发现实际验收缺口时才按 TDD 作最小修复。

**Tech Stack:** Git、Python 3.12、SQLAlchemy、Alembic、pytest、FastAPI、React、TypeScript、Vite、systemd user units

**Spec:** `docs/superpowers/specs/2026-09-04-oaradar-v2-rc-closure-design.md`

## Global Constraints

- 当前本地工作区、当前 HEAD、当前本地配置和当前本地数据是唯一事实源；`origin/main` 仅用于差异审计。
- OA 侧始终严格只读；不得审批、回复、删除、转发或改变 OA 记录。
- 真实 OA 内容、配置、数据库、日志、Cookie、浏览器状态、下载文件、HTML、备份和冒烟证据不得进入 Git。
- 测试只使用合成或不可逆脱敏 fixture；OA 标识按文本存储；归档路径相对 `data_root`。
- 已验证原件不得覆盖；第 10 层仍有子项时记录 `depth_limit_reached` 且不得完成。
- 不开发冻结范围中的功能，不创建新架构，不物理清理旧代码，不执行全量历史发布或回填。
- 不 push、不合并 main、不推送标签、不删除分支、不修改版本号、不正式部署，除非 RC_PASS 后另获明确授权。
- 不使用 reset、rebase、clean 或默认 stash 处理用户修改；不把归属不明的修改纳入 RC。
- 完整 pytest 要求 0 failed；既有合法条件 skip 可保留并说明，不得新增或扩大 skip/xfail 掩盖失败。
- 本计划不包含 24 小时或 7 天观察；它们只属于部署后的 `OARADAR_V2_STABLE_PASS`。

---

### Task 0: 保护工作区并冻结候选边界

**Files:**
- Read: `AGENTS.md`
- Read: `README.md`
- Read: `README.zh-CN.md`
- Read: `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`
- Read: `docs/superpowers/specs/2026-09-04-oaradar-v2-rc-closure-design.md`
- Read: `docs/superpowers/plans/2026-08-19-oaradar-v2-phase-2-pending.md`
- Read: `docs/superpowers/plans/2026-08-19-oaradar-v2-phase-3-archive-markdown-boundary.md`
- Read: `docs/superpowers/plans/2026-08-19-oaradar-v2-phase-4-markdown-delivery.md`
- Read: `docs/runbook-oaradar-ops.md`
- Create outside repository: restricted-permission Git and worktree evidence files

**Interfaces:**
- Consumes: current local repository, current HEAD and all user-owned working-tree changes
- Produces: `release/oaradar-v2-rc-closure` candidate branch/worktree, verified Git bundle, redacted Gate 0 evidence

- [ ] **Step 1: Read the governing contracts before executing commands**

Read every file listed above completely. Record only their paths and confirmation status; do not copy OA content into the evidence ledger.

- [ ] **Step 2: Record the immutable Git baseline**

Run:

```bash
git status --short --branch
git branch --show-current
git rev-parse HEAD
git log --oneline --decorate -30
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git worktree list --porcelain
```

Expected: each command completes; the ledger records branch, HEAD, ahead commits, diff summary and worktrees without claiming that `origin/main` reflects local capability.

- [ ] **Step 3: Preserve a dirty original worktree without changing it**

Create a mode-0700 working directory outside the repository and record the Gate 0 HEAD:

```bash
OARADAR_RC_ROOT="$(mktemp -d /tmp/oaradar-v2-rc-closure.XXXXXX)"
chmod 700 "$OARADAR_RC_ROOT"
git rev-parse HEAD > "$OARADAR_RC_ROOT/gate0-head.txt"
```

If `git status --porcelain` is non-empty, save:

```bash
git diff --binary > "$OARADAR_RC_ROOT/working.diff"
git diff --cached --binary > "$OARADAR_RC_ROOT/index.diff"
git ls-files --others --exclude-standard > "$OARADAR_RC_ROOT/untracked.txt"
sha256sum "$OARADAR_RC_ROOT/working.diff" "$OARADAR_RC_ROOT/index.diff"
```

Copy only necessary untracked files into that restricted directory after inspecting the list; never copy ignored runtime data wholesale. Record SHA256 values for the saved diff files. Do not use stash, reset or clean.

If the worktree is clean, record `clean_worktree=true` and do not create empty backup artifacts.

- [ ] **Step 4: Create the candidate branch in the safe location**

For a clean original worktree, run:

```bash
git switch -c release/oaradar-v2-rc-closure
```

For a dirty original worktree, create an external worktree from the recorded HEAD instead:

```bash
OARADAR_GATE0_HEAD="$(sed -n '1p' "$OARADAR_RC_ROOT/gate0-head.txt")"
git worktree add -b release/oaradar-v2-rc-closure "$OARADAR_RC_ROOT/worktree" "$OARADAR_GATE0_HEAD"
```

Expected: the RC branch points exactly at the recorded HEAD; the dirty original worktree remains byte-for-byte untouched. The temporary worktree path must never be committed.

- [ ] **Step 5: Back up committed history with a Git bundle**

Run against the RC worktree:

```bash
git bundle create "$OARADAR_RC_ROOT/oaradar-v2-rc-closure.bundle" --all
git bundle verify "$OARADAR_RC_ROOT/oaradar-v2-rc-closure.bundle"
sha256sum "$OARADAR_RC_ROOT/oaradar-v2-rc-closure.bundle"
```

Expected: bundle verification succeeds. State explicitly that the bundle protects committed objects only; Step 3 protects uncommitted content.

- [ ] **Step 6: Confirm the candidate boundary**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git log --oneline origin/main..HEAD
```

Expected: correct RC branch, expected HEAD, no accidental inclusion of user changes. Do not commit merely to mark Task 0 complete.

### Task 1: 消除迁移与公开发布阻塞

**Files:**
- Inspect: `src/oa_knowledge/db/migrations/versions/0040_external_review_without_issuer.py`
- Inspect: `src/oa_knowledge/db/migrations/versions/0041_agnes_semantic_decision_source.py`
- Modify only if evidence requires: `tests/test_cli.py`
- Modify only if evidence requires: `tests/test_classification_migration.py`
- Modify: `docs/superpowers/plans/2026-08-29-agnes-safe-semantic-v2.md`
- Test: `tests/test_cli.py`
- Test: `tests/test_classification_migration.py`
- Test: `tests/test_database.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: Alembic revision chain ending at the current local head
- Produces: one verified Alembic head, proven fresh and 0040 upgrade paths, zero public-release findings

- [ ] **Step 1: Inspect the revision graph and migration contents**

Run:

```bash
uv run alembic heads
uv run alembic history --verbose
sed -n '1,240p' src/oa_knowledge/db/migrations/versions/0040_external_review_without_issuer.py
sed -n '1,240p' src/oa_knowledge/db/migrations/versions/0041_agnes_semantic_decision_source.py
```

Expected: exactly one head; revision 0041 has down_revision 0040. If multiple heads or a wrong parent exists, stop changing assertions and repair the existing migration graph with a failing synthetic migration test; do not create 0042 unless the graph cannot be corrected safely.

- [ ] **Step 2: Prove both upgrade paths on disposable databases**

Use `tmp_path`-backed synthetic databases through the existing migration test helpers. Ensure tests cover:

```python
def test_fresh_database_upgrades_to_single_head(tmp_path):
    # existing migration helper creates a blank SQLite database and upgrades to head
    assert current_revision == "0041_agnes_semantic_decision_source"


def test_0040_database_upgrades_to_0041_without_losing_rows(tmp_path):
    # upgrade disposable DB to 0040, insert a synthetic retained row, then upgrade to head
    assert current_revision == "0041_agnes_semantic_decision_source"
    assert retained_synthetic_row_count == 1
```

Add these assertions to `tests/test_classification_migration.py` or reuse equivalent existing tests; never downgrade, stamp or mutate the production database.

- [ ] **Step 3: Run migration tests and identify the actual defect**

Run:

```bash
uv run pytest tests/test_classification_migration.py tests/test_database.py tests/test_cli.py -q
```

Expected: the two disposable upgrade paths pass. If only `test_init_is_idempotent_and_status_works` expects 0040, update that exact expected head to 0041. Do not weaken assertions, skip tests or add a migration solely to satisfy the old string.

- [ ] **Step 4: Remove the local absolute path from the public document**

Replace the absolute `**Spec:**` target in `docs/superpowers/plans/2026-08-29-agnes-safe-semantic-v2.md` with a repository-relative reference to an existing committed specification. If no matching committed spec exists, replace it with the neutral text `**Spec:** user-approved design input (not stored in this repository)`; do not add the local attachment or relax the checker.

- [ ] **Step 5: Verify both blockers are gone**

Run:

```bash
uv run pytest tests/test_classification_migration.py tests/test_database.py tests/test_cli.py tests/test_public_release.py -q
uv run python scripts/check_public_release.py
git diff --check
```

Expected: 0 failed and Public Release Checker reports zero findings.

- [ ] **Step 6: Commit the minimal blocker fixes**

Stage only files actually changed after reviewing `git diff --name-only`:

```bash
git add tests/test_cli.py tests/test_classification_migration.py docs/superpowers/plans/2026-08-29-agnes-safe-semantic-v2.md
git diff --cached --check
git commit -m "fix: close RC migration and release checks"
```

Omit unchanged paths from `git add`. Expected: one reviewable commit containing no runtime data and no unrelated refactor.

### Task 2: Verify the three pipelines and complete automated regression

**Files:**
- Test: `tests/test_pending_sync.py`
- Test: `tests/test_pending_summary.py`
- Test: `tests/test_pending_cleanup.py`
- Test: `tests/test_pending_capture.py`
- Test: `tests/test_feishu_service.py`
- Test: `tests/test_scheduled_sync.py`
- Test: `tests/test_done_archive.py`
- Test: `tests/test_done_download_convergence.py`
- Test: `tests/test_archive.py`
- Test: `tests/test_detail_archive.py`
- Test: `tests/test_integrity_reconciliation.py`
- Test: `tests/test_markdown_delivery.py`
- Test: `tests/test_markdown_handoff.py`
- Test: `tests/test_markdown_queue.py`
- Test: `tests/test_markdown_export.py`
- Test: `tests/test_source_markdown_service.py`
- Test: `tests/test_parsers.py`
- Test: `tests/test_production_pipeline.py`
- Test: `tests/test_e2e_autorun.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_web_v2_contract.py`
- Test: `tests/test_web_retired_routes.py`
- Test: `tests/test_web_security.py`
- Modify only for a proven gap: the smallest existing test and production files exercising that gap

**Interfaces:**
- Consumes: three pipeline contracts from the approved spec and local tests that actually exist
- Produces: acceptance-item-to-test mapping, focused test results, complete regression results, reproducible frontend build

- [ ] **Step 1: Build the acceptance mapping from collected test names**

Run:

```bash
uv run pytest --collect-only -q
rg -n "baseline|discovery_hash|input_hash|unknown_outcome|pending_cleanup|depth_limit|archive_verify|content_signature|ParseArtifact|item_index|_index.md|mtime|retired" tests
```

Create a repository-external ledger with one row per Section 7 acceptance item: pipeline, requirement, exact test node ID, current evidence status. Do not claim coverage from a filename alone.

- [ ] **Step 2: Run the focused Pending suite**

Run:

```bash
uv run pytest tests/test_pending_sync.py tests/test_pending_summary.py tests/test_pending_cleanup.py tests/test_pending_capture.py tests/test_feishu_service.py tests/test_scheduled_sync.py tests/test_worker.py tests/test_e2e_autorun.py -q
```

Expected: 0 failed. Confirm exact test evidence for baseline behavior, `feishu:pending:{logical_item_id}:{input_hash}`, model-disabled zero calls, deterministic fallback, sent-to-cleanup crash recovery, unknown outcome and absence of archive/Markdown side effects.

- [ ] **Step 3: Run the focused Done Archive suite**

Run:

```bash
uv run pytest tests/test_done_archive.py tests/test_done_download_convergence.py tests/test_archive.py tests/test_detail_archive.py tests/test_integrity_reconciliation.py tests/test_production_pipeline.py tests/test_worker.py -q
```

Expected: 0 failed. Confirm exact test evidence for local integrity, no attachment, depth limit, unchanged manifest, immutable verified files, the `oa_item_key + content_signature + schema_version` task key and Markdown failure isolation.

- [ ] **Step 4: Run the focused Markdown Delivery suite**

Run:

```bash
uv run pytest tests/test_markdown_delivery.py tests/test_markdown_handoff.py tests/test_markdown_queue.py tests/test_markdown_export.py tests/test_source_markdown_service.py tests/test_parsers.py tests/test_production_pipeline.py tests/test_worker.py -q
```

Expected: 0 failed. Confirm exact evidence for verified-local input, no browser/OA/Feishu calls, active ParseArtifact-only publishing, current-schema item index uniqueness, stable `_index.md`, path independence and normal-schedule idempotence.

- [ ] **Step 5: Run cross-flow and Web contract tests**

Run:

```bash
uv run pytest tests/test_production_pipeline.py tests/test_e2e_autorun.py tests/test_worker.py tests/test_web_v2_contract.py tests/test_web_retired_routes.py tests/test_web_security.py -q
```

Expected: 0 failed; retired routes remain unavailable and Web security boundaries remain intact.

- [ ] **Step 6: Close only proven acceptance gaps with TDD**

For every ledger row without exact coverage, add one minimal test to the closest existing test file. Cross-flow gaps use one of these explicit node IDs; pipeline-local gaps use the same `test_rc_*_contract_gap` naming in the closest existing pipeline test file:

```bash
uv run pytest tests/test_e2e_autorun.py::test_rc_pending_contract_gap -q
uv run pytest tests/test_e2e_autorun.py::test_rc_done_archive_contract_gap -q
uv run pytest tests/test_e2e_autorun.py::test_rc_markdown_delivery_contract_gap -q
```

Create and run only the node required by the identified gap; do not add all three pre-emptively. Require that node to fail before changing production code. Then modify the shared production path responsible for the behavior, run the same node again, and rerun the corresponding focused suite. Do not manufacture RED for behavior already covered and passing; do not create a new test framework or refactor unrelated code.

- [ ] **Step 7: Run the complete Python and release gates without early stop**

Run exactly to final summaries:

```bash
uv run python scripts/check_public_release.py
uv run pytest
```

Expected: checker zero findings; pytest reaches 100% with 0 failed. Record passed and skipped counts and explain every existing skip; compare skipped count with the pre-fix baseline and investigate any increase.

- [ ] **Step 8: Reproduce and build the frontend**

Run:

```bash
cd webui
npm ci
npm run check
npm run build
cd ..
git diff --check
```

Expected: dependency installation, TypeScript check and Vite production build each exit 0. Generated static assets may change only if the checked-in frontend source requires it.

- [ ] **Step 9: Re-run Web and public-release verification after the build**

Run:

```bash
uv run pytest tests/test_web_v2_contract.py tests/test_web_retired_routes.py tests/test_web_security.py tests/test_public_release.py -q
uv run python scripts/check_public_release.py
```

Expected: 0 failed and zero findings.

- [ ] **Step 10: Commit only actual acceptance fixes**

If Task 2 changed files, group them by independently reviewable pipeline defect and commit each group after its focused suite passes. For example, a proven Pending defect is staged only with its closest source and test:

```bash
git diff --check
git add src/oa_knowledge/pending_sync.py tests/test_pending_sync.py
git diff --cached --check
git commit -m "fix: close pending RC acceptance gap"
```

Use the equivalent exact source/test pair and `archive` or `markdown` commit subject for a proven defect in those pipelines. Omit unchanged paths. If no files changed, record `task_2_code_changes=none`; do not create an empty commit.

### Task 3: Perform isolated, bounded, OA-read-only smoke tests

**Files:**
- Read: `docs/runbook-oaradar-ops.md`
- Read: `config.example.yaml`
- Create outside repository: isolated config, temporary database/data root, service-state snapshot, restricted raw logs, redacted smoke ledger
- Modify: none unless smoke reveals a reproducible acceptance defect that first receives a synthetic failing test under Task 2 rules

**Interfaces:**
- Consumes: Gate 2 green candidate, existing read-only OA login capability, 1–3 selected existing Done items
- Produces: redacted two-run smoke evidence and restored pre-smoke service state

- [ ] **Step 1: Capture service state and install guaranteed restoration**

For each OARadar Web, OA Worker, Markdown Worker, hourly timer and nightly timer unit, record `is-enabled` and `is-active`. Create a restricted shell session whose EXIT/INT/TERM trap restores each unit to its recorded state. Do not install or rewrite unit files.

Use the exact core unit names from the runbook and save both state axes before any stop:

```bash
OARADAR_UNITS="oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service oaradar-hourly.timer oaradar-nightly.timer"
for unit in $OARADAR_UNITS; do
  systemctl --user is-enabled "$unit" > "$OARADAR_RC_ROOT/$unit.enabled" 2>&1 || true
  systemctl --user is-active "$unit" > "$OARADAR_RC_ROOT/$unit.active" 2>&1 || true
done

restore_units() {
  for unit in $OARADAR_UNITS; do
    enabled_state="$(sed -n '1p' "$OARADAR_RC_ROOT/$unit.enabled")"
    active_state="$(sed -n '1p' "$OARADAR_RC_ROOT/$unit.active")"
    if [ "$enabled_state" = enabled ]; then systemctl --user enable "$unit"; else systemctl --user disable "$unit"; fi
    if [ "$active_state" = active ]; then systemctl --user start "$unit"; else systemctl --user stop "$unit"; fi
  done
}
trap restore_units EXIT INT TERM
```

Test the trap logic against the recorded state before stopping anything.

- [ ] **Step 2: Back up and fingerprint production-local state outside Git**

Using the documented backup procedure, copy the database, config and necessary runtime metadata into a mode-0700 directory outside the repository. Record aggregate original-file count, byte total and SHA256 prefixes for a bounded sample. Do not copy artifacts into the repository and do not reveal titles or filenames in the redacted ledger.

- [ ] **Step 3: Create an isolated smoke configuration**

Create temporary database, `data_root`, archive output and Source Markdown output paths outside production directories. Reuse only the existing read-only OA login mechanism. Force real Feishu transport off and use a temporary loopback Web port. Verify the resolved paths do not point at production originals or llm_wiki `wiki/`.

- [ ] **Step 4: Verify Pending baseline safely**

Against the isolated database/data root, run one real-OA baseline with notification disabled. Record aggregate occurrence/task/delivery counts before and after. Expected: occurrence baseline exists; detail open count, pending business task count and NotificationDelivery count remain zero. Do not clear or alter the production baseline.

Run synthetic/fake-transport Pending acceptance tests for a new item, exactly-once delivery, sent cleanup, crash recovery and unknown outcome; do not send a real Feishu message.

- [ ] **Step 5: Verify Done Archive idempotence on 1–3 existing items**

Select the smallest bounded sample that includes at least one attachment; include multi-attachment or nested content only if already available. Run the normal read-only capture into isolated local storage twice. Compare database rows, file count, size, SHA256 and mtime between runs.

Expected: no OA writes, no duplicate download, no verified-file overwrite, no hash or mtime change on the second normal run. Do not use `--force`, repair or rebuild for this check.

- [ ] **Step 6: Verify Markdown Delivery idempotence and isolation**

Use one verified isolated archive from Step 5. Run ParseArtifact → Source Markdown → `_index.md` twice and compare parse jobs, exports, paths, SHA256 and mtime.

Expected: active ParseArtifact-backed output, one current-schema item_index record, one stable `_index.md` path, no second parse/publish and no mtime change. Confirm no browser, OA or Feishu call. Prove Markdown failure isolation only with an existing synthetic test, fake parser or disposable copy; never damage production originals or database.

- [ ] **Step 7: Verify the candidate Web surface**

Start the candidate Web process with isolated config on a temporary loopback port. Check Overview, Pending Notifications, Done Archives, Markdown Output and Settings; compare aggregate values with the isolated database. Confirm retired APIs are 404 or explicitly retired, routine navigation excludes retired capabilities, Host/Origin/CSRF protections remain active, and healthcheck reports only core services/timers.

- [ ] **Step 8: Restore services and verify restoration even after failure**

Allow the trap/finally handler to run, then independently query every unit again. Expected: enabled/disabled and active/inactive exactly match the Step 1 snapshot. `OARADAR_V2_RC_PASS` does not deploy or leave candidate services active.

- [ ] **Step 9: Finalize the redacted smoke ledger**

Record only commands, exit codes, aggregate counts, status codes, relative candidate paths and short hash prefixes. Keep any raw stdout/log containing real titles or filenames in a restricted repository-external directory. Run `git status --short` and Public Release Checker to prove no smoke artifact entered Git.

### Task 4: Freeze evidence and issue the unique RC decision

**Files:**
- Create outside repository: final redacted RC evidence ledger and acceptance matrix
- Modify: public operator documentation only if Task 2 proved it materially wrong
- Commit: only reviewed source/test/public-document changes from Tasks 1–3

**Interfaces:**
- Consumes: Gate 0 baseline, Gate 1 blocker evidence, Gate 2 complete results, Gate 3 smoke evidence
- Produces: `OARADAR_V2_RC_PASS` or a precise `OARADAR_V2_RC_BLOCKED`

- [ ] **Step 1: Review every candidate change and commit boundary**

Run:

```bash
git status --short --branch
git diff --check
OARADAR_GATE0_HEAD="$(sed -n '1p' "$OARADAR_RC_ROOT/gate0-head.txt")"
git diff --stat "$OARADAR_GATE0_HEAD"..HEAD
git log --oneline "$OARADAR_GATE0_HEAD"..HEAD
git diff --name-only "$OARADAR_GATE0_HEAD"..HEAD
```

Expected: every changed file maps to an identified blocker or acceptance defect; no real data, backup, raw log or unrelated feature appears.

- [ ] **Step 2: Run the final verification commands fresh**

Run to completion after the final commit:

```bash
uv run python scripts/check_public_release.py
uv run pytest
cd webui
npm ci
npm run check
npm run build
cd ..
uv run pytest tests/test_web_v2_contract.py tests/test_web_retired_routes.py tests/test_web_security.py tests/test_public_release.py -q
git status --short --branch
```

Expected: zero release findings, complete pytest 0 failed, explained legitimate skips only, frontend check/build exit 0, Web/public tests 0 failed, and no uncommitted generated change.

- [ ] **Step 3: Complete the acceptance matrix**

For every spec Section 7 item, record: requirement, exact test node ID, latest result, redacted smoke evidence or `automated-only` with reason. No row may be blank and no filename-only inference counts as coverage.

- [ ] **Step 4: Audit safety and service restoration**

Confirm: OA write count zero; real Feishu test messages zero; production-original mutation count zero; Git confidential finding count zero; all services restored to pre-smoke state; no full historical job executed.

- [ ] **Step 5: Issue exactly one terminal RC state**

If every RC gate passes, output:

```text
OARADAR_V2_RC_PASS
```

and state: “OARadar V2 三条核心流程的代码开发已经完成。24 小时和 7 天属于部署后的稳定性观察，不再属于开发阻塞项。”

If and only if an external condition cannot be resolved locally after exhaustive safe checks, output:

```text
OARADAR_V2_RC_BLOCKED
```

with the exact blocking command, exact error, completed investigation, why local continuation is impossible and one concrete user decision. Ordinary test failures, code defects and public-document findings require continued repair and are not `RC_BLOCKED`.

- [ ] **Step 6: Present the evidence in the required order**

Final response sections: current branch/HEAD; commits relative to `origin/main`; changed files/reasons; Alembic head and both disposable upgrade paths; Public Release Checker; complete pytest totals; frontend check/build; three-line acceptance matrix; redacted smoke evidence; unresolved issues; data-safety assessment; the sole next authorization; terminal state. Do not use completion percentages or propose more RC development after `RC_PASS`.
